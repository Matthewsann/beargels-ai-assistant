"""답글 말투 회귀 테스트 — AI/공지 말투 금지(사장님 피드백 2026-07-24).

'~바라요', '되셨으면 좋겠어요', '정성껏 준비하겠습니다' 등은 사람이 안 쓰는
말투라 금지. 템플릿 폴백이 이 표현을 절대 만들지 않는지 검증한다.
"""

import pytest

from assistant.beargels import _template_reply, _THANKS_VARIANTS

BANNED = [
    "바라요", "바랍니다", "되셨으면", "되었길", "즐거운 한 끼",
    "정성껏 준비하겠습니다", "정성을 다하겠습니다", "큰 힘이 됩니다",
    "보답하겠습니다", "보답할게요",
    # 격식체(합니다체) 종결 — 해요체와 섞지 않기(사장님 피드백 2026-07-24)
    "입니다", "습니다", "드립니다",
    # 방문 표현 — 배달이라 '주문 주세요'로(방문 아님)
    "들러주세요", "놀러오세요", "오시면", "와주셔", "또 오세요",
]

REVIEWS = [
    {"platform": "coupang", "review_no": "1", "author": "김손님", "rating": 5,
     "content": "", "order_count": 1},
    {"platform": "coupang", "review_no": "2", "author": "이단골", "rating": 5,
     "content": "늘 맛있어요", "order_count": 5},
    {"platform": "baemin", "review_no": "3", "author": "박고객", "rating": 2,
     "content": "배달이 늦고 식었어요", "order_count": 1},
    {"platform": "baemin", "review_no": "4", "author": "최손님", "rating": 5,
     "content": "글루텐프리 있나요?", "order_count": 1},
]


@pytest.mark.parametrize("review", REVIEWS)
def test_template_reply_has_no_ai_phrases(review):
    from assistant.beargels import classify_review
    typ = classify_review(review)
    reply = _template_reply(typ, review, review["author"],
                            review["order_count"], review["rating"], 500)
    for bad in BANNED:
        assert bad not in reply, f"금지 표현 '{bad}' 포함: {reply}"


def test_thanks_variants_clean():
    joined = " ".join(_THANKS_VARIANTS)
    for bad in BANNED:
        assert bad not in joined, f"_THANKS_VARIANTS 에 금지 표현 '{bad}'"


def test_persona_forbids_unkeepable_promises():
    # 사장님 피드백: 지킬 수 없는 약속(공짜 증정 등) 금지 규칙이 페르소나에 있어야.
    from assistant.beargels import REPLY_PERSONA
    assert "지킬 수 없는 약속" in REPLY_PERSONA


def test_persona_has_delivery_and_nofabrication_rules():
    from assistant.beargels import REPLY_PERSONA
    assert "주문 주세요" in REPLY_PERSONA           # 배달 표현
    assert "지어내지 않는다" in REPLY_PERSONA         # 없는 사실 금지
