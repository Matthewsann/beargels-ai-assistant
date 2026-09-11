"""Notification Layer Phase 3-A — 고객 요청 [공유 완료]가 알림을 자동으로 닫는다 (2026-09-11).

계약:
  · 미전파 요청 → request.unshared:review:<reviews.id> 한 줄, status open
  · 같은 요청 재감지 → 새 줄 없이 occurrences+1
  · POST /requests/shared (기존 '공유 완료' 라우트) → 그 요청의 열린 알림이 resolved
      resolved_by='직원웹(공유 완료)', resolve_reason='request_shared', resolved_at 기록
  · resolved 된 뒤 같은 요청이 또 잡혀도 새 줄 없음, 되살리지 않음(Phase 2 규칙 유지)
  · 다른 요청은 별도 줄
  · 알림 쪽이 실패해도 공유 완료(kv request_shared) 자체는 예전처럼 성공
  · 표가 없으면 resolve_by_key 는 조용히 None

실DB 검증은 이 파일이 아니라 Phase 3-A 자가진단으로 했다(행 만들고 지움).
여기서는 메모리 표(test_notifications_phase2._Store)로 같은 코드 경로를 돈다.
"""
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "service", ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from test_notifications_phase2 import _Store  # noqa: E402  — 같은 가짜 표

KEY = "testkey"


@pytest.fixture
def ns(monkeypatch):
    from database import notification_store as m
    store = _Store()
    monkeypatch.setattr(m, "get_client", lambda: store)
    m._reset_cache()
    return m, store


def _detect(m, rid, quote="탄자국이 있어서 아쉬워요"):
    """maybe_request_nag 가 부르는 것과 같은 모양으로 알림을 낸다."""
    import alerts
    return alerts.notify(event_type="request.unshared",
                         dedupe_key=f"request.unshared:review:{rid}",
                         title=f"고객 요청 미전파 — [맛·품질] {quote[:24]}…",
                         message="3일 넘게 단톡방에 안 갔어요.",
                         source="worker", source_ref=f"review:{rid}")


# ── Test 1·2·5 ────────────────────────────────────────────────────────────────

def test_unshared_request_opens_one_notification(ns):
    m, store = ns
    row, outcome = _detect(m, 1734)
    assert outcome == "created"
    assert row["dedupe_key"] == "request.unshared:review:1734"
    assert row["status"] == "open" and row["severity"] == "high"    # 정책표 기본값
    assert row["link"] == "/review" and row["recipient_id"] == "owner"


def test_same_request_again_bumps_occurrences(ns):
    m, store = ns
    _detect(m, 1734)
    row, outcome = _detect(m, 1734)
    assert outcome == "updated" and row["occurrences"] == 2
    assert len(store.tables["notifications"]) == 1


def test_different_request_gets_own_row(ns):
    m, store = ns
    _detect(m, 1734)
    _detect(m, 1735)
    keys = sorted(r["dedupe_key"] for r in store.tables["notifications"])
    assert keys == ["request.unshared:review:1734", "request.unshared:review:1735"]


# ── Test 3·4: 공유 완료 → resolved, 되살리지 않음 ─────────────────────────────

def test_resolve_by_key_closes_open_row(ns):
    m, store = ns
    _detect(m, 1734)
    row = m.resolve_by_key("request.unshared:review:1734",
                           by="직원웹(공유 완료)", reason="request_shared")
    assert row["status"] == "resolved"
    assert row["resolved_by"] == "직원웹(공유 완료)"
    assert row["resolve_reason"] == "request_shared"
    assert row["resolved_at"]
    assert len(store.tables["notifications"]) == 1                   # 새 줄 없음


def test_resolve_by_key_is_noop_when_nothing_open(ns):
    m, store = ns
    assert m.resolve_by_key("request.unshared:review:404") is None
    _detect(m, 1)
    m.resolve_by_key("request.unshared:review:1", reason="request_shared")
    assert m.resolve_by_key("request.unshared:review:1", reason="request_shared") is None
    assert store.tables["notifications"][0]["resolve_reason"] == "request_shared"  # 안 덮어씀


def test_resolved_row_stays_resolved_on_redetection(ns):
    m, store = ns
    _detect(m, 1734)
    m.resolve_by_key("request.unshared:review:1734", reason="request_shared")
    row, outcome = _detect(m, 1734)
    assert outcome == "kept_closed"
    assert row["status"] == "resolved" and row["resolve_reason"] == "request_shared"
    assert len(store.tables["notifications"]) == 1


def test_resolve_by_key_quiet_when_table_missing(monkeypatch):
    from database import notification_store as m
    monkeypatch.setattr(m, "get_client", lambda: _Store(with_table=False))
    m._reset_cache()
    assert m.resolve_by_key("request.unshared:review:1", reason="request_shared") is None


# ── 라우트: 기존 '공유 완료'가 알림도 닫는다 · 알림이 실패해도 공유는 된다 ──

@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setenv("SERVICE_PATH", KEY)
    import service.app as m
    importlib.reload(m)
    m.app.testing = True
    kv = {}
    monkeypatch.setattr(m.db, "get_setting", lambda k, d=None: kv.get(k, d))
    monkeypatch.setattr(m.db, "menu_set_setting", lambda k, v: kv.__setitem__(k, v))
    return m, kv


def test_shared_route_resolves_matching_notifications(svc, monkeypatch):
    m, kv = svc
    resolved = []
    monkeypatch.setattr(m.notif, "resolve_by_key",
                        lambda key, by="", reason="": resolved.append((key, by, reason)))
    r = m.app.test_client().post(f"/{KEY}/requests/shared", json={"ids": [1734, 1735]})
    assert r.get_json() == {"ok": True, "count": 2}
    assert kv["request_shared"] == [1734, 1735]                      # 예전 동작 그대로
    assert sorted(resolved) == [
        ("request.unshared:review:1734", "직원웹(공유 완료)", "request_shared"),
        ("request.unshared:review:1735", "직원웹(공유 완료)", "request_shared")]


def test_shared_route_survives_notification_failure(svc, monkeypatch):
    m, kv = svc
    logged = []
    monkeypatch.setattr(m.notif, "resolve_by_key",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("DB 잠깐 끊김")))
    monkeypatch.setattr(m.db, "log_error", lambda *a, **k: logged.append(k.get("kind")))
    r = m.app.test_client().post(f"/{KEY}/requests/shared", json={"ids": [7]})
    assert r.get_json() == {"ok": True, "count": 1}
    assert kv["request_shared"] == [7]
    assert logged == ["RuntimeError"]                                # 조용히 삼키지 않고 기록


def test_shared_route_still_rejects_empty(svc):
    m, kv = svc
    r = m.app.test_client().post(f"/{KEY}/requests/shared", json={"ids": []})
    assert r.get_json()["ok"] is False and "request_shared" not in kv
