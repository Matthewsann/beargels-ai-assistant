"""'답글 수정' 진행 상태 조회 회귀 테스트.

증상: 등록된 답글을 수정하면 3분 내내 '아직 확인이 안 돼요'만 반복
(사장님 제보 2026-08-13, 집 PC 는 켜져 있음).

원인: latest_review_job 은 잡을 **문구**로 찾는다(`리뷰 {id} …`). 그런데
일꾼이 '알 수 없는 잡 종류: post_edit — 일꾼 업데이트 필요' 처럼 리뷰 id 가
없는 문구로 끝내면 그 잡을 영영 못 찾아, 화면은 이유도 모른 채 빈 상태만
받는다.

계약: 요청이 만든 잡 id 로 조회하면 문구가 무엇이든 상태·사유가 보인다.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SERVICE_PATH", "testkey")
    import importlib

    import service.app as app_mod
    importlib.reload(app_mod)
    monkeypatch.setattr(app_mod, "_worker_view",
                        lambda: {"alive": True, "text": "대기 중"})
    return app_mod, app_mod.app.test_client()


def test_edit_post_returns_job_id(client, monkeypatch):
    app_mod, c = client
    monkeypatch.setattr(app_mod.db, "save_reply_draft",
                        lambda *a, **k: None)
    monkeypatch.setattr(app_mod.db, "request_post_edit",
                        lambda rid, by=None: {"id": 4242})

    r = c.post("/testkey/review/7/edit_post", data={"draft": "새 답글"})
    assert r.get_json() == {"ok": True, "job": 4242}


def test_state_uses_job_id_not_message_text(client, monkeypatch):
    """문구로는 못 찾는 잡도 id 로는 찾아 사유를 보여준다."""
    app_mod, c = client
    monkeypatch.setattr(app_mod.db, "get_job", lambda jid: {
        "id": jid, "status": "error",
        "message": "알 수 없는 잡 종류: post_edit — 일꾼 업데이트 필요"})
    monkeypatch.setattr(app_mod.db, "latest_review_job",
                        lambda *a: None)   # 문구 검색은 못 찾는 상황

    st = c.get("/testkey/review/7/edit_state?job=4242").get_json()
    assert st["status"] == "error"
    # 원문 대신 직원이 할 수 있는 말로 바꿔준다.
    assert "옛 버전" in st["message"] and "5_자동등록_고치기" in st["message"]


def test_state_falls_back_to_message_search(client, monkeypatch):
    """잡 id 가 없으면(옛 화면) 기존 방식으로도 동작해야 한다."""
    app_mod, c = client
    monkeypatch.setattr(app_mod.db, "latest_review_job",
                        lambda kind, rid: {"status": "done",
                                           "message": f"리뷰 {rid} 답글 수정 완료"})
    st = c.get("/testkey/review/7/edit_state").get_json()
    assert st["status"] == "done"


def test_state_reports_worker_alive(client, monkeypatch):
    """타임아웃 안내를 고르려면 화면이 일꾼 생존 여부를 알아야 한다."""
    app_mod, c = client
    monkeypatch.setattr(app_mod.db, "get_job", lambda jid: {"status": "pending",
                                                            "message": ""})
    st = c.get("/testkey/review/7/edit_state?job=1").get_json()
    assert st["worker_alive"] is True


# ---------------------------------------------------------------------------
# 답글 '등록' 실패 사유 노출 (2026-08-13 사장님 제보: '등록이 안 됐어요'만 뜸)
# ---------------------------------------------------------------------------

def test_post_reply_returns_job_id(client, monkeypatch):
    app_mod, c = client
    monkeypatch.setattr(app_mod.db, "mark_approved", lambda *a: None)
    monkeypatch.setattr(app_mod.db, "request_post",
                        lambda rid, by=None: {"id": 77})
    assert c.post("/testkey/review/5/post").get_json() == {"ok": True, "job": 77}


def test_draft_state_reports_failure_reason(client, monkeypatch):
    app_mod, c = client
    monkeypatch.setattr(app_mod.db, "get_review",
                        lambda rid: {"reply_status": "drafted"})
    monkeypatch.setattr(app_mod.db, "get_job", lambda jid: {
        "status": "error",
        "message": "CDP attach 실패(127.0.0.1:9222). launch_chrome.bat 확인"})
    st = c.get("/testkey/review/5/draft?job=77").get_json()
    assert st["reply_status"] == "drafted"
    assert "크롬" in st["why"]


@pytest.mark.parametrize("msg,expect", [
    ("알 수 없는 잡 종류: post — 일꾼 업데이트 필요", "0_업데이트.bat"),
    ("[리허설] WRITE_DRY_RUN=true", "연습 모드"),
    ("[쿠팡] 세션 만료 — 재로그인 필요.", "로그인이 풀렸"),
    ("에스컬레이션(민감) 리뷰는 자동 게시 불가", "사장님이 직접"),
    ("대상 배민 리뷰 카드를 찾지 못했습니다", "리뷰수집"),
])
def test_reason_translations(client, msg, expect):
    app_mod, _ = client
    assert expect in app_mod._why_post_failed(msg)


def test_no_message_no_reason(client):
    app_mod, _ = client
    assert app_mod._why_post_failed(None) == ""
