"""목표 감사(2026-08-29)에서 확정된 고침들의 회귀 테스트.

CLAUDE.md '리뷰 답글 페이지의 목표' — 10건 4.5분의 내역이 큐 대기 15.3초
+ 실행 11.7초/건이었고, 실행의 70%는 통짜 sleep, 큐 대기의 전부는 일꾼의
15초 낮잠이었다. 그리고 배민은 '등록'을 누른 뒤 실제로 달렸는지 보지 않았다.
여기 테스트는 그 고침들이 되돌아가지 않게 지킨다.
"""

import inspect

import pytest


# --- 배민: 통짜 sleep 정리 (실행 11.7초 → ~6초) ---------------------------

def test_apply_baemin_waits_on_conditions_not_the_clock():
    """페이지를 기다릴 땐 조건 대기 — 시계 대기(sleep)는 사람 리듬 구간만.

    남겨야 하는 human_pause 는 두 곳뿐이다: 카드 스크롤 정착(0.3~0.6)과
    제출 직전의 뜸(0.5~1.0). 나머지(진입 2~3초, Escape 뒤 1초, 작성기 열림
    뒤 1.15초, 제출 뒤 2.3초)는 클릭도 입력도 없는 순수 대기라 걷어냈다.
    """
    from crawler.review_reply import ReplyToReviewAction
    src = inspect.getsource(ReplyToReviewAction._apply_baemin)
    assert "wait_for_selector" in src, "진입은 카드가 뜰 때까지 조건 대기"
    # 정상 경로 2곳(스크롤 정착·제출 직전)만 남는다
    assert src.count("human_pause(") <= 2, (
        "human_pause 가 다시 늘었다 — 등록 1건이 도로 느려진다")
    assert "human_pause(2.0, 3.0)" not in src
    assert "human_pause(1.8, 2.8)" not in src
    assert "human_pause(0.8, 1.5)" not in src


def test_apply_baemin_verifies_the_reply_actually_landed():
    """'등록'을 누른 뒤 새 본문이 카드에 달렸는지 확인해야 성공이다.

    예전엔 무조건 성공을 반환해, 조용한 클릭 실패가 posted 로 집계되고
    카드가 사라졌다 — 30일 기한이 지나면 영영 못 단다(감사 확정, HIGH).
    """
    from crawler.review_reply import ReplyToReviewAction
    src = inspect.getsource(ReplyToReviewAction._apply_baemin)
    assert "_baemin_wait_posted" in src
    # 확인 실패는 예외로 — agent 가 drafted 로 되돌려 카드를 되살린다
    assert "나타나지 않았" in src


def test_wait_posted_matches_new_body_not_just_any_box(monkeypatch):
    """'답글박스가 있다'로는 안 된다 — 수정 경로에선 옛 답글이 이미 있다."""
    from crawler import review_reply as rr
    monkeypatch.setattr(rr, "_baemin_reply_texts",
                        lambda page, rid: ["사장님 8월 29일 옛날에 달린 다른 답글입니다"])
    assert rr._baemin_wait_posted(None, "123", "새로 쓴 답글 본문이에요",
                                  timeout_s=0.5) is False

    monkeypatch.setattr(rr, "_baemin_reply_texts",
                        lambda page, rid: ["사장님 8월 29일 새로 쓴 답글 본문이에요 감사합니다"])
    assert rr._baemin_wait_posted(None, "123", "새로 쓴   답글\n본문이에요") is True


def test_wait_posted_ignores_whitespace_differences(monkeypatch):
    """화면 텍스트는 줄바꿈이 끼므로 공백 무시로 비교한다."""
    from crawler import review_reply as rr
    monkeypatch.setattr(rr, "_baemin_reply_texts",
                        lambda page, rid: ["사장님\n답글  본문\n감사합니다"])
    assert rr._baemin_wait_posted(None, "1", "답글 본문 감사합니다") is True


# --- 일꾼: 두 박자 루프 (큐 대기 15.3초 → ~1초) ---------------------------

def test_claim_next_job_has_a_light_interactive_mode():
    """빠른 박자용 — 직원 잡만 보는 조회 1회짜리 모드가 있어야 한다."""
    from database import supabase_client as db
    sig = inspect.signature(db.claim_next_job)
    assert "interactive_only" in sig.parameters
    src = inspect.getsource(db.claim_next_job)
    # interactive_only 면 두 번째(전체) 조회로 내려가지 않는다
    assert "not interactive_only" in src


# --- 화면: 저장 실패가 옛 초안 게시로 이어지지 않게 -----------------------

@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("SERVICE_PATH", "testkey")
    import service.app as app_mod
    monkeypatch.setattr(app_mod, "SERVICE_PATH", "testkey")
    return app_mod, app_mod.app.test_client()


def test_save_draft_reports_failure_to_js(client, monkeypatch):
    """저장이 실패하면 JS 호출엔 {ok: false} — 성공 흉내를 내면 안 된다."""
    app_mod, c = client

    def boom(review_id, text):
        raise RuntimeError("DB 죽음")

    monkeypatch.setattr(app_mod.db, "save_reply_draft", boom)
    monkeypatch.setattr(app_mod.db, "log_error", lambda *a, **k: None)
    r = c.post("/testkey/review/1/save", data={"draft": "고친 답글"},
               headers={"X-Requested-With": "fetch"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is False


def test_save_draft_success_is_json_for_js(client, monkeypatch):
    """성공도 JSON — 302 를 따라가 /todo 100장을 다시 그리던 낭비 제거."""
    app_mod, c = client
    monkeypatch.setattr(app_mod.db, "save_reply_draft", lambda rid, t: None)
    r = c.post("/testkey/review/1/save", data={"draft": "고친 답글"},
               headers={"X-Requested-With": "fetch"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_save_draft_keeps_redirect_for_plain_forms(client, monkeypatch):
    """JS 가 아닌 평범한 폼 전송은 예전처럼 리다이렉트(비-JS 폴백)."""
    app_mod, c = client
    monkeypatch.setattr(app_mod.db, "save_reply_draft", lambda rid, t: None)
    r = c.post("/testkey/review/1/save", data={"draft": "고친 답글"})
    assert r.status_code == 302


def test_screen_stops_posting_when_save_fails():
    """submitPost·scheduleOne 은 저장 성공을 확인한 뒤에만 다음으로 간다."""
    import pathlib
    html = pathlib.Path("service/templates/staff.html").read_text(encoding="utf-8")
    # saveDraft 는 성공 여부(boolean)를 돌려주고, 실패를 삼키지 않는다
    assert ".catch(() => false)" in html
    assert "savedDraft[id] = val" in html          # 안 바뀐 값은 재저장 안 함
    # 등록·예약 둘 다 저장 실패면 멈춘다
    assert "저장되지 않아 등록을 멈췄어요" in html
    assert "저장되지 않았어요" in html


# --- 죽은 정시 일괄 경로 제거 ---------------------------------------------

def test_dead_auto_post_path_stays_dead():
    """run_auto_post 는 지웠다 — 되켜면 잡 큐 중복방지를 우회해 같은 리뷰에
    답글이 두 번 달릴 수 있었다(2026-08-27 사고와 같은 유형)."""
    from worker import agent
    for name in ("run_auto_post", "AUTO_POST_TIMES", "post_slot_due",
                 "maybe_auto_post"):
        assert not hasattr(agent, name), name


# --- 웹앱은 anthropic 패키지 없이도 떠야 한다 (2026-08-31 실장애) ----------
# PythonAnywhere 에는 anthropic 이 안 깔려 있다. assistant/beargels.py 의
# 최상단 `from anthropic import Anthropic`(llm.py 이전의 잔재) 때문에
# '등록해야 할 답글' 화면이 통째로 죽었다: "No module named 'anthropic'".

def test_beargels_has_no_module_level_anthropic_import():
    """AI 호출은 llm.py 한 곳으로만 — 이 모듈은 웹앱도 import 한다."""
    import pathlib
    src = pathlib.Path("assistant/beargels.py").read_text(encoding="utf-8")
    for line in src.splitlines():
        stripped = line.strip()
        assert not stripped.startswith(("import anthropic", "from anthropic")), (
            "beargels 최상단에서 anthropic 을 import 하면 웹앱 화면이 죽는다")


def test_web_app_survives_without_anthropic(monkeypatch):
    """anthropic 이 없어도 beargels·service.app 이 import 되고 배지 계산도 산다."""
    import builtins
    import importlib

    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "anthropic" or name.startswith("anthropic."):
            raise ModuleNotFoundError("No module named 'anthropic'")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    import assistant.beargels as B
    importlib.reload(B)                      # anthropic 없는 상태로 다시 읽기

    import service.app as app_mod
    monkeypatch.setattr(app_mod.db, "edit_rate_by_kind",
                        lambda: {"question": {"n": 50, "rate": 0.0},
                                 "rating_only": {"n": 50, "rate": 0.0}})
    app_mod._trusted_kinds.cache_clear()
    got = app_mod._trusted_kinds()
    app_mod._trusted_kinds.cache_clear()
    # 죽지 않고, 민감 유형(question)은 여전히 배지에서 빠진다
    assert "question" not in got
    assert "rating_only" in got


# --- 실제 리뷰 확인 버튼 + 끊긴 초안 경고 (2026-08-31 사장님 요청) --------

def test_card_exposes_a_key_to_find_the_review_on_the_platform():
    """배민·쿠팡 모두 리뷰 하나만 여는 주소가 없다(배민은 URL 파라미터도 안
    받는 SPA — 2026-08-31 실측). 그래서 목록을 열면서 '찾을 열쇠'를 준다:
    배민=리뷰번호(카드에 찍혀 있다), 쿠팡=작성자(번호가 화면에 없다)."""
    import service.app as app
    bm = app._review_view({"platform": "baemin", "review_no": "2026082802870907",
                           "author": "파스타1인분장인", "reply_draft": "초안"})
    assert bm["find_key"] == "2026082802870907"
    cp = app._review_view({"platform": "coupang", "review_no": "9999",
                           "author": "김*슬", "reply_draft": "초안"})
    assert cp["find_key"] == "김*슬", "쿠팡은 리뷰번호가 화면에 없어 이름으로 찾는다"


def test_review_link_button_is_on_the_card():
    import pathlib
    html = pathlib.Path("service/templates/staff.html").read_text(encoding="utf-8")
    assert "🔍 실제 리뷰" in html
    assert "copyFindKey" in html
    assert 'target="_blank"' in html


def test_cut_sentence_warning_ignores_the_greeting_line():
    """호칭 줄('백*아님,')은 쉼표로 끝나는 게 정상 — 여기에 경보가 뜨면
    멀쩡한 초안 대부분이 경고를 달고, 진짜 끊긴 글을 대충 넘기게 된다."""
    import pathlib
    html = pathlib.Path("service/templates/staff.html").read_text(encoding="utf-8")
    assert "checkCut" in html and "CUT_TAIL" in html
    # 호칭 줄 제외 규칙이 살아 있어야 한다
    assert "님\s*[,·]?$" in html
