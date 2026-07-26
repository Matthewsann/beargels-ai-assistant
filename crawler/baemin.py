"""
배달의민족(배민) 크롤러  (Playwright)

배민 사장님(셀프서비스) 사이트에서 주문/리뷰 데이터를 수집한다.
(향후 리뷰 답글·프로모션 등 쓰기 작업까지 확장 예정 — write_guard.py 참고)

세션 전략:
  브라우저 세션은 crawler/browser.py(BrowserSession)가 담당한다. 매 실행마다
  로그인 폼을 거치지 않고, launch_chrome.bat 로 띄운 로그인된 Chrome 에
  attach 하거나 저장된 프로필을 재사용한다(로그인 플로우가 봇 탐지의 핵심
  지점이므로). 세션 만료 시 SessionExpiredError + 텔레그램 재로그인 알림.

파싱 주의:
  배민은 CSS-module 해시 클래스(예: Table_b_r4ax_...)를 쓰는데 배포마다
  해시가 바뀌므로 셀렉터로 쓰지 않는다. 주문=단일 <table> 의 <tr>,
  리뷰=컴포넌트 접두사(ReviewContent-module__ 등) 부분일치로 잡는다.
  (실계정 DOM 으로 검증됨, 2026-07)
"""

import logging
import time
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from bot.notify import send_manual_login_alert
from crawler.browser import (
    BrowserSession, SessionExpiredError, human_pause, is_session_expired,
)

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

ORDERS_URL = "https://self.baemin.com/orders/history"
REVIEWS_URL = "https://self.baemin.com/shops/reviews"
PLATFORM = "배민"

# DOM 셀렉터 — 실계정 DOM 으로 검증됨.
# 배민은 CSS-module 해시 클래스를 쓰므로 안정적 시맨틱/접두사만 사용한다:
#   - 주문: 페이지에 <table> 이 하나뿐 → 그 안의 <tr>(9칸)을 행으로 사용
#   - 리뷰: 컴포넌트 접두사 'ReviewContent-module__' 는 안정적 → 부분일치
SELECTORS = {
    "order_table": "table",
    "review_item": '[class*="ReviewContent-module__"]',
}

# 주문 테이블 컬럼 순서(헤더 기준). 데이터 행은 9칸이며 0번은 펼치기 버튼.
ORDER_COLUMNS = [
    "_expand", "order_no", "ordered_at", "ad_service",
    "campaign_id", "menu", "pay_type", "delivery_method", "price",
]

# 리뷰 별점 SVG 채움색 — 금색이 채워진 별.
STAR_FILLED_COLOR = "#FFC600"

DEBUG_DIR = Path(__file__).resolve().parent.parent / "debug"
WAIT_TIMEOUT = 15   # 요소 대기 최대 시간(초)
MAX_RETRIES = 3     # 크롤링 함수 재시도 횟수


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------

def retry(times=MAX_RETRIES, delay=5):
    """일시적 오류(타임아웃 등) 재시도 데코레이터.

    SessionExpiredError 는 RuntimeError 라 여기서 잡히지 않고 즉시 전파된다
    (재로그인 전까지 재시도해도 소용없으므로).
    """

    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            last_err = None
            for attempt in range(1, times + 1):
                try:
                    return fn(*args, **kwargs)
                except (PlaywrightTimeout, PlaywrightError) as e:
                    last_err = e
                    logger.warning(
                        "%s 실패 (%d/%d회): %s", fn.__name__, attempt, times,
                        str(e)[:120])
                    time.sleep(delay)
            raise last_err

        return wrapper

    return deco


def _dump_debug(page, tag):
    """파싱 실패 시 스크린샷과 HTML 을 debug/ 에 저장한다."""
    DEBUG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    png = DEBUG_DIR / f"{tag}-{stamp}.png"
    html = DEBUG_DIR / f"{tag}-{stamp}.html"
    try:
        page.screenshot(path=str(png))
        html.write_text(page.content(), encoding="utf-8")
        logger.info("디버그 덤프 저장: %s, %s", png, html)
    except PlaywrightError:
        logger.exception("디버그 덤프 저장 실패")


# ---------------------------------------------------------------------------
# 크롤러
# ---------------------------------------------------------------------------

class BaeminCrawler:
    """배민 사장님 사이트 크롤러 (컨텍스트 매니저로 사용 권장)."""

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

    # -- 세션 확인 ----------------------------------------------------------

    def _open_authed(self, url):
        """인증 필요 URL 로 이동하되 세션 만료면 예외+알림.

        로그인 폼을 자동으로 채우지 않는다(봇 탐지 지점 회피). 세션이 없으면
        수동 재로그인을 요청한다.
        """
        self.page.goto(url, wait_until="domcontentloaded")
        human_pause()
        if is_session_expired(self.page):
            send_manual_login_alert(PLATFORM)
            raise SessionExpiredError(
                f"[{PLATFORM}] 로그인 세션이 만료되었습니다. "
                f"launch_chrome.bat 로 띄운 Chrome 에서 다시 로그인하세요.")

    # -- 주문 ---------------------------------------------------------------

    @retry()
    def fetch_orders(self, start_date=None, end_date=None):
        """주문 내역을 크롤링해 dict 리스트로 반환한다.

        Args:
            start_date: 조회 시작일 (기본: 어제)
            end_date:   조회 종료일 (기본: 오늘)
        """
        start_date = start_date or (date.today() - timedelta(days=1))
        end_date = end_date or date.today()

        url = (f"{ORDERS_URL}?startDate={start_date:%Y-%m-%d}"
               f"&endDate={end_date:%Y-%m-%d}")
        self._open_authed(url)
        try:
            self.page.wait_for_selector(
                SELECTORS["order_table"], timeout=WAIT_TIMEOUT * 1000)
        except PlaywrightTimeout:
            _dump_debug(self.page, "orders-empty")
            logger.info("주문 테이블을 찾지 못함 (주문 없음이거나 셀렉터 확인 필요)")
            return []
        human_pause(1.5, 2.5)  # 표 렌더링 안정화

        soup = BeautifulSoup(self.page.content(), "html.parser")
        table = soup.select_one(SELECTORS["order_table"])
        rows = table.find_all("tr") if table else []
        orders = [self._parse_order_row(r) for r in rows]
        orders = [o for o in orders if o]
        logger.info("주문 %d건 수집 (%s ~ %s)", len(orders), start_date, end_date)
        return orders

    @staticmethod
    def _parse_order_row(row):
        """주문 테이블의 <tr> 하나를 dict 로 정규화한다.

        데이터 행은 9칸(펼치기/주문번호/주문시각/광고·서비스/캠페인ID/
        주문내역/결제타입/수령방법/결제금액). 헤더 행·펼침 상세 행(칸 수가
        다름)은 None 을 반환해 걸러진다.
        """
        import re
        cells = row.find_all("td")
        if len(cells) != len(ORDER_COLUMNS):
            return None  # 헤더(th) 또는 펼침 상세(1칸) 행

        vals = {col: cells[i].get_text(" ", strip=True)
                for i, col in enumerate(ORDER_COLUMNS)}

        # "배달완료 T2ES0000KNLJ" → status + order_no 분리
        status, order_no = None, None
        raw_no = vals["order_no"]
        if raw_no:
            parts = raw_no.split()
            status = parts[0] if parts else None
            order_no = parts[-1] if len(parts) > 1 else None
        if not order_no:
            return None

        # 금액 "15,200원" → 15200 (int)
        price = None
        m = re.search(r"([\d,]+)\s*원", vals["price"])
        if m:
            price = int(m.group(1).replace(",", ""))

        return {
            "platform": "baemin",
            "order_no": order_no,
            "status": status,
            "ordered_at": vals["ordered_at"] or None,
            "menu": vals["menu"] or None,
            "pay_type": vals["pay_type"] or None,
            "delivery_method": vals["delivery_method"] or None,
            "ad_service": vals["ad_service"] or None,
            "price": price,
            "raw": row.get_text(" ", strip=True),
        }

    # -- 리뷰 ---------------------------------------------------------------

    @retry()
    def fetch_reviews(self, max_scroll=5):
        """신규 리뷰를 크롤링해 dict 리스트로 반환한다.

        리뷰는 스크롤 시 지연 로딩되므로 max_scroll 회까지 아래로 스크롤해
        추가 리뷰를 불러온다.
        """
        self._open_authed(REVIEWS_URL)
        try:
            self.page.wait_for_selector(
                SELECTORS["review_item"], timeout=WAIT_TIMEOUT * 1000)
        except PlaywrightTimeout:
            _dump_debug(self.page, "reviews-empty")
            logger.info("리뷰 항목을 찾지 못함 (리뷰 없음이거나 셀렉터 확인 필요)")
            return []

        # 지연 로딩: 카드 수가 늘지 않을 때까지 아래로 스크롤
        prev = 0
        for _ in range(max_scroll):
            self.page.evaluate(
                "window.scrollTo(0, document.body.scrollHeight)")
            human_pause(2.0, 3.5)
            cur = len(self.page.query_selector_all(SELECTORS["review_item"]))
            if cur == prev:
                break
            prev = cur

        soup = BeautifulSoup(self.page.content(), "html.parser")
        items = soup.select(SELECTORS["review_item"])
        reviews = [self._parse_review_item(i) for i in items]
        reviews = [r for r in reviews if r]
        logger.info("리뷰 %d건 수집", len(reviews))
        return reviews

    @staticmethod
    def _parse_review_item(item):
        """리뷰 카드 하나를 dict 로 정규화한다. 파싱 불가 시 None.

        카드 텍스트 순서(실 DOM 검증):
          배달유형 | 작성자 | 날짜 | 리뷰번호 N | N회 주문 고객 |
          (누적주문) | 리뷰본문... | 주문메뉴 | 메뉴들... | 배달리뷰 | ...
        별점은 금색(#FFC600) 별 SVG 개수로 산출한다.
        """
        import re
        raw = item.get_text(" ", strip=True)
        if not raw:
            return None

        # 잎(leaf) 텍스트를 순서대로 수집
        parts = [el.get_text(" ", strip=True)
                 for el in item.find_all(True)
                 if not el.find(True) and el.get_text(strip=True)]

        # 잎 순서: [배달유형?, 닉네임, 날짜, 리뷰번호 N, ...]. 배달유형 badge 는
        # 있을 때/없을 때가 있어 위치가 밀리므로 고정 인덱스로 잡으면 닉네임 대신
        # 날짜가 잡힌다. → 닉네임 = '날짜 잎' 바로 앞 잎 으로 안정적으로 잡는다.
        _date_leaf = re.compile(r"^\d{4}년\s*\d{1,2}월\s*\d{1,2}일$")
        date_idx = next((i for i, x in enumerate(parts)
                         if _date_leaf.match(x)), None)
        if date_idx is not None and date_idx >= 1:
            author = parts[date_idx - 1]
            delivery_type = parts[0] if date_idx >= 2 else None
        else:
            delivery_type = parts[0] if parts else None
            author = parts[1] if len(parts) > 1 else None

        written_at = None
        m = re.search(r"\d{4}년\s*\d{1,2}월\s*\d{1,2}일", raw)
        if m:
            written_at = m.group(0)

        review_no = None
        m = re.search(r"리뷰번호\s*([0-9]+)", raw)
        if m:
            review_no = m.group(1)

        # 별점: 금색으로 채워진 별 SVG 개수 (0~5)
        rating = None
        svgs = item.find_all("svg")
        if svgs:
            filled = 0
            for s in svgs:
                fills = {s.get("fill", "")}
                fills |= {p.get("fill", "")
                          for p in s.find_all(["path", "use", "polygon"])}
                if STAR_FILLED_COLOR in fills:
                    filled += 1
            rating = filled if filled else None

        # 주문 메뉴: '주문메뉴' 라벨을 포함한 ReviewMenus 컨테이너에서 추출.
        # ⚠️ 배민은 고객이 선택한 리뷰 키워드 칩(예: "대박맛집입니다")을 실제
        #    메뉴와 동일한 MenuItem 컴포넌트로 이 컨테이너 안에 섞어 렌더링한다.
        #    구조만으로는 완벽 분리가 불가하므로 menus 에 칩이 섞일 수 있다
        #    (원문은 raw 에 보존됨).
        menus = []
        for box in item.select('[class*="ReviewMenus-module__"]'):
            if not box.get_text(strip=True).startswith("주문메뉴"):
                continue
            for li in box.select('li[class*="MenuItem-module__"]'):
                t = li.get_text(" ", strip=True)
                if t and t != "주문메뉴" and t not in menus:
                    menus.append(t)
            break

        # 리뷰 본문: 누적주문 안내 라벨 이후 ~ 주문메뉴 라벨 이전 텍스트
        content = None
        marker = "(최근 6개월 누적 주문)"
        if marker in parts:
            i = parts.index(marker) + 1
            end = parts.index("주문메뉴") if "주문메뉴" in parts else len(parts)
            body = [p for p in parts[i:end] if p]
            content = " ".join(body).strip() or None

        # 답글 여부: 미답변 카드에만 '사장님 댓글 등록하기' 버튼이 있다
        # (review_reply.py 의 실게시 경로에서 검증된 문구, 2026-07-24).
        # 버튼이 없으면 이미 답글이 달린 것으로 본다.
        reply_status = "none" if "사장님 댓글 등록하기" in raw else "posted"

        return {
            "platform": "baemin",
            "author": author,
            "rating": rating,
            "content": content,
            "written_at": written_at,
            "review_no": review_no,
            "menus": menus or None,
            "delivery_type": delivery_type,
            "reply_status": reply_status,
            "raw": raw,
        }


# ---------------------------------------------------------------------------
# 모듈 레벨 API
# ---------------------------------------------------------------------------

def fetch_orders(start_date=None, end_date=None):
    """배민 주문 내역을 크롤링해 반환한다."""
    with BaeminCrawler() as crawler:
        return crawler.fetch_orders(start_date, end_date)


def fetch_reviews():
    """배민 신규 리뷰를 크롤링해 반환한다."""
    with BaeminCrawler() as crawler:
        return crawler.fetch_reviews()


if __name__ == "__main__":
    # 단독 실행: attach 모드는 먼저 launch_chrome.bat 로 로그인된 Chrome 을 켜둔다.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    with BaeminCrawler() as crawler:
        orders = crawler.fetch_orders()
        print(f"주문 {len(orders)}건:")
        for o in orders[:5]:
            print(" -", o["raw"][:80])
        reviews = crawler.fetch_reviews()
        print(f"리뷰 {len(reviews)}건:")
        for r in reviews[:5]:
            body = r["content"] or "(본문 없음)"
            print(f" - [{r['author']}] ★{r['rating']} {body[:60]}")
