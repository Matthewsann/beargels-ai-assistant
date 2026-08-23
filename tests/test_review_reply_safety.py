"""리뷰 답글 안전 계약 회귀 테스트 — 실고객 오게시 방지가 목적.

⚠️ 이 테스트는 브라우저/네트워크를 절대 건드리지 않는다. 실제 게시 경로
(_apply → 브라우저)는 승인+비-dry-run 에서만 도달하며, 여기서는 그 앞단
안전장치(dry-run·승인·에스컬레이션 차단)만 검증한다.
"""

import pytest

from crawler.review_reply import ReplyToReviewAction, ReplyPostError


GOOD_REVIEW = {
    "platform": "coupang", "review_no": "111", "author": "김손님",
    "rating": 5, "content": "베이글 짱맛! 또 올게요", "menus": ["플레인 베이글"],
    "order_count": 2, "reply_status": "none",
}
ESCALATION_REVIEW = {
    "platform": "baemin", "review_no": "222", "author": "박손님",
    "rating": 1, "content": "머리카락 나왔어요 환불해주세요", "reply_status": "none",
}


def _action(review, text="감사합니다! 또 뵙길 기다릴게요"):
    return ReplyToReviewAction(review, reply_text=text)


def test_preview_no_side_effects_returns_draft():
    a = _action(GOOD_REVIEW)
    p = a.preview()
    assert "감사합니다" in p and "김손님" in p
    # preview 는 브라우저를 열지 않는다(세션 미주입 상태로 문자열만 반환).
    assert a.session is None


def test_dry_run_never_posts():
    a = _action(GOOD_REVIEW)
    res = a.run(dry_run=True)
    assert res["applied"] is False and res["dry_run"] is True


def test_confirm_true_but_dry_run_still_no_post():
    # 승인(confirm)했어도 dry_run 이면 게시하지 않는다.
    a = _action(GOOD_REVIEW)
    res = a.run(confirm=True, dry_run=True)
    assert res["applied"] is False


def test_no_confirm_no_post_raises():
    # 승인 없이 실게시(dry_run=False) 시도 → 차단(PermissionError).
    a = _action(GOOD_REVIEW)
    with pytest.raises(PermissionError):
        a.run(confirm=False, dry_run=False)


def test_escalation_blocked_even_with_confirm():
    # 에스컬레이션 리뷰는 confirm+비-dry-run 이어도 _apply 초입에서 거부.
    # (브라우저 열기 전에 raise 되므로 네트워크 접촉 없음)
    a = ReplyToReviewAction(ESCALATION_REVIEW)  # draft 자동 생성
    with pytest.raises(ReplyPostError):
        a.run(confirm=True, dry_run=False)


def test_escalation_draft_is_a_guide_not_a_refusal():
    """민감 리뷰도 1차 가이드 초안은 준다(사장님 요청 2026-08-16).

    예전엔 '직접 대응 필요' 한 줄만 줘서 사장님이 맨손으로 써야 했다.
    이제 초안을 주되 **자동 게시는 여전히 막는다**(위 테스트가 그걸 지킨다).
    """
    draft = ReplyToReviewAction(ESCALATION_REVIEW).draft()
    assert not draft.startswith("⚠️")          # 거절 문구가 아니라
    assert len(draft) > 30                     # 실제로 쓸 수 있는 초안이고
    assert "사과" in draft or "죄송" in draft   # 사과로 시작하며
    # 사실 확인 전이라 보상·환불을 약속하면 안 된다.
    for banned in ("환불", "보상", "교환", "쿠폰", "고객센터"):
        assert banned not in draft, f"민감 리뷰 초안에 '{banned}' 가 들어감"


# --- 보상 약속 차단은 코드로 (모델을 믿지 않는다) ---------------------------
# 지시문에 "환불·보상 안내 금지"라고 써 뒀는데도 모델이 "환불은 고객센터로
# 접수해 주세요" 문장을 넣은 초안이 실제로 나왔다(2026-08-23). 모델을 바꿀
# 때마다 다시 터질 수 있으므로 코드로 잘라낸다.

def test_compensation_sentences_are_removed():
    from assistant.beargels import _drop_compensation
    out = _drop_compensation(
        "먼저 불편을 드려 죄송합니다. 즉시 점검하겠습니다.\n\n"
        "환불 관련 사항은 앱 내 고객센터로 접수해 주세요. 다시 사과드립니다.")
    for banned in ("환불", "보상", "교환", "쿠폰", "고객센터"):
        assert banned not in out
    assert "죄송합니다" in out and "점검하겠습니다" in out   # 사과·다짐은 남는다
    assert "다시 사과드립니다" in out                        # 같은 문단의 멀쩡한 문장도


def test_normal_reply_is_untouched():
    from assistant.beargels import _drop_compensation
    text = "맛있게 드셨다니 기뻐요! 또 오세요 🥯"
    assert _drop_compensation(text) == text
