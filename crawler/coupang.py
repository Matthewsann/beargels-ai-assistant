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
from datetime import date, datetime, timedelta, timezone

from alerts import session_expired
from crawler.browser import (
    BrowserSession, SessionExpiredError, human_pause, is_session_expired,
)

logger = logging.getLogger(__name__)

PLATFORM = "쿠팡이츠"
REVIEWS_URL = "https://store.coupangeats.com/merchant/management/reviews"
REVIEW_API_GLOB = "**/reviews/search*"

# 주문(매출) — 정찰(2026-07)로 확인한 내부 API:
#   POST /api/v1/merchant/web/order/condition
#     body: {pageNumber, pageSize, storeId, startDate(epoch ms), endDate(epoch ms)}
#     resp: {totalSalePrice, totalOrderCount, avgOrderAmount,
#            orderPageVo:{content:[...], pageNumber, total, totalElements,
#                         lastPageNumber}}
#     주문 원소: orderId, uniqueOrderId, abbrOrderId, totalAmount, salePrice,
#               actuallyAmount, discountPrice, canceledAmount, status,
#               createdAt(epoch ms), reviewRating, items[{name,quantity,
#               unitSalePrice,subTotalPrice,itemOptions[...]}]
# 리뷰와 동일하게 Akamai 봇차단 → 페이지 자신의 POST 를 가로채 body 만 바꾼다.
ORDERS_URL = "https://store.coupangeats.com/merchant/management/orders/{store}"
ORDER_API_GLOB = "**/merchant/web/order/condition"

# .env 에 COUPANG_STORE_ID 를 넣으면 그 값을, 없으면 페이지에서 자동 감지.
import os  # noqa: E402
STORE_ID = os.getenv("COUPANG_STORE_ID", "889230")

# 쿠팡 createdAt/날짜필터는 KST epoch 밀리초 기준.
KST = timezone(timedelta(hours=9))


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
                session_expired(PLATFORM)
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
            # 문제리뷰 보고용 — 어떤 '주문'에 대한 리뷰인지(직원 단톡 공유).
            "order_no": r.get("abbrOrderId") or (
                str(r.get("orderId")) if r.get("orderId") else None),
            "ordered_at": (r.get("orderedAt") or "").replace("T", " ")[:16]
                          or None,
            # replies 가 있으면 이미 답변한 리뷰. 실제 답글 본문은 AI 답글
            # 공부의 학습 데이터로 쓴다(2026-08-10).
            "reply_status": "posted" if r.get("replies") else "none",
            "platform_reply": ((r.get("replies") or [{}])[0].get("content")
                               or None) if r.get("replies") else None,
            "raw": json.dumps(r, ensure_ascii=False),
        }

    # -- 주문 (매출) --------------------------------------------------------

    def fetch_orders(self, days=1, start_date=None, end_date=None,
                     max_pages=60, page_size=10):
        """최근 days 일간의 주문/매출을 수집한다.

        페이지의 order/condition POST 를 가로채 body(날짜·페이지)를 바꿔가며
        전체 페이지를 순회한다(리뷰와 동일한 Akamai 우회). 각 페이지마다 주문
        페이지를 리로드해 페이지 자신이 요청을 날리게 한다.

        Args:
            days: 오늘 포함 최근 며칠 (start/end_date 미지정 시). 기본 1=어제~오늘.
            start_date/end_date: date 또는 'YYYY-MM-DD'. 주면 days 무시.

        Returns: 배민과 동일 스키마의 주문 dict 리스트(price=salePrice).
        """
        start_ms, end_ms = self._date_range_ms(days, start_date, end_date)

        orders = []
        for page_num in range(max_pages):
            data = self._fetch_order_page(page_num, start_ms, end_ms, page_size)
            if data is None:
                break
            opv = data.get("orderPageVo") or {}
            content = opv.get("content") or []
            orders.extend(self._normalize_order(o) for o in content)
            total = opv.get("totalElements") or opv.get("total") or 0
            last = opv.get("lastPageNumber")
            if (not content or len(orders) >= total
                    or (last is not None and page_num >= last)):
                break
            human_pause(2.0, 3.5)

        logger.info("쿠팡 주문 %d건 수집 (최근 %s일)",
                    len(orders), days if not start_date else "기간지정")
        return orders

    def fetch_sales_summary(self, days=1, start_date=None, end_date=None):
        """집계 매출 지표만 한 번의 호출로 가져온다(주문 목록 순회 없이).

        Returns: {revenue, order_count, avg_order_value, cancelled_count} 또는
                 실패 시 None.
        """
        start_ms, end_ms = self._date_range_ms(days, start_date, end_date)
        data = self._fetch_order_page(0, start_ms, end_ms, page_size=1)
        if data is None:
            return None
        rev = data.get("totalSalePrice")
        return {
            "revenue": int(rev) if isinstance(rev, (int, float)) else 0,
            "order_count": data.get("totalOrderCount") or 0,
            "avg_order_value": round(data.get("avgOrderAmount") or 0),
            "cancelled_count": data.get("totalCancelledOrderCount") or 0,
        }

    @staticmethod
    def _date_range_ms(days, start_date, end_date):
        """조회 구간을 KST epoch 밀리초 (start, end) 로 변환한다."""
        def _to_date(d):
            if d is None:
                return None
            if isinstance(d, date):
                return d
            return datetime.strptime(d, "%Y-%m-%d").date()

        sd, ed = _to_date(start_date), _to_date(end_date)
        now = datetime.now(KST)
        if sd is None:
            sd = (now - timedelta(days=days)).date()
        if ed is None:
            ed = now.date()
        start = datetime(sd.year, sd.month, sd.day, 0, 0, 0, tzinfo=KST)
        end = datetime(ed.year, ed.month, ed.day, 23, 59, 59, 999000,
                       tzinfo=KST)
        return int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    # 쿠팡 order/condition 은 pageSize 가 크면(>~10) 빈 응답을 준다(검증됨).
    MAX_PAGE_SIZE = 10
    # 짧은 시간에 반복 요청하면 error.code=10056(레이트리밋)을 준다 → 백오프.
    RATE_LIMIT_CODE = "10056"

    def _fetch_order_page(self, page_num, start_ms, end_ms, page_size,
                          retries=3):
        """order/condition POST 를 가로채 body 를 바꿔 한 페이지를 가져온다.

        200 이어도 body 가 {data:null, error:{code:10056}} 인 레이트리밋 응답이
        올 수 있어, 그 경우 백오프 후 재시도한다. 최종 실패는 None.
        """
        page_size = min(page_size, self.MAX_PAGE_SIZE)

        def handle(route):
            try:
                body = json.loads(route.request.post_data or "{}")
            except Exception:  # noqa: BLE001
                body = {}
            body.update({
                "pageNumber": page_num,
                "pageSize": page_size,
                "storeId": int(STORE_ID),
                "startDate": start_ms,
                "endDate": end_ms,
            })
            route.continue_(post_data=json.dumps(body))

        url = ORDERS_URL.format(store=STORE_ID)
        for attempt in range(1, retries + 1):
            self.page.route(ORDER_API_GLOB, handle)
            try:
                with self.page.expect_response(
                        lambda r: "order/condition" in r.url,
                        timeout=20000) as ri:
                    self.page.goto(url, wait_until="domcontentloaded")
                resp = ri.value
            except Exception:  # noqa: BLE001
                if is_session_expired(self.page):
                    session_expired(PLATFORM)
                    raise SessionExpiredError(
                        f"[{PLATFORM}] 로그인 세션이 만료되었습니다. "
                        f"launch_chrome.bat 로 띄운 Chrome 에서 다시 로그인하세요.")
                logger.warning("쿠팡 주문 응답 캡처 실패 (page %d)", page_num)
                return None
            finally:
                self.page.unroute(ORDER_API_GLOB, handle)

            if resp.status != 200:
                logger.warning("쿠팡 주문 API status %d", resp.status)
                return None
            try:
                data = resp.json()
            except Exception:  # noqa: BLE001
                logger.warning("쿠팡 주문 JSON 파싱 실패")
                return None

            err = data.get("error") if isinstance(data, dict) else None
            if err:
                code = err.get("code") if isinstance(err, dict) else None
                if code == self.RATE_LIMIT_CODE and attempt < retries:
                    # 레이트리밋 — 점점 길게 쉬었다 재시도.
                    logger.info("쿠팡 레이트리밋(10056) — 백오프 후 재시도 %d/%d",
                                attempt, retries)
                    human_pause(8.0 * attempt, 14.0 * attempt)
                    continue
                logger.warning("쿠팡 주문 API 오류 %s: %s",
                               code, (err or {}).get("message"))
                return None
            return data
        return None

    @staticmethod
    def _normalize_order(o):
        """쿠팡 주문 JSON 원소를 공용 스키마(배민과 동일) dict 로 정규화한다.

        매출(price)은 salePrice(판매가)를 쓴다 — 집계 totalSalePrice 와 합이
        일치한다. 취소 주문은 status 로 구분(집계에서 별도 처리).
        """
        created = o.get("createdAt")
        ordered_at, ordered_date = None, None
        if isinstance(created, (int, float)):
            dt = datetime.fromtimestamp(created / 1000, tz=KST)
            ordered_at = dt.strftime("%Y. %m. %d. %H:%M:%S")
            ordered_date = dt.date().isoformat()

        items = o.get("items") or []
        names = [it.get("name") for it in items if it.get("name")]
        menu = ", ".join(names) or None

        price = o.get("salePrice")
        if not isinstance(price, (int, float)):
            price = o.get("totalAmount")

        return {
            "platform": "coupang",
            "order_no": (o.get("abbrOrderId") or o.get("uniqueOrderId")
                         or (str(o.get("orderId")) if o.get("orderId") else None)),
            "status": o.get("status"),
            "ordered_at": ordered_at,
            "ordered_date": ordered_date,
            "menu": menu,
            "menus": names or None,
            "pay_type": None,
            "delivery_method": None,
            "ad_service": None,
            "price": int(price) if isinstance(price, (int, float)) else None,
            "raw": json.dumps(o, ensure_ascii=False),
        }


def fetch_reviews(days=2):
    with CoupangCrawler() as c:
        return c.fetch_reviews(days=days)


def fetch_orders(days=1):
    with CoupangCrawler() as c:
        return c.fetch_orders(days=days)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    with CoupangCrawler() as c:
        summ = c.fetch_sales_summary(days=7)
        print(f"쿠팡 매출요약(최근7일): {summ}")
        orders = c.fetch_orders(days=7)
        print(f"쿠팡 주문 {len(orders)}건:")
        for o in orders[:5]:
            print(f" - {o['order_no']} [{o['status']}] {o['price']:>7}원 "
                  f"{o['ordered_date']} {(o['menu'] or '')[:40]}")
        revs = c.fetch_reviews(days=30, max_pages=3)
        print(f"쿠팡 리뷰 {len(revs)}건:")
        for r in revs[:5]:
            print(f" - [{r['author']}] ★{r['rating']} "
                  f"{r['reply_status']} {(r['content'] or '(사진)')[:40]}")
