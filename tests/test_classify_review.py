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


def test_high_rating_no_text_defaults_to_rating_only():
    # 사진이 '확인'되지 않으면 사진 리뷰가 아니라 별점만 리뷰로(2026-08-12).
    assert cr("", 5) == "rating_only"


def test_question_detected():
    assert cr("혹시 글루텐프리 베이글도 판매하나요?", 5) == "question"


@pytest.mark.parametrize("bad", ["2026년 7월 24일", "2026-07-24", "7월 24일", "", None, "12345"])
def test_clean_author_rejects_non_names(bad):
    from assistant.beargels import _clean_author
    assert _clean_author(bad) == "고객"


@pytest.mark.parametrize("good", ["김**", "KIM***", "박손님", "이영*"])
def test_clean_author_keeps_masked_names(good):
    from assistant.beargels import _clean_author
    assert _clean_author(good) == good


def test_complaint_report_includes_order_info():
    # 직원 단톡 공유용 — 주문시각·주문번호·문제내용 필수(사장님 요청 2026-07-26).
    from assistant.beargels import format_complaint_report
    rv = {"platform": "coupang", "review_no": "111", "author": "김*진",
          "rating": 2, "content": "포장이 젖어서 왔어요",
          "menus": ["잠봉뵈르"], "order_no": "0E2C6A",
          "ordered_at": "2026-07-21 18:07"}
    out = format_complaint_report([rv], "오후 2시")
    assert "주문번호: 0E2C6A" in out
    assert "주문시각: 2026-07-21 18:07" in out
    assert "문제내용" in out and "포장이 젖어서" in out


def test_complaint_report_baemin_fallback_to_review_id():
    # 배민은 주문번호가 없어 리뷰번호/작성일로 식별.
    from assistant.beargels import format_complaint_report
    rv = {"platform": "baemin", "review_no": "20260726123", "author": "박고객",
          "rating": 1, "content": "메뉴가 누락됐어요",
          "written_at": "2026년 7월 26일"}
    out = format_complaint_report([rv])
    assert "#20260726123" in out and "2026년 7월 26일" in out


def test_rating_only_when_no_photo_evidence():
    # 사진 없는 별점만 리뷰를 '사진 리뷰'로 오분류해 "사진 감사해요" 답글이
    # 실고객에 나간 사고(2026-08-12) 회귀 방지.
    from assistant.beargels import classify_review
    rv = {"platform": "coupang", "rating": 5, "content": "",
          "raw": '{"images": [], "comment": ""}'}
    assert classify_review(rv) == "rating_only"
    # raw 가 없어 판별 불가한 경우(배민 등)도 사진 언급을 피하는 쪽으로.
    assert classify_review({"platform": "baemin", "rating": 5,
                            "content": ""}) == "rating_only"


def test_photo_only_when_images_confirmed():
    from assistant.beargels import classify_review
    rv = {"platform": "coupang", "rating": 5, "content": "",
          "raw": '{"images": [{"url": "x"}]}'}
    assert classify_review(rv) == "photo_only"
