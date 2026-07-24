"""리뷰 분류 회귀 테스트 — 특히 음식리뷰 '고소'(savory) 오탐 방지."""

import pytest

from assistant.beargels import classify_review


def cr(content, rating=5):
    return classify_review({"content": content, "rating": rating})


@pytest.mark.parametrize("text", [
    "서비스로 주신 러스크도 고소하고 넘 맛있었어요",
    "빵이 고소한 맛이 일품이라 자꾸 손이 가요 정말 좋아요",
    "고소해서 계속 먹게 되는 베이글",
])
def test_savory_gososo_not_escalated(text):
    # '고소'(savory)는 흔한 칭찬 → 에스컬레이션 아님.
    assert cr(text, 5) != "escalate"


@pytest.mark.parametrize("text,rating", [
    ("고소하겠습니다 환불 안해주면", 1),
    ("법적 대응 하겠습니다", 2),
    ("머리카락 나왔어요", 1),
    ("이물질 나왔습니다 신고할게요", 1),
])
def test_real_escalation_flagged(text, rating):
    assert cr(text, rating) == "escalate"


def test_low_rating_is_complaint():
    assert cr("배달이 너무 늦고 식었어요", 2) == "complaint"


def test_high_rating_no_text_is_photo_only():
    assert cr("", 5) == "photo_only"


def test_question_detected():
    assert cr("혹시 글루텐프리 베이글도 판매하나요?", 5) == "question"
