"""
쿠팡이츠 크롤러  (Playwright + 페이지 내부 API 인터셉트)

쿠팡이츠 사장님 포털은 DOM 이 아니라 내부 JSON API 로 데이터를 불러온다.
그런데 이 API 는 Akamai 봇 차단이 걸려 있어서, 페이지 밖에서 직접 호출하거나
스크립트로 fetch 하면 403 으로 막힌다(센서 쿠키 오염).

해법: **페이지 자신이 날리는 요청만 통과한다.** 그래서 page.route 로 페이지의
reviews/search 요청을 가로채 날짜/페이지 파라미터만 바꾸고(Akamai 쿠키는 그대로
유지) 응답을 캡처한다.

정찰(2026-07)로 확인:
  - 리뷰 API : GET /api/v1/merchant/reviews/search
      params: storeId, page, statusType=EXPOSE, startDateTime=YYYY-MM-DD,
              exclusiveEndDateTime=YYYY-MM-DD
      resp.data = {content:[...], pageNumber, pageSize, total}
      리뷰 원소: orderReviewId, rating(float), comment, customerName,
                createdAt, orderedAt, orderInfo[{dishName}], replies[], tags,
                statusType, orderType
  - 매장 storeId 예: 889230 (베어글스 송도), 전체 944 / 미답변 26

TODO: 주문(매출) API 는 별도 정찰 후 동일 패턴으로 구현.
"""

import json
import logging
import re
from datetime import date, timedelta

from bot.notify import send_manual_login_alert
from crawler.browser import (
    BrowserSession, SessionExpiredError, human_pause, is_session_expired,
)

logger = logging.getLogger(__name__)

PLATFORM = "쿠팡이츠"
REVIEWS_URL = "https://store.coupangeats.com/merchant/management/reviews"
REVIEW_API_GLOB = "**/reviews/search*"

# .env 에 COUPANG_STORE_ID 를 넣으면 그 값을, 없으면 페이지에서 자동 감지.
import os  # noqa: E402
STORE_ID = os.getenv("COUPANG_STORE_ID", "889230")


class CoupangCrawler:
    """쿠팡이츠 사장님 포털 크롤러 (내부 API 인터셉트 방식)."""

    def __init__(self, headless=False, mode=None):
        self.headless = headless
        self.mode = mode
        self._session = None
        self.page = None

    def __enter__(self):
        self._session = BrowserSession(mode=self.mode, headless=self.headless)
        self._session.__enter__()
        self.page = self._session.page
        return self

    def __exit__(self, *exc):
        if self._session:
            self._session.__exit__(*exc)
            self._session = None
        self.page = None

    # -- 리뷰 ---------------------------------------------------------------

    def fetch_reviews(self, days=2, max_pages=5):
        """최근 days 일간의 리뷰를 수집한다.

        페이지의 reviews/search 요청을 가로채 날짜 범위와 page 번호를 바꿔가며
        전체 페이지를 순회한다. 각 페이지마다 페이지를 리로드해 페이지 자신이
        요청을 날리게 한다(Akamai 통과).

        Returns: 배민과 동일한 스키마의 dict 리스트.
        """
        start = (date.today() - timedelta(days=days)).isoformat()
        # exclusiveEndDateTime 은 배타적이므로 내일로 설정해 오늘까지 포함
        end = (date.today() + timedelta(days=1)).isoformat()

        reviews = []
        for page_num in range(1, max_pages + 1):
            data = self._fetch_review_page(page_num, start, end)
            if data is None:
                break
            content = data.get("content", [])
            reviews.extend(self._normalize_review(r) for r in content)
            total = data.get("total", 0)
            if not content or len(reviews) >= total:
                break
            human_pause(2.0, 3.5)

        logger.info("쿠팡 리뷰 %d건 수집 (최근 %d일)", len(reviews), days)
        return reviews

    def _fetch_review_page(self, page_num, start, end):
        """reviews/search 요청을 가로채 파라미터를 바꿔 한 페이지를 가져온다."""
        def handle(route):
            u = route.request.url
            u = re.sub(r"startDateTime=[0-9-]+", f"startDateTime={start}", u)
            u = re.sub(r"exclusiveEndDateTime=[0-9-]+",
                       f"exclusiveEndDateTime={end}", u)
            u = re.sub(r"([?&])page=\d+", rf"\1page={page_num}", u)
            route.continue_(url=u)

        self.page.route(REVIEW_API_GLOB, handle)
        try:
            with self.page.expect_response(
                    lambda r: "reviews/search" in r.url, timeout=20000) as ri:
                self.page.goto(REVIEWS_URL, wait_until="domcontentloaded")
            resp = ri.value
        except Exception:  # noqa: BLE001
            # 응답 캡처 실패 — 세션 만료 여부 확인
            if is_session_expired(self.page):
                send_manual_login_alert(PLATFORM)
                raise SessionExpiredError(
                    f"[{PLATFORM}] 로그인 세션이 만료되었습니다. "
                    f"launch_chrome.bat 로 띄운 Chrome 에서 다시 로그인하세요.")
            logger.warning("쿠팡 리뷰 응답 캡처 실패 (page %d)", page_num)
            return None
        finally:
            self.page.unroute(REVIEW_API_GLOB, handle)

        if resp.status != 200:
            logger.warning("쿠팡 리뷰 API status %d", resp.status)
            return None
        try:
            return resp.json().get("data", {})
        except Exception:  # noqa: BLE001
            logger.warning("쿠팡 리뷰 JSON 파싱 실패")
            return None

    @staticmethod
    def _normalize_review(r):
        """쿠팡 리뷰 JSON 원소를 공용 스키마 dict 로 정규화한다."""
        created = r.get("createdAt") or ""
        written_date = created[:10] if len(created) >= 10 else None
        menus = [o.get("dishName") for o in (r.get("orderInfo") or [])
                 if o.get("dishName")]
        rating = r.get("rating")
        return {
            "platform": "coupang",
            "review_no": str(r.get("orderReviewId")) if r.get(
                "orderReviewId") is not None else None,
            "author": r.get("customerName"),
            "rating": int(rating) if isinstance(rating, (int, float)) else None,
            "content": (r.get("comment") or "").strip() or None,
            "written_at": created or None,
            "written_date": written_date,
            "menus": menus or None,
            "delivery_type": r.get("orderType"),
            "order_count": r.get("orderCount"),  # 첫/재주문 판별(답글 개인화)
            # replies 가 있으면 이미 답변한 리뷰
            "reply_status": "posted" if r.get("replies") else "none",
            "raw": json.dumps(r, ensure_ascii=False),
        }

    # -- 주문 (TODO) --------------------------------------------------------

    def fetch_orders(self, start_date=None, end_date=None):
        """쿠팡이츠 주문/매출을 수집한다. (주문 API 정찰 후 구현 예정)"""
        raise NotImplementedError("쿠팡 주문 API 미구현 (정찰 필요)")


def fetch_reviews(days=2):
    with CoupangCrawler() as c:
        return c.fetch_reviews(days=days)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    with CoupangCrawler() as c:
        revs = c.fetch_reviews(days=30, max_pages=3)
        print(f"쿠팡 리뷰 {len(revs)}건:")
        for r in revs[:5]:
            print(f" - [{r['author']}] ★{r['rating']} "
                  f"{r['reply_status']} {(r['content'] or '(사진)')[:40]}")
