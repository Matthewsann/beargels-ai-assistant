"""알림 구멍 막기 — Notification Layer Phase 0 (2026-09-10).

왜: 알림함(_owner_alerts)은 error_log 의 kind 가 화이트리스트 6종일 때만 띄운다.
그런데 세 사건이 예외 클래스명(kind=type(e).__name__)으로 남아 이름이 안 맞았다:
  · 게시 중 세션 만료 → 'SessionExpiredError' (수집 중엔 'SessionExpired' 로 알림)
  · 답글 게시 실패     → 'ReplyPostError'   (8월 실측 50건, /todo 열기 전엔 아무도 몰랐다)
  · 장부 시트 실패     → 'LedgerSheetError' (6일 연속 실패를 /sales 열기 전엔 몰랐다)
그리고 /care 는 alerts 를 받고도 그리지 않았고, @cached 는 인자를 안 받아
_owner_alerts(limit=N) 이 TypeError 였다.

고친 방식: 화이트리스트에 예외 이름을 더 넣지 않는다(그 구조를 굳히지 않으려고).
대신 사건이 나는 자리에서 기존 사장님용 kind(SessionExpired / Notice)로 한 번 더
알린다. 기술 로그(error_log 의 예외 kind)는 그대로 — 새벽 점검·장부 경고가 읽는다.

DB·브라우저·AI 불필요.
"""
import importlib
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "service") not in sys.path:
    sys.path.insert(0, str(ROOT / "service"))


# ── 1. 게시 중 세션 만료 → 수집기와 같은 'SessionExpired' 알림 ──────────────

class _Page:
    """세션이 풀린 척하는 빈 페이지 — goto/대기는 아무 일도 안 한다."""
    def goto(self, *a, **k):
        pass

    def wait_for_selector(self, *a, **k):
        raise TimeoutError("로그인 화면이라 카드가 안 뜬다")


@pytest.fixture
def rr(monkeypatch):
    import crawler.review_reply as m
    told = []
    monkeypatch.setattr(m, "session_expired", lambda platform: told.append(platform))
    monkeypatch.setattr(m, "is_session_expired", lambda page: True)
    monkeypatch.setattr(m, "human_pause", lambda *a, **k: None)
    return m, told


def test_coupang_posting_session_expired_alerts_like_collector(rr):
    m, told = rr
    me = types.SimpleNamespace(review={"review_no": "123"})
    with pytest.raises(m.SessionExpiredError):
        m.ReplyToReviewAction._apply_coupang(me, _Page(), "감사합니다")
    assert told == ["쿠팡이츠"]


def test_baemin_posting_session_expired_alerts_like_collector(rr):
    m, told = rr
    me = types.SimpleNamespace(review={"review_no": "123"})
    with pytest.raises(m.SessionExpiredError):
        m.ReplyToReviewAction._apply_baemin(me, _Page(), "감사합니다")
    assert told == ["배민"]


# ── 2·3. 일꾼: 게시 실패·장부 실패가 사장님 알림(Notice)으로도 남는다 ───────

@pytest.fixture
def agent(monkeypatch):
    """DB·알림을 가짜로 바꾼 worker.agent."""
    import worker.agent as ag

    fake = types.SimpleNamespace(
        errors=[], jobs=[], drafted=[], pings=[],
        get_review=lambda rid: {"id": rid, "reply_status": "approved",
                                "platform": "baemin", "review_no": "r1",
                                "reply_draft": "고맙습니다"},
        log_error=lambda src, msg, **k: fake.errors.append((msg, k)),
        finish_job=lambda jid, st, msg, n=0: fake.jobs.append((jid, st, msg)),
        mark_drafted=lambda rid: fake.drafted.append(rid),
        worker_ping=lambda *a, **k: fake.pings.append(a),
    )
    monkeypatch.setattr(ag, "db", fake)
    told = []
    monkeypatch.setattr(ag, "notify_owner",
                        lambda text, kind="Notice", **k: told.append((kind, text)))
    monkeypatch.setattr(ag, "ensure_chrome", lambda *a, **k: True)
    return ag, fake, told


def _fail_posting_with(monkeypatch, exc):
    import crawler.review_reply as rrm

    class Boom:
        def __init__(self, *a, **k):
            pass

        def run(self, confirm=True):
            raise exc
    monkeypatch.setattr(rrm, "ReplyToReviewAction", Boom)


def test_reply_post_error_reaches_owner_inbox(agent, monkeypatch):
    ag, fake, told = agent
    from crawler.review_reply import ReplyPostError
    _fail_posting_with(monkeypatch, ReplyPostError("대상 배민 리뷰 카드를 찾지 못했습니다."))

    ag.run_post_job({"id": 9, "message": "42"})

    # 기술 로그는 예전 그대로(새벽 점검이 읽는다)
    assert fake.errors and fake.errors[0][1]["kind"] == "ReplyPostError"
    # 사장님 알림함용 Notice 가 새로 남는다
    assert told == [("Notice", told[0][1])]
    assert "리뷰 42" in told[0][1] and "카드를 찾지 못했습니다" in told[0][1]
    # 카드는 되돌아가고 잡은 error 로 끝난다 — 예전 동작 유지
    assert fake.drafted == [42]
    assert fake.jobs[-1][1] == "error" and fake.jobs[-1][2].startswith("리뷰 42 ")


def test_deadline_error_still_skips_quietly(agent, monkeypatch):
    """기한 만료는 예전처럼 '넘어가기'로 정리하고 알림은 안 낸다."""
    ag, fake, told = agent
    from crawler.review_reply import ReplyDeadlineError
    fake.mark_skipped = lambda rid: fake.drafted.append(("skipped", rid))
    _fail_posting_with(monkeypatch, ReplyDeadlineError("기한 지남"))

    ag.run_post_job({"id": 9, "message": "42"})

    assert told == []
    assert fake.jobs[-1][1] == "done"


def test_other_exceptions_do_not_spam_owner(agent, monkeypatch):
    """세션 만료(crawler 가 이미 알림)·타임아웃 같은 다른 예외는 알림함에 안 넣는다."""
    ag, fake, told = agent
    _fail_posting_with(monkeypatch, TimeoutError("느림"))

    ag.run_post_job({"id": 9, "message": "42"})

    assert told == []
    assert fake.errors[0][1]["kind"] == "TimeoutError"


def test_ledger_sync_failure_keeps_error_kind_and_tells_owner(agent, monkeypatch):
    ag, fake, told = agent
    import worker.ledger_sheet as ls
    monkeypatch.setattr(ls, "sync", lambda: (_ for _ in ()).throw(
        RuntimeError("인증 파일이 없습니다: token.json 먼저 python authorize_drive.py")))

    out = ag._sync_ledger_sheet()

    assert out.startswith("장부 시트 실패")
    # 대시보드 장부 경고(ledger_alert)가 읽는 kind 는 그대로
    assert fake.errors[0][1]["kind"] == "LedgerSheetError"
    # 알림함에는 원인·할 일이 사장님 말로
    assert len(told) == 1 and told[0][0] == "Notice"
    assert "구글 로그인이 막혀 있어요" in told[0][1]
    assert "csv" in told[0][1].lower() or "장부관리 폴더" in told[0][1]


# ── 4·5. 웹: /care 가 알림을 그린다 · @cached 가 인자를 받는다 ────────────────

KEY = "testkey"


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setenv("SERVICE_PATH", KEY)
    import service.app as m
    importlib.reload(m)
    m.app.testing = True
    return m


def test_cached_remembers_per_argument():
    from service.app import cached
    calls = []

    @cached(60)
    def f(n=5):
        calls.append(n)
        return list(range(n))

    assert f() == [0, 1, 2, 3, 4]
    assert f() == [0, 1, 2, 3, 4]                # 캐시 — 다시 안 부른다
    assert f(1) == [0]
    assert f(10) == list(range(10))
    assert f(n=10) == list(range(10))
    assert calls == [5, 1, 10, 10]               # 인자별로 한 번씩
    f.cache_clear()
    assert f() == [0, 1, 2, 3, 4] and calls[-1] == 5


def _fake_alert_rows(n):
    return [{"id": i, "kind": "Notice", "at": f"2026-09-10T0{i % 10}:00:00+00:00",
             "message": f"알림 {i}"} for i in range(1, n + 1)]


def test_owner_alerts_honours_limit(svc, monkeypatch):
    m = svc
    monkeypatch.setattr(m.db, "get_errors", lambda only_unfixed=True, limit=100: _fake_alert_rows(12))
    m._owner_alerts.cache_clear()
    assert len(m._owner_alerts()) == 5           # 기본값은 예전 그대로
    assert len(m._owner_alerts(limit=1)) == 1
    assert len(m._owner_alerts(limit=5)) == 5
    assert len(m._owner_alerts(limit=10)) == 10


def test_care_page_renders_alerts(svc, monkeypatch):
    m = svc
    monkeypatch.setattr(m.db, "get_attention_reviews", lambda **k: ([], 0))
    monkeypatch.setattr(m, "_tab_counts", lambda: {"todo": 0, "prob": 0, "hist": 0, "all": 0})
    monkeypatch.setattr(m.db, "get_errors", lambda only_unfixed=True, limit=100: [
        {"id": 7, "kind": "Notice", "at": "2026-09-10T01:00:00+00:00",
         "message": "장부 자동 반영 실패 — 구글 로그인이 막혀 있어요."}])
    m._owner_alerts.cache_clear()

    html = m.app.test_client().get(f"/{KEY}/care").get_data(as_text=True)

    assert "장부 자동 반영 실패" in html
    assert f"/{KEY}/alert/7/ack" in html          # [확인] 버튼이 붙는다
    assert 'class="alertbox"' in html
