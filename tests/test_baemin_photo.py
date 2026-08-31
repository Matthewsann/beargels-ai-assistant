"""배민 리뷰 사진 수집 (2026-08-31 사장님 발견 버그의 회귀 테스트).

사진만 남긴 배민 ★5 리뷰(2026082802870907)가 화면에 "(글·사진 없이 별점만
남김)"으로 뜨고, AI 초안도 "별점만 꾹 눌러주셨는데"라고 틀리게 썼다.
원인: 파서가 get_text() 로 텍스트만 뽑아 **<img> 가 통째로 사라졌고**,
_has_photo 는 배민이면 무조건 None(모름)이었는데 화면은 '없음'으로 단정했다.

실 DOM(2026-08-31 로그인 화면에서 확인): 리뷰 사진은
  <img alt="리뷰 사진" class="Thumbnail_… " src="https://bmreview.cdn.baemin.com/…">
사진 없는 카드에는 img 가 아예 없다.
"""

from bs4 import BeautifulSoup

from crawler.baemin import BaeminCrawler

# 실 DOM 구조를 본뜬 최소 카드 (사진 있는 쪽)
_CARD_WITH_PHOTO = """
<div class="ReviewContent-module__x1">
 <span>알뜰배달</span><span>파스타1인분장인</span><span>2026년 8월 28일</span>
 <span>리뷰번호 2026082802870907</span><span>1회 주문 고객</span>
 <span>(최근 6개월 누적 주문)</span>
 <img class="Thumbnail_b_r4ax_1 c_x"
      src="https://bmreview.cdn.baemin.com/i/a.jpg" alt="리뷰 사진">
 <div class="ReviewMenus-module__m"><span>주문메뉴</span>
   <li class="MenuItem-module__i">쇼콜라 테디 케이크</li></div>
 <span>사장님 댓글 등록하기</span>
</div>"""
_CARD_NO_PHOTO = _CARD_WITH_PHOTO.replace(
    '<img class="Thumbnail_b_r4ax_1 c_x"\n'
    '      src="https://bmreview.cdn.baemin.com/i/a.jpg" alt="리뷰 사진">', "")


def _parse(html):
    return BaeminCrawler._parse_review_item(
        BeautifulSoup(html, "html.parser").div)


def test_parser_captures_review_photos():
    """사진이 raw.images 에 담겨야 한다 — 텍스트만 뽑으면 영영 모른다."""
    r = _parse(_CARD_WITH_PHOTO)
    assert isinstance(r["raw"], dict), "raw 는 dict(text+images) 다"
    assert r["raw"]["images"] == ["https://bmreview.cdn.baemin.com/i/a.jpg"]
    # 기존 필드들은 그대로 나와야 한다
    assert r["author"] == "파스타1인분장인"
    assert r["review_no"] == "2026082802870907"
    assert r["menus"] == ["쇼콜라 테디 케이크"]


def test_parser_reports_no_photo_as_empty_not_missing():
    """사진 없는 카드는 images=[] — '모름'이 아니라 '없음'을 확실히 안다."""
    r = _parse(_CARD_NO_PHOTO)
    assert r["raw"]["images"] == []


def test_has_photo_reads_new_baemin_raw():
    from assistant.beargels import _has_photo
    assert _has_photo({"platform": "baemin",
                       "raw": {"text": "x", "images": ["u"]}}) is True
    assert _has_photo({"platform": "baemin",
                       "raw": {"text": "x", "images": []}}) is False
    # 옛 행(텍스트 raw)은 여전히 '모름' — 없다고 단정하면 같은 사고가 난다
    assert _has_photo({"platform": "baemin", "raw": "옛날 카드 텍스트"}) is None


def test_photo_only_classification_now_works_for_baemin():
    """글 없이 사진만 남긴 ★5 → photo_only (초안이 사진을 언급하게 된다)."""
    from assistant.beargels import classify_review
    rev = {"platform": "baemin", "rating": 5, "content": None,
           "raw": {"text": "1회 주문 고객", "images": ["u"]}}
    assert classify_review(rev) == "photo_only"


def test_low_rating_photo_only_baemin_escalates():
    """★1~2 + 글 없이 사진만 = 이물질 사진일 확률 — 배민도 이제 걸러진다."""
    from assistant.beargels import classify_review
    rev = {"platform": "baemin", "rating": 1, "content": "",
           "raw": {"text": "1회 주문 고객", "images": ["u"]}}
    assert classify_review(rev) == "escalate"


def test_order_count_still_parsed_from_dict_raw():
    """raw 가 dict 로 바뀌어도 '3회 주문 고객'은 계속 읽힌다 — 안 읽히면
    단골이 첫 주문 취급을 받는다."""
    from assistant.beargels import order_count_of
    assert order_count_of({"platform": "baemin",
                           "raw": {"text": "3회 주문 고객 (최근)",
                                   "images": []}}) == 3
    # 옛 텍스트 raw 도 그대로
    assert order_count_of({"platform": "baemin",
                           "raw": "5회 주문 고객"}) == 5


def test_order_info_survives_dict_raw():
    """_order_info 가 dict raw 에 정규식을 돌려 TypeError 로 죽으면 안 된다."""
    import service.app as app
    info = app._order_info({"platform": "baemin",
                            "raw": {"text": "7회 주문 고객", "images": []}})
    assert info["order_count"] == 7
    # 옛 텍스트 raw
    info2 = app._order_info({"platform": "baemin", "raw": "2회 주문 고객"})
    assert info2["order_count"] == 2


def test_screen_tells_only_what_it_knows():
    """사진 여부를 모르면 모른다고, 알면 사진을 보여준다 — 단정 금지."""
    import service.app as app
    v = app._review_view({"platform": "baemin", "reply_draft": "d",
                          "raw": {"text": "x", "images": ["https://u/a.jpg"]}})
    assert v["has_photo"] is True
    assert v["photos"] == ["https://u/a.jpg"]
    v2 = app._review_view({"platform": "baemin", "reply_draft": "d",
                           "raw": "옛 텍스트"})
    assert v2["has_photo"] is None and v2["photos"] == []
    import pathlib
    html = pathlib.Path("service/templates/staff.html").read_text(encoding="utf-8")
    assert "사진 여부 미확인" in html, "모를 땐 모른다고 말해야 한다"
    assert "rvphotos" in html, "사진이 있으면 화면에서 바로 보여준다"
