"""로컬 답글 엔진(API 폴백 고도화) 회귀 테스트."""

import pytest

from assistant.beargels import classify_review
from assistant.local_reply import _short_menu, _with_topic_josa, compose
from tests.test_reply_tone import BANNED, FORMAL_ENDINGS

REVIEWS = [
    {"platform": "coupang", "review_no": "a1", "author": "김*진", "rating": 5,
     "content": "베이글 진짜 쫄깃하고 맛있어요! 포장도 꼼꼼했어요",
     "menus": ["플레인 베이글", "크림치즈"], "order_count": 1},
    {"platform": "baemin", "review_no": "b2", "author": "이단골", "rating": 5,
     "content": "항상 시켜먹는데 매번 맛있어요. 애기도 잘먹어요",
     "menus": ["딸기 테디 케이크"], "order_count": 4},
    {"platform": "coupang", "review_no": "c3", "author": "박손님", "rating": 5,
     "content": "", "menus": ["[SET] 베이글 샌드위치 + 미니샐러드 + 음료 세트"],
     "order_count": 1},
    {"platform": "baemin", "review_no": "d4", "author": "최고객", "rating": 5,
     "content": "글루텐프리도 있나요?", "menus": ["향긋한 바질 베이글"],
     "order_count": 2},
]


def _reply(r, max_len=300):
    typ = classify_review(r)
    return typ, compose(r, typ, r["author"], r["order_count"], r["rating"],
                        max_len)


@pytest.mark.parametrize("review", REVIEWS)
def test_no_banned_phrases_and_casual_tone(review):
    typ, reply = _reply(review)
    for bad in BANNED:
        assert bad not in reply, f"금지 표현 '{bad}': {reply}"
    for bad in FORMAL_ENDINGS:
        assert bad not in reply, f"격식체 '{bad}': {reply}"


@pytest.mark.parametrize("review", REVIEWS)
def test_starts_with_author_and_fits_limit(review):
    _, reply = _reply(review, max_len=300)
    assert reply.startswith(f"{review['author']}님,")
    assert len(reply) <= 300


def test_replies_vary_between_reviews():
    replies = {_reply(r)[1] for r in REVIEWS}
    assert len(replies) == len(REVIEWS)   # 리뷰별 시드 랜덤 → 복붙 없음


def test_mentions_menu_keyword():
    _, reply = _reply(REVIEWS[0])
    assert "베이글" in reply or "크림치즈" in reply


def test_set_menu_shortened_and_matched():
    # 긴 세트명이 통째로 들어가거나 '베이글' 반응으로 오매칭되면 안 된다.
    assert _short_menu("[SET] 베이글 샌드위치 + 미니샐러드 + 음료 세트") == \
        "베이글 샌드위치"


def test_topic_josa():
    assert _with_topic_josa("베이글") == "베이글은"
    assert _with_topic_josa("케이크") == "케이크는"


def test_question_reply_acknowledges_question():
    typ, reply = _reply(REVIEWS[3])
    assert typ == "question"
    assert "물어봐" in reply or "여쭤봐" in reply
