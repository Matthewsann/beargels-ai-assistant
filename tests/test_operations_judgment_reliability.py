"""Phase 3-C-2 — 운영 판단 신뢰성: '없음'과 '못 읽음'을 구분한다 (2026-09-12).

원인: 판단 원천 함수들(work_store._work_rows/_meeting_rows, supabase_client._count·
get_attention_reviews·search_reviews, app._customer_requests)이 조회 실패를 삼키고
빈 목록·0 을 준다. 판단 엔진이 그걸 믿으면 "할 일 없음"이라는 거짓 판단이 된다.

계약:
  · 기본 호출은 예전 그대로(빈 결과). strict=True / *_checked 만 실패를 드러낸다.
  · judge_sources 는 실패한 원천이 하나라도 있으면 warning 을 붙이고, 건강한 원천의
    판단은 그대로 낸다. 실패 때문에 알림·업무를 만들지 않는다.
  · 홈은 원천이 죽어도 200 이고, 경고 한 줄이 보인다.
DB·네트워크 불필요.
"""
import importlib
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tests", ROOT / "service"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from database import supabase_client as db  # noqa: E402
from database import work_store as wk  # noqa: E402
from service import operations_judgment as oj  # noqa: E402
from test_operations_judgment import _req, _task  # noqa: E402

REF = date(2026, 9, 12)


# ── 가짜 supabase — 아무 체인이나 받고 execute 에서 data 를 주거나 터진다 ─────────

class _R:
    def __init__(self, data, count=None):
        self.data, self.count = data, count


class _Q:
    def __init__(self, data=None, count=None, boom=None):
        self.d, self.c, self.boom = data, count, boom

    def __getattr__(self, name):           # select/eq/or_/in_/order/limit/range …
        if name == "not_":                 # postgrest 의 not_ 은 메서드가 아니라 속성
            return self
        return lambda *a, **k: self

    def execute(self):
        if self.boom:
            raise self.boom
        return _R(self.d, self.c)


class _Client:
    def __init__(self, data=None, count=None, boom=None):
        self.q = _Q(data, count, boom)

    def table(self, _name):
        return self.q


def _row(i, due=None, owner="", done=False):
    return {"id": i, "content": f"업무 {i}", "owner": owner, "done": done,
            "due_date": due.isoformat() if due else None,
            "created_at": (REF - timedelta(days=1)).isoformat(), "parent_id": None}


@pytest.fixture
def quiet_meeting(monkeypatch):
    """회의 할 일 원천은 비어 있고 건강하다 — 업무 표만 본다."""
    from database import meeting_store as mt
    monkeypatch.setattr(mt, "open_tasks", lambda limit=20: [])
    monkeypatch.setattr(wk, "subtasks_ready", lambda: False)
    monkeypatch.setattr(wk, "today", lambda: REF)


# ── 1·2·3: 원천 하나의 세 상태 ────────────────────────────────────────────────

def test_1_건강한_원천_데이터_있음(monkeypatch, quiet_meeting):
    monkeypatch.setattr(wk, "get_client", lambda: _Client([_row(39, REF - timedelta(days=2))]))
    tasks, failed = wk.open_tasks_checked()
    assert failed == []
    assert [t["id"] for t in tasks] == ["w:39"] and tasks[0]["pri"]["rank"] == 0


def test_2_건강한_원천_비어_있음은_정상(monkeypatch, quiet_meeting):
    monkeypatch.setattr(wk, "get_client", lambda: _Client([]))
    assert wk.open_tasks_checked() == ([], [])
    rep = oj.judge_sources({"tasks": oj.source("tasks", []), "requests": oj.source("requests", []),
                            "escalate": oj.source("escalate", 0),
                            "attention": oj.source("attention", 0)}, today=REF)
    assert rep == {"judgments": [], "failed": [], "warning": "", "complete": True}


def test_3_실패한_원천은_실패로_읽힌다(monkeypatch, quiet_meeting):
    monkeypatch.setattr(wk, "get_client", lambda: _Client(boom=RuntimeError("db down")))
    tasks, failed = wk.open_tasks_checked()
    assert tasks == [] and failed == ["업무"]
    assert wk.open_tasks() == []                       # 예전 호출은 예전 그대로(삼킴)


def test_3b_회의_할_일만_실패하면_업무는_살고_실패_이름이_남는다(monkeypatch):
    from database import meeting_store as mt
    monkeypatch.setattr(wk, "get_client", lambda: _Client([_row(7, REF)]))
    monkeypatch.setattr(wk, "subtasks_ready", lambda: False)
    monkeypatch.setattr(wk, "today", lambda: REF)
    monkeypatch.setattr(mt, "open_tasks", lambda limit=20: (_ for _ in ()).throw(RuntimeError("x")))
    tasks, failed = wk.open_tasks_checked()
    assert [t["id"] for t in tasks] == ["w:7"] and failed == ["회의 할 일"]


def test_3c_건수_조회는_strict_일_때만_예외(monkeypatch):
    monkeypatch.setattr(db, "get_client", lambda: _Client(boom=RuntimeError("db down")))
    assert db.count_pending(with_draft=True, escalate=True) == 0          # 예전 그대로
    with pytest.raises(RuntimeError):
        db.count_pending(with_draft=True, escalate=True, strict=True)
    assert db.get_attention_reviews(replied=False, limit=1, select="id") == ([], 0)
    with pytest.raises(RuntimeError):
        db.get_attention_reviews(replied=False, limit=1, select="id", strict=True)
    assert db.search_reviews(days=30, limit=300, sort="new") == ([], 0)
    with pytest.raises(RuntimeError):
        db.search_reviews(days=30, limit=300, sort="new", strict=True)


def test_3d_건수_조회_성공은_strict_여도_같은_값(monkeypatch):
    monkeypatch.setattr(db, "get_client", lambda: _Client([{"id": 1}], count=3))
    assert db.count_pending(with_draft=True, escalate=True, strict=True) == 3
    assert db.get_attention_reviews(replied=False, limit=1, select="id", strict=True)[1] == 3


# ── 4·5: 한 원천이 죽어도 나머지 판단은 살고, '업무 없음'으로 둔갑하지 않는다 ────

def test_4_업무_원천_실패_요청_원천_정상():
    rep = oj.judge_sources({
        "tasks": oj.source("tasks", None, ok=False, error="gather"),
        "requests": oj.source("requests", [_req(1734, 4)]),
        "escalate": oj.source("escalate", 0), "attention": oj.source("attention", 0)},
        today=REF)
    assert [j["dedupe_key"] for j in rep["judgments"]] == ["request.unshared:review:1734"]
    assert rep["failed"] == ["업무 보드"] and rep["complete"] is False
    assert rep["warning"].startswith(oj.WARNING_TEXT) and "업무 보드" in rep["warning"]


def test_5_업무_원천_실패는_할_일_없음이_아니다():
    rep = oj.judge_sources({
        "tasks": oj.source("tasks", None, ok=False, error="gather"),
        "requests": oj.source("requests", []),
        "escalate": oj.source("escalate", 0), "attention": oj.source("attention", 0)},
        today=REF)
    assert rep["judgments"] == []                 # 판단은 없지만
    assert rep["complete"] is False and rep["warning"]      # '없음'이라고 말하지 않는다
    # 실패 때문에 판단·알림을 지어내지 않는다
    assert all(j["source_type"] != "work" for j in rep["judgments"])


def test_5b_부분_실패는_읽은_부분의_판단을_내고_어느_부분인지_말한다():
    rep = oj.judge_sources({
        "tasks": oj.source("tasks", [_task(7, "오늘", due=REF)], ok=False,
                           error="회의 할 일", detail=True),
        "requests": oj.source("requests", []),
        "escalate": oj.source("escalate", 0), "attention": oj.source("attention", 0)},
        today=REF)
    assert [j["dedupe_key"] for j in rep["judgments"]] == ["work.today:w:7"]
    assert rep["failed"] == ["업무 보드: 회의 할 일"]


def test_5c_빠진_원천은_못_읽음으로_본다():
    rep = oj.judge_sources({"tasks": oj.source("tasks", [])}, today=REF)
    assert set(rep["failed"]) == {"고객 요청", "민감 리뷰 건수", "문제 리뷰 건수"}


# ── 7: 전부 건강하면 3-C-1 과 판단이 똑같다 ──────────────────────────────────

def test_7_전부_건강하면_판단은_그대로다():
    tasks = [_task(39, "냉장고", due=REF - timedelta(days=2)), _task(4, "담당 없음")]
    reqs = [_req(10, 5)]
    counts = {"escalate": 1, "attention": 2}
    rep = oj.judge_sources({
        "tasks": oj.source("tasks", tasks), "requests": oj.source("requests", reqs),
        "escalate": oj.source("escalate", 1), "attention": oj.source("attention", 2)}, today=REF)
    assert rep["judgments"] == oj.get_daily_judgments(tasks=tasks, requests=reqs,
                                                      review_counts=counts, today=REF)
    assert rep["failed"] == [] and rep["warning"] == "" and rep["complete"] is True


# ── 화면: app 연결 ──────────────────────────────────────────────────────────

KEY = "testkey"


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setenv("SERVICE_PATH", KEY)
    import service.app as m
    importlib.reload(m)
    m.app.testing = True
    monkeypatch.setattr(m, "_owner_alerts", lambda: [])
    monkeypatch.setattr(m, "_notif_alerts", lambda: [])
    for name in ("meet_tasks", "_work_top_cached", "_briefs_cached"):
        if hasattr(m, name):
            monkeypatch.setattr(m, name, lambda *a, **k: [])
    return m


def _gather_stub(**calls):
    out = {k: None for k in calls}
    for k in ("inbox", "meet_tasks", "work_top", "briefs", "judgments"):
        if k in calls:
            out[k] = calls[k]()
    return out


def test_6_원천_실패_경고가_홈에_보인다(svc, monkeypatch):
    m = svc
    monkeypatch.setattr(m, "gather", _gather_stub)
    monkeypatch.setattr(m, "_judgments_cached", lambda: oj.judge_sources({
        "tasks": oj.source("tasks", None, ok=False, error="gather"),
        "requests": oj.source("requests", [_req(1734, 4)]),
        "escalate": oj.source("escalate", 0), "attention": oj.source("attention", 0)}, today=REF))
    html = m.app.test_client().get(f"/{KEY}/").get_data(as_text=True)
    assert 'class="sec-lbl">오늘의 운영 판단' in html
    assert 'class="jwarn"' in html and "일부 운영 데이터를 불러오지 못했습니다" in html
    assert "업무 보드" in html
    assert f'href="/{KEY}/review#requests"' in html          # 건강한 원천의 판단은 그대로


def test_6b_판단이_하나도_없어도_실패했으면_칸이_뜬다(svc, monkeypatch):
    m = svc
    monkeypatch.setattr(m, "gather", _gather_stub)
    monkeypatch.setattr(m, "_judgments_cached", lambda: oj.judge_sources({
        "tasks": oj.source("tasks", None, ok=False, error="gather"),
        "requests": oj.source("requests", []),
        "escalate": oj.source("escalate", 0), "attention": oj.source("attention", 0)}, today=REF))
    html = m.app.test_client().get(f"/{KEY}/").get_data(as_text=True)
    assert 'class="sec-lbl">오늘의 운영 판단' in html and 'class="jwarn"' in html
    assert 'class="judge"' not in html                        # 지어낸 판단은 없다


def test_7b_전부_건강하면_경고가_없다(svc, monkeypatch):
    m = svc
    monkeypatch.setattr(m, "gather", _gather_stub)
    monkeypatch.setattr(m, "_judgments_cached", lambda: oj.judge_sources({
        "tasks": oj.source("tasks", [_task(39, "냉장고", due=REF - timedelta(days=2))]),
        "requests": oj.source("requests", []),
        "escalate": oj.source("escalate", 0), "attention": oj.source("attention", 0)}, today=REF))
    html = m.app.test_client().get(f"/{KEY}/").get_data(as_text=True)
    assert 'class="jwarn"' not in html and 'data-key="work.overdue:w:39"' in html


def test_8_실제_원천이_죽어도_홈은_200_이고_경고가_뜬다(svc, monkeypatch):
    """gather 스텁 없이 — _judgments_cached 가 진짜로 원천을 부르고, 그 원천들이 터진다."""
    m = svc
    boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down"))  # noqa: E731
    monkeypatch.setattr(wk, "open_tasks_checked", boom)
    monkeypatch.setattr(m, "_customer_requests_checked", boom)
    monkeypatch.setattr(m.db, "count_pending", boom)
    monkeypatch.setattr(m.db, "get_attention_reviews", boom)
    m._judgments_cached.cache_clear()
    rep = m._judgments_cached()
    assert rep["judgments"] == [] and rep["complete"] is False
    assert set(rep["failed"]) == {"업무 보드", "고객 요청", "민감 리뷰 건수", "문제 리뷰 건수"}

    monkeypatch.setattr(m, "gather", _gather_stub)
    r = m.app.test_client().get(f"/{KEY}/")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert 'class="jwarn"' in html and "일부 운영 데이터를 불러오지 못했습니다" in html


def test_8b_한_원천만_죽으면_나머지_판단은_진짜_경로로_나온다(svc, monkeypatch):
    m = svc
    monkeypatch.setattr(wk, "open_tasks_checked",
                        lambda: ([_task(39, "냉장고", due=REF - timedelta(days=2))], []))
    monkeypatch.setattr(m, "_customer_requests_checked",
                        lambda limit=20: ([], 0, "조회 실패(RuntimeError)"))
    monkeypatch.setattr(m.db, "count_pending", lambda **k: 0)
    monkeypatch.setattr(m.db, "get_attention_reviews", lambda **k: ([], 0))
    m._judgments_cached.cache_clear()
    rep = m._judgments_cached()
    assert [j["dedupe_key"] for j in rep["judgments"]] == ["work.overdue:w:39"]
    assert rep["failed"] == ["고객 요청"]


def test_customer_requests_예전_모양과_cache_clear_유지(svc, monkeypatch):
    m = svc
    monkeypatch.setattr(m.db, "search_reviews", boom_search := (
        lambda **k: (_ for _ in ()).throw(RuntimeError("db down"))))
    monkeypatch.setattr(m.db, "log_error", lambda *a, **k: None)
    m._customer_requests.cache_clear()
    assert m._customer_requests() == ([], 0)                   # 예전 모양 그대로
    items, more, err = m._customer_requests_checked(limit=200)
    assert items == [] and more == 0 and err.startswith("조회 실패")
    assert callable(m._customer_requests.cache_clear)
    m._customer_requests.cache_clear()
    monkeypatch.setattr(m.db, "search_reviews", lambda **k: ([], 0))
    assert m._customer_requests_checked()[2] == ""             # 비운 뒤엔 새로 읽는다
    del boom_search
