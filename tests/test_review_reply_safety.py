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


def test_escalation_draft_is_manual_notice():
    a = ReplyToReviewAction(ESCALATION_REVIEW)
    assert a.draft().startswith("⚠️")
