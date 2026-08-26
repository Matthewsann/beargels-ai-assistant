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


# --- 먹통 크롬 자가복구 -------------------------------------------------------
# CDP 포트는 HTTP 응답을 계속 주면서 정작 붙지는 못하는 '반쯤 죽은' 상태가
# 된다. cdp_alive() 가 HTTP 만 보다가 수집이 몇 시간째 조용히 실패했다
# (2026-08-18, 작업 372·373·374 연속 error).

def test_attach_failure_is_recognized():
    from worker import agent
    err = RuntimeError("CDP attach 실패(127.0.0.1:9222). ... connect_over_cdp: "
                       "Timeout 180000ms exceeded.")
    assert agent._is_attach_failure(err)
    assert not agent._is_attach_failure(RuntimeError("로그인 세션이 만료되었습니다"))


def test_chrome_autorestart_can_be_turned_off(monkeypatch):
    from worker import agent
    monkeypatch.setenv("WORKER_CHROME_AUTORESTART", "false")
    monkeypatch.setattr(agent, "_profile_chrome_pids",
                        lambda: (_ for _ in ()).throw(AssertionError("꺼야 한다")))
    assert agent.restart_chrome("test") is False


def test_restart_chrome_only_kills_our_profile(monkeypatch):
    """사장님이 평소 쓰는 Chrome 은 절대 건드리면 안 된다."""
    from worker import agent
    import inspect
    src = inspect.getsource(agent._profile_chrome_pids)
    assert ".browser_profile" in src        # 프로필 경로로 걸러서 고른다


# --- 도중에 끊긴 브라우저는 코드가 한 번 더 시도한다 (2026-08-25) ----------
# 실제: 답글 등록 #557 이 "Target page, context or browser has been closed"로
# 실패했는데 직원이 다시 누른 #558 은 4초 만에 성공했다. 사람에게 다시
# 누르게 하지 않는다.

def test_browser_gone_is_recognized():
    from worker import agent
    for msg in ("Page.query_selector: Target page, context or browser has been closed",
                "Target closed", "browser has been closed"):
        assert agent._is_browser_gone(RuntimeError(msg)), msg


def test_other_errors_are_not_retried_as_browser_gone():
    from worker import agent
    assert not agent._is_browser_gone(RuntimeError("로그인 세션이 만료되었습니다"))
    assert not agent._is_browser_gone(RuntimeError("답글 기한이 지났습니다"))


def test_post_job_retries_once_on_browser_gone():
    """등록 경로에 재시도가 실제로 들어 있는지(코드 계약)."""
    import inspect
    from worker import agent
    src = inspect.getsource(agent.run_post_job)
    assert "_is_browser_gone" in src and "res = _post()" in src


def test_worker_does_not_idle_between_jobs():
    """큐에 일이 남아 있으면 쉬지 않고 바로 다음 건을 집는다.

    예전엔 한 건 끝낼 때마다 15초를 쉬어서, 27건 재생성에서 전체 시간의
    43%가 대기였다(2026-08-26).
    """
    import inspect
    from worker import agent
    src = inspect.getsource(agent.main)
    assert "if not busy:" in src and "time.sleep(POLL_SECONDS)" in src
