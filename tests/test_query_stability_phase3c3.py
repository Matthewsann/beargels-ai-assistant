"""Phase 3-C-3 — 프로덕션 조회 안정성 (2026-09-12).

증거(PythonAnywhere error.log 09-07~12): httpx.ReadError "[Errno 11] Resource temporarily
unavailable" 42건, 전부 httpcore _sync/http2.py 경유. 한 순간에 서로 다른 원천 3개가 같이
실패 — HTTP/2 연결 하나를 스레드들이 다중화하다 읽기 오류가 나면 그 연결의 스트림이
한꺼번에 죽는다. 한 화면(홈)이 조회 15건을 동시에 보냈고 그중 업무·민감 리뷰 건수는 두 번씩.

계약:
  · get_client 는 postgrest 세션을 HTTP/1.1 로 바꾼다(주소·헤더·타임아웃 유지). 실패하면 예전대로.
  · run_query: 일시적 전송 오류(httpx.TransportError, 시간초과 제외)만 1회 재시도, 로그에
    원천·예외 클래스·메시지. 서버 거절(APIError)·코드 오류·시간초과는 재시도 없음.
  · 실패는 여전히 실패(3-C-2 유지): 빈 결과와 구분, 판단은 '없음'으로 안 본다.
  · 홈은 같은 원천을 한 번만 읽는다(업무·요청·민감 리뷰 건수는 판단 report 재사용).
  · '오늘 이것부터'는 업무 표 실패를 숨기지 않는다.
DB·네트워크 불필요.
"""
import importlib
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tests", ROOT / "service"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from database import supabase_client as db  # noqa: E402
from database import work_store as wk  # noqa: E402
from service import operations_judgment as oj  # noqa: E402
from test_operations_judgment import _req, _task  # noqa: E402
from test_operations_judgment_reliability import _Client, _row  # noqa: E402

REF = date(2026, 9, 12)


def _read_error():
    return httpx.ReadError("[Errno 11] Resource temporarily unavailable")


class _Flaky:
    """처음 n 번은 터지고 그다음부터 답한다 — 호출 횟수를 센다."""
    def __init__(self, fails, exc, value="ok"):
        self.fails, self.exc, self.value, self.calls = fails, exc, value, 0

    def __call__(self):
        self.calls += 1
        if self.calls <= self.fails:
            raise self.exc
        return self.value


# ── run_query: 1·2·3·4·5 ─────────────────────────────────────────────────────

def test_1_정상_조회는_한_번만_부른다():
    f = _Flaky(0, None, value=[{"id": 1}])
    assert db.run_query("t", f) == [{"id": 1}] and f.calls == 1


def test_2_빈_결과는_성공이다():
    f = _Flaky(0, None, value=[])
    assert db.run_query("t", f) == [] and f.calls == 1


def test_3_일시_오류_뒤_성공은_한_번_재시도로_살린다(caplog):
    f = _Flaky(1, _read_error(), value=[{"id": 1}])
    with caplog.at_level(logging.WARNING, logger="database.supabase_client"):
        assert db.run_query("reviews 건수", f) == [{"id": 1}]
    assert f.calls == 2
    assert any("ReadError" in r.getMessage() and "reviews 건수" in r.getMessage()
               and "재시도" in r.getMessage() for r in caplog.records)   # 원천·클래스·메시지


def test_4_계속_실패하면_두_번만_부르고_예외를_올린다(caplog):
    f = _Flaky(5, _read_error())
    with caplog.at_level(logging.WARNING, logger="database.supabase_client"):
        with pytest.raises(httpx.ReadError):
            db.run_query("work_tasks", f)
    assert f.calls == 2                                       # 최대 1회 재시도
    assert any(r.levelno == logging.ERROR and "재시도도 실패" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("exc", [
    KeyError("boom"), TypeError("x"), ValueError("v"),
    httpx.ReadTimeout("timed out"), httpx.ConnectTimeout("t"),
])
def test_5_코드_오류와_시간초과는_재시도하지_않는다(exc):
    f = _Flaky(5, exc)
    with pytest.raises(type(exc)):
        db.run_query("t", f)
    assert f.calls == 1


def test_5b_APIError_서버_거절은_재시도하지_않는다():
    from postgrest.exceptions import APIError
    f = _Flaky(5, APIError({"message": "Could not find the table", "code": "PGRST205",
                            "hint": None, "details": None}))
    with pytest.raises(APIError):
        db.run_query("t", f)
    assert f.calls == 1


# ── 6·8: 실패는 빈 것과 다르다 — 원천 함수 경유 ───────────────────────────────

def test_6_ReadError_두_번이면_실패로_남는다(monkeypatch):
    monkeypatch.setattr(db, "get_client", lambda: _Client(boom=_read_error()))
    assert db.count_pending(with_draft=True, escalate=True) == 0        # 예전 호출: 삼킴
    with pytest.raises(httpx.ReadError):
        db.count_pending(with_draft=True, escalate=True, strict=True)   # strict: 실패는 실패
    with pytest.raises(httpx.ReadError):
        db.search_reviews(days=30, limit=300, sort="new", strict=True)
    with pytest.raises(httpx.ReadError):
        db.get_attention_reviews(replied=False, limit=1, select="id", strict=True)


def test_6b_ReadError_한_번은_재시도로_성공한다(monkeypatch):
    """같은 질의 객체를 다시 보내는 게 재시도다 — 첫 전송만 터지고 두 번째는 답한다."""
    from test_operations_judgment_reliability import _Q, _R
    class _QOnce(_Q):
        def __init__(self):
            super().__init__(data=[{"id": 1}], count=4)
            self.n = 0
        def execute(self):
            self.n += 1
            if self.n == 1:
                raise _read_error()
            return _R(self.d, self.c)
    c = _Client()
    c.q = _QOnce()
    monkeypatch.setattr(db, "get_client", lambda: c)
    assert db.count_pending(with_draft=True, escalate=True, strict=True) == 4
    assert c.q.n == 2


def test_6c_업무_표_ReadError_두_번이면_실패_이름이_남는다(monkeypatch):
    from database import meeting_store as mt
    monkeypatch.setattr(wk, "get_client", lambda: _Client(boom=_read_error()))
    monkeypatch.setattr(wk, "subtasks_ready", lambda: False)
    monkeypatch.setattr(wk, "today", lambda: REF)
    monkeypatch.setattr(mt, "open_tasks", lambda limit=20: [])
    assert wk.open_tasks_checked() == ([], ["업무"])


def test_6d_업무_표_ReadError_한_번은_재시도로_살린다(monkeypatch):
    from database import meeting_store as mt
    calls = {"n": 0}
    def client():
        calls["n"] += 1
        return _Client(boom=_read_error()) if calls["n"] == 1 else _Client([_row(39, REF - timedelta(days=2))])
    monkeypatch.setattr(wk, "get_client", client)
    monkeypatch.setattr(wk, "subtasks_ready", lambda: False)
    monkeypatch.setattr(wk, "today", lambda: REF)
    monkeypatch.setattr(mt, "open_tasks", lambda limit=20: [])
    tasks, failed = wk.open_tasks_checked()
    assert [t["id"] for t in tasks] == ["w:39"] and failed == []


def test_8_판단은_실패_원천을_없음으로_보지_않는다():
    rep = oj.judge_sources({
        "tasks": oj.source("tasks", None, ok=False, error="gather"),
        "requests": oj.source("requests", []),
        "escalate": oj.source("escalate", 0), "attention": oj.source("attention", 0)}, today=REF)
    assert rep["judgments"] == [] and rep["complete"] is False and rep["warning"]


# ── HTTP/1.1 전환 ────────────────────────────────────────────────────────────

def test_http1_전환은_주소_헤더_타임아웃을_그대로_옮긴다():
    class _PG:
        session = httpx.Client(base_url="https://x.supabase.co/rest/v1",
                               headers={"apikey": "k", "Authorization": "Bearer k"},
                               timeout=20, http2=False)
    class _C:
        postgrest = _PG()
    old = _C.postgrest.session
    db._use_http1(_C)
    new = _C.postgrest.session
    assert new is not old
    assert str(new.base_url) == str(old.base_url)
    assert new.headers["apikey"] == "k" and new.headers["authorization"] == "Bearer k"
    assert new.timeout == old.timeout
    # HTTP/2 가 꺼져 있다 — httpx 는 http2 여부를 transport 에 둔다
    assert getattr(new._transport, "_pool", None) is not None and new._transport._pool._http2 is False


def test_http1_전환_실패는_예전_클라이언트를_망가뜨리지_않는다():
    class _C:
        postgrest = None                       # .session 접근에서 AttributeError
    db._use_http1(_C)                          # 예외가 밖으로 안 나온다


# ── 7·9·10·11: 홈 ────────────────────────────────────────────────────────────

KEY = "testkey"


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setenv("SERVICE_PATH", KEY)
    import service.app as m
    importlib.reload(m)
    m.app.testing = True
    monkeypatch.setattr(m, "_owner_alerts", lambda: [])
    monkeypatch.setattr(m, "_notif_alerts", lambda: [])
    for name in ("meet_tasks", "_briefs_cached"):
        if hasattr(m, name):
            monkeypatch.setattr(m, name, lambda *a, **k: [])
    return m


def _gather_stub(**calls):
    out = {k: None for k in calls}
    for k in ("inbox", "meet_tasks", "briefs", "judgments"):
        if k in calls:
            out[k] = calls[k]()
    if "todo_baemin" in calls:
        out["todo_baemin"] = 1          # 답글 카드가 칩(🚨 직접 대응)을 그리게
    return out


def _report(m, tasks_src, requests_src, escalate_src, attention_src):
    """_judgments_cached 가 만드는 모양 그대로."""
    src = {"tasks": tasks_src, "requests": requests_src,
           "escalate": escalate_src, "attention": attention_src}
    rep = oj.judge_sources(src, today=REF)
    rep["tasks"] = src["tasks"]["data"] or []
    rep["tasks_failed"] = not src["tasks"]["ok"]
    rep["requests"] = src["requests"]["data"] or []
    rep["escalate"] = src["escalate"]["data"]
    return rep


def test_7_원천_하나가_죽어도_홈은_200(svc, monkeypatch):
    m = svc
    monkeypatch.setattr(m, "gather", _gather_stub)
    monkeypatch.setattr(m, "_judgments_cached", lambda: _report(
        m, oj.source("tasks", None, ok=False, error="gather"),
        oj.source("requests", [_req(1734, 4)]), oj.source("escalate", 0), oj.source("attention", 0)))
    r = m.app.test_client().get(f"/{KEY}/")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and 'class="jwarn"' in html and "review#requests" in html


def test_9_오늘_이것부터는_업무_실패를_숨기지_않는다(svc, monkeypatch):
    m = svc
    monkeypatch.setattr(m, "gather", _gather_stub)
    monkeypatch.setattr(m, "_judgments_cached", lambda: _report(
        m, oj.source("tasks", None, ok=False, error="gather"),
        oj.source("requests", []), oj.source("escalate", 0), oj.source("attention", 0)))
    html = m.app.test_client().get(f"/{KEY}/").get_data(as_text=True)
    assert 'class="mtask mtwarn"' in html and "업무 데이터를 불러오지 못했어요" in html


def test_9b_업무가_정말_없으면_카드도_경고도_없다(svc, monkeypatch):
    m = svc
    monkeypatch.setattr(m, "gather", _gather_stub)
    monkeypatch.setattr(m, "_judgments_cached", lambda: _report(
        m, oj.source("tasks", []), oj.source("requests", []),
        oj.source("escalate", 0), oj.source("attention", 0)))
    html = m.app.test_client().get(f"/{KEY}/").get_data(as_text=True)
    assert 'class="mtasks"' not in html and 'class="mtask mtwarn"' not in html and 'class="jwarn"' not in html


def test_9c_오늘_이것부터는_판단과_같은_업무로_같은_규칙을_쓴다(svc, monkeypatch):
    m = svc
    monkeypatch.setattr(m, "gather", _gather_stub)
    tasks = [_task(39, "냉장고 수리", due=REF - timedelta(days=2), owner="민수"),
             _task(40, "여유 업무", due=REF + timedelta(days=20), owner="민수")]
    monkeypatch.setattr(m, "_judgments_cached", lambda: _report(
        m, oj.source("tasks", tasks), oj.source("requests", [_req(1, 1), _req(2, 1)]),
        oj.source("escalate", 2), oj.source("attention", 0)))
    html = m.app.test_client().get(f"/{KEY}/").get_data(as_text=True)
    assert "냉장고 수리" in html and "여유 업무" not in html         # top_priorities: 여유는 제외
    assert "🚨 사장님 직접 대응 2건" in html                        # escalate 는 report 에서
    assert "전파할 고객 요청 2건" in html                            # req_n 도 report 에서


def test_10_홈은_업무_요청_민감건수를_한_번만_읽는다(svc, monkeypatch):
    """gather 를 진짜로 돌리되 원천은 가짜 — 호출 횟수를 센다."""
    m = svc
    calls = {"tasks": 0, "requests": 0, "escalate": 0, "attention": 0}
    def tasks():
        calls["tasks"] += 1
        return [_task(39, "냉장고", due=REF - timedelta(days=2))], []
    def requests(limit=20):
        calls["requests"] += 1
        return [_req(1, 4)], 0, ""
    def count_pending(**k):
        if k.get("escalate"):
            calls["escalate"] += 1
        return 1
    def attention(**k):
        calls["attention"] += 1
        return [], 0
    monkeypatch.setattr(wk, "open_tasks_checked", tasks)
    monkeypatch.setattr(m, "_customer_requests_checked", requests)
    monkeypatch.setattr(m, "_customer_requests", lambda limit=20: requests(limit)[:2])
    monkeypatch.setattr(m.db, "count_pending", count_pending)   # todo 배지도 1건씩
    monkeypatch.setattr(m.db, "get_attention_reviews", attention)
    for name in ("oldest_pending_date", "last_collect_at"):
        monkeypatch.setattr(m.db, name, lambda *a, **k: None)
    monkeypatch.setattr(m.db, "get_setting", lambda *a, **k: {})
    monkeypatch.setattr(m.blog, "count_posts", lambda *a, **k: 0)
    monkeypatch.setattr(m, "_learning_cached", lambda: None)
    monkeypatch.setattr(m.mt, "open_task_count", lambda: 0)
    m._judgments_cached.cache_clear()
    html = m.app.test_client().get(f"/{KEY}/").get_data(as_text=True)
    assert calls == {"tasks": 1, "requests": 1, "escalate": 1, "attention": 1}
    assert "냉장고" in html and 'data-key="work.overdue:w:39"' in html
    assert "🚨 사장님 직접 대응 1건" in html and "전파할 고객 요청 1건" in html


def test_11_전부_건강하면_판단은_3C1과_같다():
    tasks = [_task(39, "냉장고", due=REF - timedelta(days=2)), _task(4, "담당 없음"),
             _task(3, "묵은 것", owner="a", created=REF - timedelta(days=10))]
    reqs = [_req(10, 5)]
    rep = oj.judge_sources({
        "tasks": oj.source("tasks", tasks), "requests": oj.source("requests", reqs),
        "escalate": oj.source("escalate", 1), "attention": oj.source("attention", 2)}, today=REF)
    assert rep["judgments"] == oj.get_daily_judgments(
        tasks=tasks, requests=reqs, review_counts={"escalate": 1, "attention": 2}, today=REF)
    assert rep["failed"] == [] and rep["warning"] == ""
