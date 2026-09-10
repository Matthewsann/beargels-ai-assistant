"""Notification Layer Phase 2 — 고객 요청 미전파 1종을 notifications 표로 잇는다 (2026-09-11).

계약:
  · 발생원(maybe_request_nag)은 alerts.notify() 만 부른다 — 표의 생김새를 모른다
  · 요청 하나(reviews.id) = 알림 한 줄. 같은 요청이 다시 잡히면 새 줄 대신 occurrences+1
  · 다른 요청은 각각 별도 줄
  · 닫힌(resolved) 줄은 되살리지 않는다 — 횟수만 기록 (자동 해소는 Phase 3)
  · 표가 없으면 notify() 는 예전 길(error_log Notice)로, 잔소리는 예전 묶음 한 줄 그대로
  · 홈에서만 새 알림함이 보이고 [확인]=read, [처리됨]=resolved. error_log 알림함은 그대로

DB·네트워크 불필요 — supabase 클라이언트를 메모리 표로 흉내낸다.
"""
import importlib
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "service") not in sys.path:
    sys.path.insert(0, str(ROOT / "service"))


# ── 메모리 표 — supabase-py 체인 흉내 ───────────────────────────────────────

class _Res:
    def __init__(self, data):
        self.data = data


class _Q:
    def __init__(self, store, name):
        self.s, self.name = store, name
        self.filters, self.order_key, self.desc, self.lim = [], None, False, None
        self.op, self.payload = "select", None

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self.filters.append(lambda r: r.get(col) == val)
        return self

    def in_(self, col, vals):
        vals = list(vals)
        self.filters.append(lambda r: r.get(col) in vals)
        return self

    def order(self, col, desc=False):
        self.order_key, self.desc = col, desc
        return self

    def limit(self, n):
        self.lim = n
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def execute(self):
        if self.name not in self.s.tables:
            raise self.s.missing_error()
        rows = self.s.tables[self.name]
        if self.op == "insert":
            key = self.payload.get("dedupe_key")
            if any(r["dedupe_key"] == key and r["status"] in ("open", "sending", "sent", "read")
                   for r in rows):
                e = Exception("duplicate key value violates unique index"); e.code = "23505"
                raise e
            row = dict(self.payload, id=self.s.next_id(), created_at=self.s.tick(),
                       updated_at=None)
            rows.append(row)
            return _Res([row])
        hit = [r for r in rows if all(f(r) for f in self.filters)]
        if self.op == "update":
            for r in hit:
                r.update(self.payload)
            return _Res(hit)
        if self.order_key:
            hit.sort(key=lambda r: r.get(self.order_key) or "", reverse=self.desc)
        if self.lim:
            hit = hit[:self.lim]
        return _Res(hit)


class _Store:
    def __init__(self, with_table=True):
        self.tables = {"notifications": []} if with_table else {}
        self._id, self._t = 0, 0

    def next_id(self):
        self._id += 1
        return self._id

    def tick(self):
        self._t += 1
        return f"2026-09-11T00:00:{self._t:02d}+00:00"

    @staticmethod
    def missing_error():
        e = Exception("Could not find the table 'public.notifications' in the schema cache")
        e.code = "PGRST205"
        return e

    def table(self, name):
        return _Q(self, name)


@pytest.fixture
def ns(monkeypatch):
    from database import notification_store as m
    store = _Store()
    monkeypatch.setattr(m, "get_client", lambda: store)
    m._reset_cache()
    return m, store


def _req(review_id, topic="맛·품질", quote="베이글에 탄자국이 있어서 아쉬워요"):
    return {"id": review_id, "topic": topic, "quote": quote,
            "date": "2026-09-01", "collected": "2026-09-01", "platform": "배민"}


# ── Test 1·2·3: 저장·중복·분리 ─────────────────────────────────────────────

def test_first_detection_creates_one_open_row(ns):
    m, store = ns
    row, outcome = m.record("request.unshared", "request.unshared:review:1734",
                            "고객 요청 미전파 — [맛·품질] …", "3일 넘게 안 갔어요",
                            severity="high", source_ref="review:1734", link="/review")
    assert outcome == "created"
    assert len(store.tables["notifications"]) == 1
    r = store.tables["notifications"][0]
    assert r["status"] == "open" and r["occurrences"] == 1
    assert r["severity"] == "high" and r["recipient_id"] == "owner"
    assert r["dedupe_key"] == "request.unshared:review:1734"


def test_same_request_again_bumps_occurrences_not_rows(ns, monkeypatch):
    m, store = ns
    import itertools
    ticks = itertools.count(1)
    # 윈도우 시계 해상도와 무관하게 — 부를 때마다 다른 시각(updated_at 도 이걸 읽는다)
    monkeypatch.setattr(m, "_now_iso", lambda: f"2026-09-11T00:00:{next(ticks):02d}+00:00")
    m.record("request.unshared", "request.unshared:review:1734", "제목", "본문")
    first_seen = store.tables["notifications"][0]["last_seen_at"]
    row, outcome = m.record("request.unshared", "request.unshared:review:1734",
                            "제목(갱신)", "본문(갱신)")
    assert outcome == "updated"
    assert len(store.tables["notifications"]) == 1
    r = store.tables["notifications"][0]
    assert r["occurrences"] == 2 and r["status"] == "open"
    assert r["last_seen_at"] != first_seen
    assert r["title"] == "제목(갱신)"                  # 문구는 최신으로


def test_different_requests_get_separate_rows(ns):
    m, store = ns
    m.record("request.unshared", "request.unshared:review:1", "a", "")
    m.record("request.unshared", "request.unshared:review:2", "b", "")
    m.record("request.unshared", "request.unshared:review:1", "a", "")
    rows = store.tables["notifications"]
    assert len(rows) == 2
    assert {r["dedupe_key"]: r["occurrences"] for r in rows} == {
        "request.unshared:review:1": 2, "request.unshared:review:2": 1}


def test_resolved_row_is_not_revived_by_recurrence(ns):
    """[처리됨] 뒤 같은 요청이 또 잡혀도 새 줄·되살림 없이 횟수만 남는다."""
    m, store = ns
    m.record("request.unshared", "request.unshared:review:9", "a", "")
    m.mark_resolved(store.tables["notifications"][0]["id"], by="사장님(화면)", reason="manual")
    row, outcome = m.record("request.unshared", "request.unshared:review:9", "a", "")
    assert outcome == "kept_closed"
    assert len(store.tables["notifications"]) == 1
    assert row["status"] == "resolved" and row["occurrences"] == 2


def test_unique_index_race_falls_back_to_update(ns):
    """insert 가 유니크 충돌(23505)로 튕기면 다시 찾아 갱신한다."""
    m, store = ns
    real = m._rows_for
    calls = {"n": 0}

    def flaky(key):
        calls["n"] += 1
        if calls["n"] == 1:
            return []                                   # 첫 조회: 못 봄(경합)
        return real(key)
    monkeypatch_target = m
    monkeypatch_target._rows_for = flaky
    try:
        store.tables["notifications"].append({
            "id": 77, "dedupe_key": "k", "status": "open", "occurrences": 1,
            "created_at": "2026-09-11T00:00:00+00:00"})
        row, outcome = m.record("request.unshared", "k", "t", "")
    finally:
        m._rows_for = real
    assert outcome == "updated" and row["occurrences"] == 2


# ── 표가 없을 때 — 예전 길 그대로 ──────────────────────────────────────────

def test_notify_falls_back_to_error_log_when_table_missing(monkeypatch):
    from database import notification_store as m
    import alerts
    store = _Store(with_table=False)
    monkeypatch.setattr(m, "get_client", lambda: store)
    m._reset_cache()
    old = []
    monkeypatch.setattr(alerts, "notify_owner",
                        lambda text, kind="Notice", **k: old.append((kind, text, k.get("path"))))

    assert alerts.notifications_ready() is False
    out = alerts.notify("request.unshared", "request.unshared:review:5", "제목", "본문")

    assert out is None
    assert old == [("Notice", "제목 — 본문", "request.unshared")]


def test_available_is_cached_and_recovers(ns):
    m, store = ns
    assert m.available() is True
    del store.tables["notifications"]
    assert m.available() is True          # 있다고 기억한 건 다시 안 묻는다
    m._reset_cache()
    assert m.available() is False


# ── 발생원: maybe_request_nag ────────────────────────────────────────────────

@pytest.fixture
def nag(monkeypatch):
    import worker.agent as ag
    from datetime import datetime
    kv = {}
    fake = types.SimpleNamespace(
        get_setting=lambda k, d=None: kv.get(k, d),
        menu_set_setting=lambda k, v: kv.__setitem__(k, v),
        search_reviews=lambda **k: ([{"id": 1}, {"id": 2}], 2),
    )
    monkeypatch.setattr(ag, "db", fake)
    import assistant.customer_requests as cr
    monkeypatch.setattr(cr, "find_requests",
                        lambda rows, limit=20: [_req(1), _req(2, topic="포장", quote="빵칼 넣어주세요")])
    sent, old = [], []
    monkeypatch.setattr(ag, "notify", lambda **k: sent.append(k))
    monkeypatch.setattr(ag, "notify_owner",
                        lambda text, kind="Notice", **k: old.append((kind, text)))

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 11, 12, 0, 0)
    monkeypatch.setattr(ag, "datetime", _Now)
    return ag, kv, sent, old


def test_request_nag_emits_one_notification_per_request_when_ready(nag, monkeypatch):
    ag, kv, sent, old = nag
    monkeypatch.setattr(ag, "notifications_ready", lambda: True)

    ag.maybe_request_nag()

    assert old == []                                       # 묶음 Notice 는 안 낸다
    assert [s["dedupe_key"] for s in sent] == [
        "request.unshared:review:1", "request.unshared:review:2"]
    assert all(s["event_type"] == "request.unshared" for s in sent)
    assert sent[0]["source_ref"] == "review:1" and "[맛·품질]" in sent[0]["title"]
    assert kv["request_nag_day"] == "2026-09-11"          # 하루 1회 표식은 그대로


def test_request_nag_keeps_old_bundle_when_table_missing(nag, monkeypatch):
    ag, kv, sent, old = nag
    monkeypatch.setattr(ag, "notifications_ready", lambda: False)

    ag.maybe_request_nag()

    assert sent == []
    assert len(old) == 1 and old[0][0] == "Notice"
    assert "고객 요청 2건" in old[0][1] and "[맛·품질]" in old[0][1]


def test_request_nag_runs_once_a_day_either_way(nag, monkeypatch):
    ag, kv, sent, old = nag
    monkeypatch.setattr(ag, "notifications_ready", lambda: True)
    ag.maybe_request_nag()
    ag.maybe_request_nag()
    assert len(sent) == 2                                   # 두 번째 호출은 건너뜀


# ── 웹: 홈 알림함 · [확인] · [처리됨] · 기존 알림함 불변 ──────────────────

KEY = "testkey"


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setenv("SERVICE_PATH", KEY)
    import service.app as m
    importlib.reload(m)
    m.app.testing = True
    return m


def _gather_stub(**calls):
    out = {k: None for k in calls}
    for k in ("alerts", "nalerts", "meet_tasks", "work_top", "briefs"):
        if k in calls:
            out[k] = calls[k]()
    return out


def test_home_shows_notification_with_two_buttons(svc, monkeypatch):
    m = svc
    monkeypatch.setattr(m, "gather", _gather_stub)
    monkeypatch.setattr(m, "_owner_alerts", lambda: [
        {"id": 3, "kind": "Notice", "at": "09-10 10:00", "message": "옛 알림함 줄"}])
    monkeypatch.setattr(m, "_notif_alerts", lambda: [
        {"id": 11, "status": "open", "read": False, "at": "09-11 10:00",
         "title": "고객 요청 미전파 — [맛·품질] 탄자국…", "message": "3일 넘게 안 갔어요",
         "occurrences": 3, "link": "/review"},
        {"id": 12, "status": "read", "read": True, "at": "09-11 10:00",
         "title": "고객 요청 미전파 — [포장] 빵칼…", "message": "", "occurrences": 1, "link": "/review"}])
    for name in ("meet_tasks", "_work_top_cached", "_briefs_cached"):
        if hasattr(m, name):
            monkeypatch.setattr(m, name, lambda *a, **k: [])

    html = m.app.test_client().get(f"/{KEY}/").get_data(as_text=True)

    assert "옛 알림함 줄" in html and f"/{KEY}/alert/3/ack" in html      # 기존 그대로
    assert "탄자국" in html and "3회 확인됨" in html
    assert f"/{KEY}/notice/11/read" in html and f"/{KEY}/notice/11/resolve" in html
    assert f"/{KEY}/notice/12/read" not in html                          # 읽은 건 [확인] 없음
    assert f"/{KEY}/notice/12/resolve" in html and "확인함" in html


def test_read_button_marks_read(svc, monkeypatch):
    m = svc
    done = []
    monkeypatch.setattr(m.notif, "mark_read", lambda nid: done.append(("read", nid)))
    r = m.app.test_client().post(f"/{KEY}/notice/11/read")
    assert r.status_code == 302 and done == [("read", 11)]


def test_resolve_button_marks_resolved(svc, monkeypatch):
    m = svc
    done = []
    monkeypatch.setattr(m.notif, "mark_resolved",
                        lambda nid, by="", reason="": done.append(("resolved", nid, reason)))
    r = m.app.test_client().post(f"/{KEY}/notice/11/resolve",
                                 headers={"X-Requested-With": "fetch"})   # _ajax() 규약
    assert r.status_code == 200 and r.get_json() == {"ok": True}
    assert done == [("resolved", 11, "manual")]


def test_owner_alerts_still_reads_error_log_only(svc, monkeypatch):
    """기존 알림함은 notifications 를 모른다 — 두 소스가 섞여 중복 표시되지 않는다."""
    m = svc
    seen = []
    monkeypatch.setattr(m.db, "get_errors", lambda only_unfixed=True, limit=100: (
        seen.append("error_log") or [{"id": 1, "kind": "Notice", "at": "2026-09-11T01:00:00+00:00",
                                      "message": "옛 줄"}]))
    monkeypatch.setattr(m.notif, "inbox", lambda **k: seen.append("notifications") or [])
    m._owner_alerts.cache_clear()
    rows = m._owner_alerts()
    assert [r["message"] for r in rows] == ["옛 줄"]
    assert seen == ["error_log"]


def test_notif_alerts_empty_when_table_missing(svc, monkeypatch):
    m = svc
    monkeypatch.setattr(m.notif, "available", lambda: False)
    m._notif_alerts.cache_clear()
    assert m._notif_alerts() == []
