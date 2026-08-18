"""크롤러가 잘못 연 탭이 쌓이지 않는지 — 회귀 테스트.

배민 리뷰 목록의 '더보기' 옆에 도움말 쪽 '더보기'가 있어서, 잘못 누르면
ceo.baemin.com/qna 가 **새 탭**으로 열린다. 같은 탭 이동이 아니라 기존
URL 가드에 걸리지 않아 조용히 쌓였고, 사장님 Chrome 에 탭이 76개까지
열렸다(2026-08-18). 다시는 쌓이면 안 된다.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crawler import browser as br  # noqa: E402


class _Page:
    def __init__(self, url, ctx=None):
        self.url, self.closed, self._ctx = url, False, ctx

    def close(self):
        self.closed = True
        if self._ctx:
            self._ctx.pages = [p for p in self._ctx.pages if p is not self]

    def wait_for_load_state(self, *a, **k):
        pass


class _Ctx:
    def __init__(self, urls):
        self.pages = []
        self.pages = [_Page(u, self) for u in urls]


def test_stray_urls():
    assert br.is_stray_url("https://ceo.baemin.com/qna?inflowService=selfservice")
    assert not br.is_stray_url("https://self.baemin.com/shops/reviews")
    assert not br.is_stray_url("https://store.coupangeats.com/merchant/reviews")


def test_close_stray_tabs_only_touches_junk():
    ctx = _Ctx(["https://self.baemin.com/shops/reviews",
                "https://ceo.baemin.com/qna?x=1",
                "https://ceo.baemin.com/qna?x=2",
                "https://store.coupangeats.com/merchant/management/home/889230"])
    assert br.close_stray_tabs(ctx) == 2
    assert [p.url for p in ctx.pages] == [
        "https://self.baemin.com/shops/reviews",
        "https://store.coupangeats.com/merchant/management/home/889230"]


def test_never_closes_the_last_tab():
    """탭이 0개가 되면 Chrome 창이 닫혀 세션(로그인)이 날아간다."""
    ctx = _Ctx(["https://ceo.baemin.com/qna?x=1"])
    assert br.close_stray_tabs(ctx) == 0
    assert len(ctx.pages) == 1


def test_popup_is_closed_and_counted():
    before = br.stray_tabs_closed()
    popup = _Page("https://ceo.baemin.com/qna?inflowService=selfservice")
    br.BrowserSession._close_popup(popup)
    assert popup.closed
    assert br.stray_tabs_closed() == before + 1


def test_our_own_tab_is_kept():
    popup = _Page("https://self.baemin.com/shops/reviews")
    br.BrowserSession._close_popup(popup)
    assert not popup.closed
