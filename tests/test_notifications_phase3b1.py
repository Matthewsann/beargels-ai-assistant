"""Notification Layer Phase 3-B-1 — 알림함 한 벌, 다섯 화면 (2026-09-11).

계약:
  · _inbox() 가 error_log 알림(e:<id>)과 notifications(n:<id>)를 한 목록으로, 최근 순
  · 같은 숫자 id 가 양쪽에 있어도 uid 가 다르고 각자 제 라우트로만 간다
      e: [확인] → /alert/<id>/ack (mark_error_fixed)
      n: [확인] → /notice/<id>/read · [처리됨] → /notice/<id>/resolve
  · 읽은 알림은 [확인] 없이 [처리됨]만 남기고 계속 보인다 · resolved 는 사라진다
  · 다섯 화면이 같은 _inbox.html 을 include 하고, 예전 alerts 루프는 남아 있지 않다
  · [확인] 뒤 15초 캐시가 닫은 줄을 붙들지 않는다

DB·네트워크 불필요.
"""
import importlib
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "service", ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from test_notifications_phase2 import _Store  # noqa: E402

KEY = "testkey"
TEMPLATES = ROOT / "service" / "templates"
FIVE = ("home.html", "dashboard.html", "staff.html", "work.html", "care.html")


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setenv("SERVICE_PATH", KEY)
    import service.app as m
    importlib.reload(m)
    m.app.testing = True
    return m


def _legacy(i, at="2026-09-11T01:00:00+00:00", msg="옛 알림"):
    return {"id": i, "kind": "Notice", "at": at, "message": msg}


def _notif(i, ts="2026-09-11T02:00:00+00:00", status="open", title="새 알림", occ=1):
    return {"id": i, "status": status, "last_seen_at": ts, "created_at": ts,
            "title": title, "message": "본문", "occurrences": occ, "link": "/review"}


def _wire(m, monkeypatch, legacy=(), notifs=()):
    monkeypatch.setattr(m.db, "get_errors", lambda only_unfixed=True, limit=100: list(legacy))
    monkeypatch.setattr(m.notif, "available", lambda: True)
    monkeypatch.setattr(m.notif, "inbox", lambda **k: list(notifs))
    m._owner_alerts.cache_clear()
    m._notif_alerts.cache_clear()


# ── Test 1·2·3: 두 저장소가 한 목록에, 충돌 없이 ──────────────────────────────

def test_legacy_alert_appears(svc, monkeypatch):
    m = svc
    _wire(m, monkeypatch, legacy=[_legacy(3)])
    rows = m._inbox()
    assert [r["uid"] for r in rows] == ["e:3"]
    assert rows[0]["title"] == "옛 알림" and rows[0]["read"] is False


def test_notification_appears(svc, monkeypatch):
    m = svc
    _wire(m, monkeypatch, notifs=[_notif(7, occ=3)])
    rows = m._inbox()
    assert [r["uid"] for r in rows] == ["n:7"]
    assert rows[0]["occurrences"] == 3 and rows[0]["message"] == "본문"


def test_same_numeric_id_in_both_sources_does_not_collide(svc, monkeypatch):
    m = svc
    _wire(m, monkeypatch, legacy=[_legacy(5)], notifs=[_notif(5)])
    rows = m._inbox()
    assert sorted(r["uid"] for r in rows) == ["e:5", "n:5"]
    assert {r["src"] for r in rows} == {"e", "n"}


def test_sorted_by_last_seen_newest_first_across_sources(svc, monkeypatch):
    m = svc
    _wire(m, monkeypatch,
          legacy=[_legacy(1, at="2026-09-11T03:00:00+00:00"), _legacy(2, at="2026-09-10T03:00:00+00:00")],
          notifs=[_notif(9, ts="2026-09-11T02:30:00+00:00"), _notif(8, ts="2026-09-11T04:00:00Z")])
    assert [r["uid"] for r in m._inbox()] == ["n:8", "e:1", "n:9", "e:2"]


def test_visible_limit_stays_five(svc, monkeypatch):
    m = svc
    _wire(m, monkeypatch, legacy=[_legacy(i) for i in range(1, 5)],
          notifs=[_notif(i) for i in range(1, 5)])
    assert len(m._inbox()) == 5


# ── Test 4·5·6·10: 버튼이 제 라우트로만 간다 ─────────────────────────────────

def _render_inbox(m, items):
    with m.app.test_request_context(f"/{KEY}/"):
        return m.render_template("_inbox.html", inbox=items, key=KEY)


def _blocks(html):
    """data-uid 별 HTML 조각."""
    parts = re.split(r'(?=<div class="alertbox[^"]*" data-uid=")', html)
    return {re.search(r'data-uid="([^"]+)"', p).group(1): p
            for p in parts if 'data-uid="' in p}


def test_buttons_route_by_source_never_cross(svc, monkeypatch):
    m = svc
    _wire(m, monkeypatch, legacy=[_legacy(5)], notifs=[_notif(5)])
    html = _render_inbox(m, m._inbox())
    b = _blocks(html)
    assert set(b) == {"e:5", "n:5"}
    assert f"/{KEY}/alert/5/ack" in b["e:5"] and "/notice/" not in b["e:5"]
    assert f"/{KEY}/notice/5/read" in b["n:5"] and f"/{KEY}/notice/5/resolve" in b["n:5"]
    assert "/alert/" not in b["n:5"]                      # 알림 id 가 error_log 라우트로 안 간다
    assert html.count("/alert/") == 1 and html.count("/notice/") == 2


def test_legacy_confirm_calls_mark_error_fixed_and_refreshes(svc, monkeypatch):
    m = svc
    fixed = []
    monkeypatch.setattr(m.db, "mark_error_fixed", lambda i, note=None: fixed.append((i, note)))
    rows = [_legacy(3)]
    monkeypatch.setattr(m.db, "get_errors", lambda only_unfixed=True, limit=100: list(rows))
    m._owner_alerts.cache_clear()
    assert [a["id"] for a in m._owner_alerts()] == [3]      # 캐시에 담김
    rows.clear()                                             # 서버에서는 닫혔다
    r = m.app.test_client().post(f"/{KEY}/alert/3/ack")
    assert r.status_code == 302 and fixed == [(3, "사장님이 화면에서 확인")]
    assert m._owner_alerts() == []                           # 캐시가 비워져 바로 사라진다


def test_notification_read_and_resolve_routes(svc, monkeypatch):
    m = svc
    calls = []
    monkeypatch.setattr(m.notif, "mark_read", lambda nid: calls.append(("read", nid)))
    monkeypatch.setattr(m.notif, "mark_resolved",
                        lambda nid, by="", reason="": calls.append(("resolved", nid, reason)))
    c = m.app.test_client()
    assert c.post(f"/{KEY}/notice/5/read").status_code == 302
    assert c.post(f"/{KEY}/notice/5/resolve").status_code == 302
    assert calls == [("read", 5), ("resolved", 5, "manual")]


# ── Test 7·8: 읽음은 남고, 처리됨은 사라진다 (실제 저장 로직으로) ────────────

@pytest.fixture
def ns(monkeypatch):
    from database import notification_store as m
    store = _Store()
    monkeypatch.setattr(m, "get_client", lambda: store)
    m._reset_cache()
    return m, store


def test_read_notification_stays_visible_without_confirm_button(svc, ns, monkeypatch):
    m = svc
    nsm, store = ns
    nsm.record("request.unshared", "request.unshared:review:1", "요청", "")
    nsm.mark_read(store.tables["notifications"][0]["id"])
    monkeypatch.setattr(m.db, "get_errors", lambda only_unfixed=True, limit=100: [])
    m._owner_alerts.cache_clear(); m._notif_alerts.cache_clear()
    rows = m._inbox()
    assert len(rows) == 1 and rows[0]["read"] is True
    html = _render_inbox(m, rows)
    assert "확인함" in html and "/notice/1/resolve" in html and "/notice/1/read" not in html


def test_resolved_notification_disappears(svc, ns, monkeypatch):
    m = svc
    nsm, store = ns
    nsm.record("request.unshared", "request.unshared:review:1", "요청", "")
    nsm.mark_resolved(store.tables["notifications"][0]["id"], reason="manual")
    monkeypatch.setattr(m.db, "get_errors", lambda only_unfixed=True, limit=100: [])
    m._owner_alerts.cache_clear(); m._notif_alerts.cache_clear()
    assert m._inbox() == []


# ── Test 9: 한 벌, 다섯 화면 ─────────────────────────────────────────────────

def test_five_templates_share_one_inbox_partial():
    for name in FIVE:
        src = (TEMPLATES / name).read_text(encoding="utf-8")
        assert '{% include "_inbox.html" %}' in src, f"{name} 이 _inbox.html 을 안 쓴다"
        assert "for a in alerts" not in src and "nalerts" not in src, f"{name} 에 옛 알림 루프가 남았다"
    partial = (TEMPLATES / "_inbox.html").read_text(encoding="utf-8")
    assert "ack_alert" in partial and "notice_read" in partial and "notice_resolve" in partial


def test_home_and_care_render_the_shared_inbox(svc, monkeypatch):
    m = svc
    _wire(m, monkeypatch, legacy=[_legacy(3)], notifs=[_notif(7, occ=2)])
    # 홈 — gather 를 건너뛰고 inbox 만 채운다
    def gather_stub(**calls):
        out = {k: None for k in calls}
        out["inbox"] = calls["inbox"]()
        return out
    monkeypatch.setattr(m, "gather", gather_stub)
    for name in ("_work_top_cached", "_briefs_cached"):
        if hasattr(m, name):
            monkeypatch.setattr(m, name, lambda *a, **k: [])
    home = m.app.test_client().get(f"/{KEY}/").get_data(as_text=True)
    assert 'data-uid="e:3"' in home and 'data-uid="n:7"' in home and "2회 확인됨" in home
    # 문제 리뷰 화면 — 직접 호출 경로
    monkeypatch.setattr(m.db, "get_attention_reviews", lambda **k: ([], 0))
    monkeypatch.setattr(m, "_tab_counts", lambda: {"todo": 0, "prob": 0, "hist": 0, "all": 0})
    care = m.app.test_client().get(f"/{KEY}/care").get_data(as_text=True)
    assert 'data-uid="e:3"' in care and 'data-uid="n:7"' in care
