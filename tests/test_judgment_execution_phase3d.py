"""Phase 3-D — 판단 → 실행 → 완료 (2026-09-13).

감사 결론: 판단 종류마다 실행 대상이 이미 있다. 업무 판단(work.*)은 기존 업무 그 자체
(task_ref = w:<id>|m:<id>), 고객 요청은 리뷰 현황의 [공유 완료](request_shared 가 진실),
문제 리뷰는 답글 등록(리뷰 상태가 진실). 그래서 **판단은 업무를 만들지 않는다** —
새 업무를 만들면 같은 문제의 완료 기준이 둘로 갈라진다.

계약:
  · 업무 판단은 기존 업무를 가리키고(action_kind=task), 홈 [✓ 완료]는 보드와 같은
    /work/task/<id>/done 을 부른다 → set_done → work.overdue:<id> 알림이 닫힌다.
  · 판단 계산·홈 새로고침을 몇 번 해도 업무·알림이 새로 생기지 않는다(멱등).
  · 다른 업무를 끝내도 이 판단·알림은 닫히지 않는다.
  · 요청·리뷰 판단의 완료는 원천 상태(request_shared·리뷰 상태)만 바꾼다.
  · 담당 없는 업무를 판단이 대표에게 몰래 배정하지 않는다.
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

from database import work_store as wk  # noqa: E402
from service import operations_judgment as oj  # noqa: E402
from test_operations_judgment import _req, _task  # noqa: E402
from test_operations_judgment_reliability import _Client, _row  # noqa: E402

REF = date(2026, 9, 12)          # _task() 헬퍼(3-C-1 테스트)와 같은 기준일
KEY = "testkey"


# ── 1·9: 판단은 기존 업무를 가리킨다, 담당은 건드리지 않는다 ─────────────────────

def test_1_기한_지난_업무_판단은_그_업무를_가리킨다():
    js = oj.get_daily_judgments(tasks=[_task(39, "냉장고 수리", due=REF - timedelta(days=2), owner="민수")], today=REF)
    j = js[0]
    assert j["task_ref"] == "w:39" and j["source_id"] == "w:39"
    assert j["action"] == "업무 처리" and j["action_kind"] == "task"
    assert j["link"] == "/work#w-39"                          # 보드의 그 줄
    assert j["dedupe_key"] == "work.overdue:w:39"             # 알림과 같은 정체성


def test_1b_회의_할_일도_그대로_가리킨다():
    js = oj.get_daily_judgments(tasks=[_task(9, "아이스크림", due=REF - timedelta(days=12), source="meeting")], today=REF)
    assert js[0]["task_ref"] == "m:9" and js[0]["action_kind"] == "task"


def test_1c_종류별_실행_경로():
    js = oj.get_daily_judgments(
        tasks=[_task(1, "늦음", due=REF - timedelta(days=1)), _task(2, "담당 없음"),
               _task(3, "묵은 것", owner="a", created=REF - timedelta(days=10))],
        requests=[_req(5, 4)], review_counts={"escalate": 1, "attention": 1}, today=REF)
    got = {j["dedupe_key"]: (j["action"], j["action_kind"], j["task_ref"]) for j in js}
    assert got["work.overdue:w:1"] == ("업무 처리", "task", "w:1")
    assert got["work.unassigned:w:2"] == ("담당자 정하기", "assign", "w:2")
    assert got["work.stale:w:3"] == ("기한 정하기", "due", "w:3")
    assert got["request.unshared:review:5"] == ("공유 처리", "share", None)
    assert got["review.escalate"] == ("답글 처리", "reply", None)
    assert got["review.attention"] == ("답글 처리", "reply", None)
    for j in js:
        assert j["done_by"]                                    # 완료의 근거가 적혀 있다


def test_9_담당_없는_업무를_대표에게_몰래_배정하지_않는다(monkeypatch):
    called = []
    monkeypatch.setattr(wk, "update_task", lambda *a, **k: called.append((a, k)))
    monkeypatch.setattr(wk, "add_task", lambda *a, **k: called.append(("add", a, k)))
    js = oj.get_daily_judgments(tasks=[_task(2, "담당 없음")], today=REF)
    assert js[0]["dedupe_key"] == "work.unassigned:w:2" and "담당자를 정해" in js[0]["reason"]
    assert called == []                                        # 배정도 생성도 없다


# ── 2·3·4: 멱등 — 업무·알림이 생기지 않는다 ─────────────────────────────────────

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
    return out


def _report(tasks, requests=(), escalate=0, attention=0):
    src = {"tasks": oj.source("tasks", tasks), "requests": oj.source("requests", list(requests)),
           "escalate": oj.source("escalate", escalate), "attention": oj.source("attention", attention)}
    rep = oj.judge_sources(src, today=REF)
    rep.update(tasks=tasks, tasks_failed=False, requests=list(requests), escalate=escalate)
    return rep


def test_2_3_4_판단_계산과_홈_새로고침은_업무도_알림도_만들지_않는다(svc, monkeypatch):
    m = svc
    from database import notification_store as ns
    writes = []
    monkeypatch.setattr(wk, "add_task", lambda *a, **k: writes.append("add_task"))
    monkeypatch.setattr(wk, "update_task", lambda *a, **k: writes.append("update_task"))
    monkeypatch.setattr(ns, "record", lambda *a, **k: writes.append("notify"))
    tasks = [_task(39, "냉장고", due=REF - timedelta(days=2))]
    a = oj.get_daily_judgments(tasks=tasks, requests=[_req(5, 4)], today=REF)
    b = oj.get_daily_judgments(tasks=tasks, requests=[_req(5, 4)], today=REF)
    assert a == b                                              # 정체성이 같다(시각 없음)
    monkeypatch.setattr(m, "gather", _gather_stub)
    monkeypatch.setattr(m, "_judgments_cached", lambda: _report(tasks, [_req(5, 4)]))
    for _ in range(3):                                         # 새로고침 3번
        assert m.app.test_client().get(f"/{KEY}/").status_code == 200
    assert writes == []


# ── 5·6: 완료 → 알림 해소, 남의 업무는 무관 ────────────────────────────────────

@pytest.fixture
def done_env(svc, monkeypatch):
    """/work/task/<id>/done 이 진짜 set_done 을 거쳐 알림 훅을 부르게 — 표는 가짜."""
    m = svc
    from database import meeting_store as mt
    from database import notification_store as ns
    resolved = []
    monkeypatch.setattr(ns, "resolve_by_key", lambda k, **kw: resolved.append(k))
    monkeypatch.setattr(wk, "get_client", lambda: _Client([_row(39, REF - timedelta(days=2))]))
    monkeypatch.setattr(wk, "subtasks_ready", lambda: False)
    monkeypatch.setattr(mt, "get_client", lambda: _Client([{"id": 9}]))
    monkeypatch.setattr(m.db, "log_error", lambda *a, **k: None)
    cleared = []
    monkeypatch.setattr(m._judgments_cached, "cache_clear", lambda: cleared.append("judgments"))
    return m, resolved, cleared


def _post_done(m, ref):
    return m.app.test_client().post(f"/{KEY}/work/task/{ref}/done", json={"done": True},
                                    headers={"X-Requested-With": "fetch"})


def test_5_홈_완료_버튼_경로는_그_업무의_기한_지남_알림을_닫는다(done_env):
    m, resolved, cleared = done_env
    r = _post_done(m, "w:39")
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert resolved == ["work.overdue:w:39"]                   # 판단의 dedupe_key 와 같다
    assert "judgments" in cleared                              # 다음 홈은 바로 새 판단


def test_5b_회의_할_일_완료도_같은_길(done_env):
    m, resolved, _ = done_env
    assert _post_done(m, "m:9").get_json()["ok"] is True
    assert resolved == ["work.overdue:m:9"]


def test_6_다른_업무를_끝내도_이_판단의_알림은_닫히지_않는다(done_env):
    m, resolved, _ = done_env
    _post_done(m, "w:40")
    assert resolved == ["work.overdue:w:40"] and "work.overdue:w:39" not in resolved
    # 요청·리뷰 알림도 업무 완료로는 안 닫힌다
    assert not any(k.startswith("request.") or k.startswith("review.") for k in resolved)


# ── 7·8: 요청·리뷰의 완료는 원천 상태가 정한다 ──────────────────────────────────

def test_7_요청_판단의_완료는_request_shared_가_진실(svc, monkeypatch):
    m = svc
    from database import notification_store as ns
    resolved, saved = [], {}
    monkeypatch.setattr(ns, "resolve_by_key", lambda k, **kw: resolved.append(k))
    monkeypatch.setattr(m, "_shared_request_ids", lambda: set())
    monkeypatch.setattr(m.db, "menu_set_setting", lambda k, v: saved.update({k: v}))
    monkeypatch.setattr(m.db, "log_error", lambda *a, **k: None)
    # 업무를 아무리 끝내도 요청 판단은 남아 있다(원천이 안 바뀌었으니)
    js = oj.get_daily_judgments(tasks=[], requests=[_req(1734, 4)], today=REF)
    assert [j["dedupe_key"] for j in js] == ["request.unshared:review:1734"]
    assert js[0]["action_kind"] == "share" and js[0]["task_ref"] is None
    # [공유 완료] — 기존 경로 그대로: request_shared 에 적히고 알림이 닫힌다
    r = m.app.test_client().post(f"/{KEY}/requests/shared", json={"ids": [1734]})
    assert r.get_json()["ok"] is True
    assert 1734 in saved.get(m.REQUEST_SHARED_KEY, [])
    assert resolved == ["request.unshared:review:1734"]
    # 공유된 요청은 _customer_requests 가 빼 주므로 다음 판단엔 없다(엔진은 받은 것만 본다)
    assert oj.get_daily_judgments(tasks=[], requests=[], today=REF) == []


def test_8_리뷰_판단의_완료는_리뷰_상태가_진실():
    # 건수가 남아 있으면 판단도 남는다 — 업무 완료·버튼과 무관. 0 이 되면 사라진다.
    a = oj.get_daily_judgments(tasks=[], requests=[], review_counts={"attention": 2}, today=REF)
    assert [j["dedupe_key"] for j in a] == ["review.attention"] and a[0]["task_ref"] is None
    assert a[0]["link"] == "/care" and a[0]["action"] == "답글 처리"
    b = oj.get_daily_judgments(tasks=[], requests=[], review_counts={"attention": 0}, today=REF)
    assert b == []


# ── 10·11·12: 기존 동작 유지 + 홈 ───────────────────────────────────────────────

def test_10_건강한_판단의_칸_열쇠_링크는_3C1_과_같다():
    tasks = [_task(1, "하루 늦음", due=REF - timedelta(days=1)),
             _task(2, "닷새 늦음", due=REF - timedelta(days=5)),
             _task(3, "오늘", due=REF, owner="a"), _task(4, "담당 없음"),
             _task(5, "회의 늦음", due=REF - timedelta(days=3), source="meeting")]
    js = oj.get_daily_judgments(tasks=tasks, requests=[_req(10, 3), _req(11, 6)],
                                review_counts={"escalate": 1, "attention": 2}, today=REF)
    assert [(j["category"], j["dedupe_key"], j["link"]) for j in js] == [
        ("urgent", "work.overdue:w:2", "/work#w-2"), ("urgent", "work.overdue:m:5", "/work#m-5"),
        ("urgent", "work.overdue:w:1", "/work#w-1"), ("today", "work.today:w:3", "/work#w-3"),
        ("check", "review.escalate", "/todo"), ("check", "request.unshared:review:11", "/review#requests"),
        ("check", "request.unshared:review:10", "/review#requests"), ("check", "review.attention", "/care"),
        ("check", "work.unassigned:w:4", "/work#w-4")]


def test_11_부분_실패는_여전히_보인다(svc, monkeypatch):
    m = svc
    monkeypatch.setattr(m, "gather", _gather_stub)
    rep = oj.judge_sources({"tasks": oj.source("tasks", [_task(7, "오늘", due=REF)], ok=False,
                                               error="회의 할 일", detail=True),
                            "requests": oj.source("requests", []), "escalate": oj.source("escalate", 0),
                            "attention": oj.source("attention", 0)}, today=REF)
    rep.update(tasks=[_task(7, "오늘", due=REF)], tasks_failed=True, requests=[], escalate=0)
    monkeypatch.setattr(m, "_judgments_cached", lambda: rep)
    html = m.app.test_client().get(f"/{KEY}/").get_data(as_text=True)
    assert 'class="jwarn"' in html and "회의 할 일" in html and 'data-task="w:7"' in html


def test_12_홈은_실행_경로를_그린다(svc, monkeypatch):
    m = svc
    monkeypatch.setattr(m, "gather", _gather_stub)
    monkeypatch.setattr(m, "_judgments_cached", lambda: _report(
        [_task(39, "냉장고 수리", due=REF - timedelta(days=2), owner="민수"), _task(2, "담당 없음")],
        [_req(5, 4)], escalate=1))
    r = m.app.test_client().get(f"/{KEY}/")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert 'data-task="w:39"' in html and "업무 처리 →" in html
    assert html.count('class="jdone"') == 1                    # 업무(task)만 완료 버튼
    assert "담당자 정하기 →" in html and "공유 처리 →" in html and "답글 처리 →" in html
    assert f'/{KEY}/work/task/" + id + "/done' in html          # 보드와 같은 완료 경로
