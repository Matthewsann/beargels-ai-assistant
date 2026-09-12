"""Notification Layer Phase 3-B-2 — 기한 지난 업무 알림 + 끝내면 자동으로 닫힘 (2026-09-12).

계약(사장님 결정):
  · 알림 하나 = 실제로 늦은 업무 하나. 키는 보드 id 그대로 work.overdue:w:<id> / m:<id>
  · rank 0(기한 지남)만. 오늘(1)·내일(2)·기한 없음·오래됨은 새 알림 없음
  · 예전 묶음 잔소리(rank 0·1·2 → Notice 1건, 하루 1회 work_nag_day)는 그대로
  · 표가 없으면 묶음 한 줄뿐 — 업무별 error_log 행을 만들지 않는다
  · 재감지 → 새 줄 없이 occurrences+1
  · 완료 → resolved(reason=task_done). work_store.set_done / meeting_store.set_task_done /
    meeting_store.save_tasks 세 길. 되돌리기는 되살리지 않는다. 삭제는 손대지 않는다

DB·네트워크 불필요 — supabase 를 메모리 표로 흉내낸다.
"""
import sys
import types
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from test_notifications_phase2 import _Store  # noqa: E402  — notifications 가짜 표

REF = date(2026, 9, 12)                       # '오늘'(KST)


# ── 가짜 supabase (업무·회의 할 일 표용) ────────────────────────────────────

class _R:
    def __init__(self, data):
        self.data = data


class _TQ:
    def __init__(self, s, name):
        self.s, self.name, self.f, self.op, self.payload = s, name, [], "select", None
        self.lim = None

    def select(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, n): self.lim = n; return self
    def eq(self, c, v): self.f.append(lambda r: r.get(c) == v); return self
    def in_(self, c, vals): vals = list(vals); self.f.append(lambda r: r.get(c) in vals); return self
    def update(self, p): self.op, self.payload = "update", p; return self
    def insert(self, p): self.op, self.payload = "insert", p; return self
    def delete(self): self.op = "delete"; return self

    def execute(self):
        rows = self.s.tables.setdefault(self.name, [])
        if self.op == "insert":
            items = self.payload if isinstance(self.payload, list) else [self.payload]
            for it in items:
                rows.append(dict(it, id=self.s.next_id()))
            return _R(items)
        hit = [r for r in rows if all(f(r) for f in self.f)]
        if self.op == "update":
            for r in hit:
                r.update(self.payload)
            return _R(hit)
        if self.op == "delete":
            self.s.tables[self.name] = [r for r in rows if r not in hit]
            return _R(hit)
        return _R(hit[: self.lim] if self.lim else hit)


class _Tables:
    def __init__(self, **tables):
        self.tables = dict(tables)
        self._id = 100

    def next_id(self):
        self._id += 1
        return self._id

    def table(self, name):
        return _TQ(self, name)


@pytest.fixture
def ns(monkeypatch):
    """notifications 가짜 표 — 진짜 notification_store 코드가 그 위에서 돈다."""
    from database import notification_store as m
    store = _Store()
    monkeypatch.setattr(m, "get_client", lambda: store)
    m._reset_cache()
    return m, store


def _rows(store):
    return store.tables["notifications"]


# ── 업무 보드가 잔소리에 주는 모양 그대로(_view + priority_of) ───────────────

def _task(source, rid, content, due=None, owner="직원A", created="2026-09-01T00:00:00+00:00"):
    from database import work_store as wk
    row = {"id": rid, "content": content, "owner": owner,
           "due_date": due.isoformat() if due else None, "created_at": created,
           "done": False, "memo": None, "meeting_id": 7 if source == "meeting" else None}
    if source == "meeting":
        row["due"] = row.pop("due_date")
        row["meeting_date"] = created[:10]
    return wk._view(row, source, REF)


@pytest.fixture
def nag(monkeypatch):
    """maybe_work_nag 를 고립시킨다 — 시각·kv·업무 목록·알림 출구를 가짜로."""
    import worker.agent as ag
    kv = {}
    monkeypatch.setattr(ag, "db", types.SimpleNamespace(
        get_setting=lambda k, d=None: kv.get(k, d),
        menu_set_setting=lambda k, v: kv.__setitem__(k, v)))
    tasks = []
    from database import work_store as wk
    monkeypatch.setattr(wk, "open_tasks", lambda: list(tasks))
    sent, old = [], []
    monkeypatch.setattr(ag, "notify", lambda **k: sent.append(k))
    monkeypatch.setattr(ag, "notify_owner", lambda text, kind="Notice", **k: old.append((kind, text)))
    monkeypatch.setattr(ag, "notifications_ready", lambda: True)

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 12, 12, 0, 0)
    monkeypatch.setattr(ag, "datetime", _Now)
    return ag, kv, tasks, sent, old


# ── Test 1~4·8·13: 무엇이 알림이 되는가 ────────────────────────────────────

def test_overdue_work_task_creates_notification(nag):
    ag, kv, tasks, sent, old = nag
    tasks.append(_task("work", 12, "9월 메뉴판 발주", due=REF - timedelta(days=3)))
    ag.maybe_work_nag()
    assert len(sent) == 1
    n = sent[0]
    assert n["event_type"] == "work.overdue" and n["dedupe_key"] == "work.overdue:w:12"
    assert n["source_ref"] == "w:12" and n["title"] == "9월 메뉴판 발주"
    assert "기한 3일 지났어요" in n["message"] and "직원A" in n["message"]


@pytest.mark.parametrize("due,label", [
    (REF, "오늘까지"), (REF + timedelta(days=1), "내일까지"), (None, "기한 없음"),
    (REF + timedelta(days=5), "이번 주")])
def test_not_overdue_tasks_do_not_create_notifications(nag, due, label):
    ag, kv, tasks, sent, old = nag
    tasks.append(_task("work", 12, f"{label} 업무", due=due))
    ag.maybe_work_nag()
    assert sent == [], label


def test_old_unassigned_task_without_due_is_not_notified(nag):
    ag, kv, tasks, sent, old = nag
    tasks.append(_task("work", 12, "방치된 업무", due=None, owner="", created="2026-08-01T00:00:00+00:00"))
    ag.maybe_work_nag()
    assert sent == []


def test_overdue_meeting_task_creates_notification(nag):
    ag, kv, tasks, sent, old = nag
    tasks.append(_task("meeting", 34, "토마토주스 사진", due=REF - timedelta(days=1)))
    ag.maybe_work_nag()
    assert [s["dedupe_key"] for s in sent] == ["work.overdue:m:34"]
    assert sent[0]["source_ref"] == "m:34"


def test_work_and_meeting_ids_do_not_collide(nag):
    ag, kv, tasks, sent, old = nag
    tasks += [_task("work", 5, "a", due=REF - timedelta(days=2)),
              _task("meeting", 5, "b", due=REF - timedelta(days=2)),
              _task("work", 6, "c", due=REF)]                       # 오늘 — 제외
    ag.maybe_work_nag()
    assert sorted(s["dedupe_key"] for s in sent) == ["work.overdue:m:5", "work.overdue:w:5"]


# ── Test 11·12: 예전 묶음은 그대로, 표 없으면 묶음뿐 ───────────────────────

def test_grouped_legacy_notice_unchanged(nag):
    ag, kv, tasks, sent, old = nag
    tasks += [_task("work", 1, "지난 것", due=REF - timedelta(days=2)),
              _task("work", 2, "오늘 것", due=REF),
              _task("work", 3, "내일 것", due=REF + timedelta(days=1))]
    ag.maybe_work_nag()
    assert len(old) == 1 and old[0][0] == "Notice"
    assert "기한이 급한 업무가 3건 있어요" in old[0][1]            # rank 0·1·2 전부
    assert "지난 것" in old[0][1] and "(업무 보드에서 확인)" in old[0][1]
    assert kv["work_nag_day"] == "2026-09-12"
    assert [s["dedupe_key"] for s in sent] == ["work.overdue:w:1"]  # 새 길은 rank 0 만


def test_fallback_keeps_only_grouped_notice_when_table_missing(nag, monkeypatch):
    ag, kv, tasks, sent, old = nag
    monkeypatch.setattr(ag, "notifications_ready", lambda: False)
    tasks += [_task("work", 1, "지난 것", due=REF - timedelta(days=2)),
              _task("work", 2, "더 지난 것", due=REF - timedelta(days=9))]
    ag.maybe_work_nag()
    assert sent == []
    assert len(old) == 1 and "2건" in old[0][1]


def test_nag_runs_once_a_day(nag):
    ag, kv, tasks, sent, old = nag
    tasks.append(_task("work", 1, "x", due=REF - timedelta(days=1)))
    ag.maybe_work_nag(); ag.maybe_work_nag()
    assert len(old) == 1 and len(sent) == 1


# ── Test 5: 재감지는 새 줄이 아니라 횟수 (진짜 alerts.notify + 가짜 표) ───

def test_repeated_scans_bump_occurrences_not_rows(nag, ns, monkeypatch):
    ag, kv, tasks, sent, old = nag
    import alerts
    monkeypatch.setattr(ag, "notify", alerts.notify)                  # 진짜 저장 경로
    nsm, store = ns
    tasks.append(_task("work", 12, "늦은 업무", due=REF - timedelta(days=3)))

    class _Day1(datetime):
        @classmethod
        def now(cls, tz=None): return cls(2026, 9, 12, 12, 0, 0)

    class _Day2(datetime):
        @classmethod
        def now(cls, tz=None): return cls(2026, 9, 13, 12, 0, 0)
    monkeypatch.setattr(ag, "datetime", _Day1); ag.maybe_work_nag()
    monkeypatch.setattr(ag, "datetime", _Day2); ag.maybe_work_nag()
    rows = _rows(store)
    assert len(rows) == 1
    assert rows[0]["dedupe_key"] == "work.overdue:w:12" and rows[0]["occurrences"] == 2
    assert rows[0]["status"] == "open" and rows[0]["severity"] == "high" and rows[0]["link"] == "/work"


# ── Test 6·7·14: 업무 완료 → resolved, 되돌려도 그대로 ──────────────────────

@pytest.fixture
def wk(monkeypatch):
    from database import work_store as m
    db = _Tables(work_tasks=[{"id": 12, "content": "늦은 업무", "done": False, "done_at": None,
                              "parent_id": None}])
    monkeypatch.setattr(m, "get_client", lambda: db)
    monkeypatch.setattr(m, "_touch", lambda: None)
    monkeypatch.setattr(m, "subtasks_ready", lambda: False)             # v14 미적용(프로덕션 그대로)
    return m, db


def test_completing_work_task_resolves_notification(wk, ns):
    m, db = wk
    nsm, store = ns
    nsm.record("work.overdue", "work.overdue:w:12", "늦은 업무", "기한 3일 지났어요")
    m.set_done(12, True)
    assert db.tables["work_tasks"][0]["done"] is True
    r = _rows(store)[0]
    assert r["status"] == "resolved" and r["resolve_reason"] == "task_done"
    assert r["resolved_by"] == "직원웹(완료)" and r["resolved_at"]


def test_undo_does_not_reopen(wk, ns):
    m, db = wk
    nsm, store = ns
    nsm.record("work.overdue", "work.overdue:w:12", "늦은 업무", "")
    m.set_done(12, True)
    m.set_done(12, False)
    assert db.tables["work_tasks"][0]["done"] is False
    assert _rows(store)[0]["status"] == "resolved"
    # 다음 잔소리가 또 잡아도 되살리지 않는다(Phase 2 규칙)
    row, outcome = nsm.record("work.overdue", "work.overdue:w:12", "늦은 업무", "")
    assert outcome == "kept_closed" and row["status"] == "resolved" and len(_rows(store)) == 1


def test_completion_without_notification_is_harmless(wk, ns):
    m, db = wk
    m.set_done(12, True)                                                # 알림이 없어도 예외 없음
    assert db.tables["work_tasks"][0]["done"] is True


def test_completion_survives_notification_failure(wk, ns, monkeypatch):
    m, db = wk
    nsm, store = ns
    monkeypatch.setattr(nsm, "resolve_by_key", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("끊김")))
    m.set_done(12, True)
    assert db.tables["work_tasks"][0]["done"] is True


def test_work_completion_does_not_touch_other_notifications(wk, ns):
    m, db = wk
    nsm, store = ns
    nsm.record("request.unshared", "request.unshared:review:12", "요청", "")
    nsm.record("work.overdue", "work.overdue:m:12", "회의 할 일", "")
    m.set_done(12, True)
    assert {r["dedupe_key"]: r["status"] for r in _rows(store)} == {
        "request.unshared:review:12": "open", "work.overdue:m:12": "open"}


# ── Test 9·10: 회의 할 일 완료 두 길 ────────────────────────────────────────

@pytest.fixture
def mt(monkeypatch):
    from database import meeting_store as m
    db = _Tables(meeting_tasks=[{"id": 34, "meeting_id": 7, "content": "토마토주스 사진",
                                 "owner": "직원B", "due_date": "2026-09-10", "memo": None,
                                 "sort": 0, "done": False, "done_at": None}])
    monkeypatch.setattr(m, "get_client", lambda: db)
    monkeypatch.setattr(m, "_touch", lambda: None)
    return m, db


def test_meeting_set_task_done_resolves(mt, ns):
    m, db = mt
    nsm, store = ns
    nsm.record("work.overdue", "work.overdue:m:34", "토마토주스 사진", "")
    m.set_task_done(34, True)
    assert db.tables["meeting_tasks"][0]["done"] is True
    assert _rows(store)[0]["status"] == "resolved" and _rows(store)[0]["resolve_reason"] == "task_done"


def test_meeting_save_tasks_completion_resolves_once(mt, ns, monkeypatch):
    m, db = mt
    nsm, store = ns
    nsm.record("work.overdue", "work.overdue:m:34", "토마토주스 사진", "")
    calls = []
    real = nsm.resolve_by_key
    monkeypatch.setattr(nsm, "resolve_by_key", lambda *a, **k: calls.append(a) or real(*a, **k))
    m.save_tasks(7, [{"id": 34, "content": "토마토주스 사진", "owner": "직원B",
                      "due_date": "2026-09-10", "done": True}])
    assert db.tables["meeting_tasks"][0]["done"] is True and db.tables["meeting_tasks"][0]["done_at"]
    assert _rows(store)[0]["status"] == "resolved"
    assert len(calls) == 1                                               # 한 번만
    # 이미 끝난 줄을 다시 저장해도(변화 없음) 또 부르지 않는다
    m.save_tasks(7, [{"id": 34, "content": "토마토주스 사진", "owner": "직원B",
                      "due_date": "2026-09-10", "done": True}])
    assert len(calls) == 1


def test_meeting_undo_via_save_tasks_does_not_reopen(mt, ns):
    m, db = mt
    nsm, store = ns
    nsm.record("work.overdue", "work.overdue:m:34", "x", "")
    m.set_task_done(34, True)
    m.save_tasks(7, [{"id": 34, "content": "x", "done": False}])
    assert db.tables["meeting_tasks"][0]["done"] is False
    assert _rows(store)[0]["status"] == "resolved"


# ── Test 15: 고객 요청 알림·정책은 그대로 ──────────────────────────────────

def test_policy_has_work_overdue_and_request_unchanged():
    import alerts
    assert alerts._POLICY["work.overdue"] == {"severity": "high", "recipient_type": "role",
                                              "recipient_id": "owner", "link": "/work"}
    assert alerts._POLICY["request.unshared"]["link"] == "/review"
