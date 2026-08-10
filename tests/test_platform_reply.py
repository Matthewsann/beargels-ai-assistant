"""플랫폼에 이미 달린 답글 수집 회귀 테스트 (2026-08-10, 사장님 요청).

답글이 달린 리뷰는 '처리할 답글'에서 빠지고(platform_replied), 실제 답글
본문(platform_reply)은 새벽 공부의 학습 데이터가 된다.
"""

from bs4 import BeautifulSoup

from crawler.baemin import BaeminCrawler, clean_owner_reply
from crawler.coupang import CoupangCrawler


def test_coupang_normalize_extracts_platform_reply():
    r = {"orderReviewId": 1, "rating": 5.0, "comment": "맛있어요",
         "customerName": "김*진", "createdAt": "2026-08-09",
         "orderInfo": [], "replies": [{"content": "김*진님, 감사해요!"}]}
    out = CoupangCrawler._normalize_review(r)
    assert out["reply_status"] == "posted"
    assert out["platform_reply"] == "김*진님, 감사해요!"


def test_coupang_normalize_no_reply():
    r = {"orderReviewId": 2, "rating": 4.0, "comment": "잘 먹었어요",
         "customerName": "박*수", "createdAt": "2026-08-09",
         "orderInfo": [], "replies": []}
    out = CoupangCrawler._normalize_review(r)
    assert out["reply_status"] == "none"
    assert out["platform_reply"] is None


def test_clean_owner_reply_strips_header_and_buttons():
    text = "사장님\n2026년 7월 10일\n폼키님,\n늘 감사해요!\n삭제"
    assert clean_owner_reply(text) == "폼키님,\n늘 감사해요!"
    assert clean_owner_reply("사장님\n삭제") is None
    assert clean_owner_reply(None) is None


def test_baemin_pairing_attaches_reply_to_preceding_review(monkeypatch):
    # 답글박스(형제 요소)는 '바로 앞' 리뷰에 귀속돼야 한다.
    html = """
    <div>
      <div class="ReviewContent-module__a1">리뷰1</div>
      <div class="ReviewCommentBox-module__b1">사장님
2026년 7월 10일
고객님답글본문
삭제</div>
      <div class="ReviewContent-module__a1">리뷰2</div>
    </div>"""
    calls = []

    def fake_parse(item):
        calls.append(item)
        return {"review_no": f"r{len(calls)}", "reply_status": "none"}

    monkeypatch.setattr(BaeminCrawler, "_parse_review_item",
                        staticmethod(fake_parse))
    soup = BeautifulSoup(html, "html.parser")
    reviews = BaeminCrawler.parse_review_cards(soup)
    assert len(reviews) == 2
    assert reviews[0]["reply_status"] == "posted"
    assert "고객님답글본문" in reviews[0]["platform_reply"]
    assert reviews[1]["reply_status"] == "none"
    assert "platform_reply" not in reviews[1]
