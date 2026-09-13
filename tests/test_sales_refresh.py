"""경영 대시보드 '장부 최신화' 버튼 (사장님 요청 2026-09-13).

왜: 장부 시트는 하루 1회(10:20) 자동 반영인데, 그게 막혀 있는 동안 사장님이
CSV 를 폴더에 넣어도 다음 날까지 화면이 안 바뀐다. 버튼 하나로 집 PC 일꾼에게
'지금 읽어' 하고, 끝나면 화면을 다시 그린다.

계약:
  · 요청은 화면 안 토큰(t)이 있어야 한다 — 없으면 401 (목표 저장과 같은 규칙)
  · 요청은 ledger_sync 잡 하나를 만들고 job_id 를 돌려준다
  · 상태 조회도 토큰이 있어야 한다 — 없으면 404 (화면이 있다는 것도 안 알림)
  · 일꾼은 그 잡을 받으면 장부를 읽고, 결과 문장을 잡에 남긴다
    (문장에 '실패'가 있으면 error, 아니면 done)
  · ledger_sync 는 사람이 기다리는 잡이라 빠른 폴링 대상이다
"""
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "service"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

KEY, PW = "testkey", "080808"


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setenv("SERVICE_PATH", KEY)
    monkeypatch.setenv("OWNER_KEY", PW)
    import service.app as m
    importlib.reload(m)
    m.app.testing = True
    return m


# ── 요청 ──────────────────────────────────────────────────────────────

def test_토큰_없이는_요청_못_한다(svc):
    r = svc.app.test_client().post(f"/{KEY}/sales/refresh", json={})
    assert r.status_code == 401


def test_토큰이_맞으면_잡을_만들고_id를_준다(svc, monkeypatch):
    made = {}
    monkeypatch.setattr(svc.ledger_store, "request_sync",
                        lambda by=None: made.setdefault("job", {"id": 77, "by": by}))
    r = svc.app.test_client().post(f"/{KEY}/sales/refresh",
                                   json={"t": svc._nav_token(PW)})
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "job_id": 77}
    assert made["job"]["by"] == "sales"


def test_잡_생성이_죽어도_화면은_이유를_받는다(svc, monkeypatch):
    def boom(by=None):
        raise RuntimeError("DB 끊김")
    monkeypatch.setattr(svc.ledger_store, "request_sync", boom)
    monkeypatch.setattr(svc.db, "log_error", lambda *a, **k: None)
    r = svc.app.test_client().post(f"/{KEY}/sales/refresh",
                                   json={"t": svc._nav_token(PW)})
    assert r.status_code == 500 and "DB 끊김" in r.get_json()["error"]


# ── 상태 조회 ───────────────────────────────────────────────────────────

def test_토큰_없이는_상태도_못_본다(svc):
    assert svc.app.test_client().get(f"/{KEY}/sales/refresh/77").status_code == 404


@pytest.mark.parametrize("job,expect", [
    ({"id": 77, "status": "pending", "message": ""}, {"status": "pending", "message": ""}),
    ({"id": 77, "status": "done", "message": "장부 시트 11개월 반영 [폴더 CSV(장부.csv)]"},
     {"status": "done", "message": "장부 시트 11개월 반영 [폴더 CSV(장부.csv)]"}),
    ({"id": 77, "status": "error", "message": "장부 시트 실패: 인증 파일이 없습니다"},
     {"status": "error", "message": "장부 시트 실패: 인증 파일이 없습니다"}),
    (None, {"status": "none"}),
])
def test_상태와_결과_문장을_그대로_준다(svc, monkeypatch, job, expect):
    monkeypatch.setattr(svc.db, "get_job", lambda jid: job)
    r = svc.app.test_client().get(f"/{KEY}/sales/refresh/77?t={svc._nav_token(PW)}")
    assert r.status_code == 200 and r.get_json() == expect


# ── 일꾼 ──────────────────────────────────────────────────────────────

@pytest.fixture
def worker(monkeypatch):
    import worker.agent as ag
    calls = {"finish": [], "ping": []}
    monkeypatch.setattr(ag.db, "finish_job", lambda *a, **k: calls["finish"].append((a, k)))
    monkeypatch.setattr(ag.db, "worker_ping", lambda *a, **k: calls["ping"].append(a))
    monkeypatch.setattr(ag.db, "log_error", lambda *a, **k: None)
    return ag, calls


def test_일꾼은_읽고_결과_문장을_잡에_남긴다(worker, monkeypatch):
    ag, calls = worker
    monkeypatch.setattr(ag, "_sync_ledger_sheet",
                        lambda notify=True: "장부 시트 11개월 반영 [폴더 CSV(장부.csv)]")
    ag.run_ledger_sync_job({"id": 77})
    (jid, status, msg, n), _ = calls["finish"][0]
    assert (jid, status, n) == (77, "done", 1) and "11개월 반영" in msg
    assert calls["ping"][-1][0] == "idle"          # 끝나면 대기로 돌아간다


def test_읽기_실패면_error로_남기고_이유를_준다(worker, monkeypatch):
    ag, calls = worker
    monkeypatch.setattr(ag, "_sync_ledger_sheet",
                        lambda notify=True: "장부 시트 실패: 인증 파일이 없습니다")
    ag.run_ledger_sync_job({"id": 77})
    (jid, status, msg, n), _ = calls["finish"][0]
    assert (status, n) == ("error", 0) and "인증" in msg


def test_예외가_터져도_잡은_닫힌다(worker, monkeypatch):
    ag, calls = worker
    def boom(notify=True):
        raise RuntimeError("갑자기")
    monkeypatch.setattr(ag, "_sync_ledger_sheet", boom)
    ag.run_ledger_sync_job({"id": 77})
    (jid, status, msg, n), _ = calls["finish"][0]
    assert status == "error" and "갑자기" in msg
    assert calls["ping"][-1][0] == "idle"


def test_버튼_경로는_홈_알림함에_안_쌓는다(worker, monkeypatch):
    """누를 때마다 같은 알림이 하나씩 늘면 안 된다 — 결과는 화면이 보여준다."""
    ag, calls = worker
    notified = []
    monkeypatch.setattr(ag, "notify_owner", lambda *a, **k: notified.append(a))
    import worker.ledger_sheet as lsh
    def blocked():
        raise RuntimeError("인증 파일이 없습니다: token.json")
    monkeypatch.setattr(lsh, "sync", blocked)
    ag.run_ledger_sync_job({"id": 77})
    (jid, status, msg, n), _ = calls["finish"][0]
    assert status == "error" and "로그인" in msg and "장부관리 폴더" in msg
    assert notified == []


def test_자동_반영_경로는_여전히_알린다(worker, monkeypatch):
    ag, calls = worker
    notified = []
    monkeypatch.setattr(ag, "notify_owner", lambda *a, **k: notified.append(a))
    import worker.ledger_sheet as lsh
    def blocked():
        raise RuntimeError("인증 파일이 없습니다: token.json")
    monkeypatch.setattr(lsh, "sync", blocked)
    ag._sync_ledger_sheet()          # 기본값 notify=True — 하루 1회 자동 반영
    assert len(notified) == 1 and "장부 자동 반영 실패" in notified[0][0]


def test_run_job이_ledger_sync를_알아본다(worker, monkeypatch):
    ag, calls = worker
    hit = {}
    monkeypatch.setattr(ag, "run_ledger_sync_job", lambda job: hit.setdefault("job", job))
    ag.run_job({"id": 77, "kind": "ledger_sync"})
    assert hit["job"]["id"] == 77


def test_사람이_기다리는_잡이라_빠른_폴링_대상이다():
    from database import supabase_client as db
    assert "ledger_sync" in db.INTERACTIVE_JOB_KINDS
