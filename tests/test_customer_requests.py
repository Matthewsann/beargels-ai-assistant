"""리뷰 속 고객 요청사항 골라내기 (2026-08-28).

사장님 확정: **AI 없이 키워드로**, 대신 '운영에 쓸 수 있는 것만'.
그래서 이 테스트의 절반은 **잡지 말아야 할 것**을 지킨다 — 요청 신호만 보면
실측 1,663건에서 절반이 오탐이었다.

여기 있는 문장은 전부 실제 리뷰에서 가져왔다.
"""

from assistant.customer_requests import (
    find_requests, format_for_kakao, request_in,
)


# --- 잡아야 하는 것 -------------------------------------------------------

def test_the_stapler_review_that_started_this():
    """사장님이 가져오신 그 리뷰 — 칭찬 속에 진짜 요청이 섞여 있다."""
    hit = request_in("맛있더요 항상 제발 절 딸래미로 받아주세요♡ 그리고 확실히 "
                     "스테이플러로 포장해주시는 것보단 스티커가 조아요💕💕 "
                     "스테이플러 포장은 열 때 넘 위험하더라구요")
    assert hit is not None
    topic, _icon, quote = hit
    assert topic == "포장"
    # 앞의 농담·인사는 덜어내고 요청 대목부터 보여준다
    assert quote.startswith("확실히 스테이플러로")
    assert "딸래미" not in quote


def test_catches_requests_across_topics():
    cases = [
        ("최소주문금액 조금만 내려주시면 안댈까요", "가격"),
        ("세트선택에서 빵 중복선택이되면 좋을것 같아유", "양·구성"),
        ("베이글에 탄자국이잇어서 아쉬워요", "맛·품질"),
        ("저번 주부터 메뉴누락 실수가 지속되서 많이 불편하네요", "배달·누락"),
    ]
    for text, topic in cases:
        hit = request_in(text)
        assert hit and hit[0] == topic, f"{text} → {hit}"


def test_typos_do_not_hide_a_request():
    """손님 글은 맞춤법대로 오지 않는다 — '조아요'도 '좋아요'다."""
    assert request_in("스테이플러보단 스티커가 조아요") is not None


# --- 잡으면 안 되는 것 ----------------------------------------------------

def test_praise_is_not_a_request():
    """실측에서 오탐이던 문장들 — 하나라도 걸리면 목록이 쓸모없어진다."""
    for text in [
        "야채 신선하고 조합이 좋아요",
        "무엇보다 아침 일찍 배달되는 게 넘 좋았어요",
        "요청사항 들어주셔서 감사합니다",
        "이 맛있는 베이글 널리널리 알려지면 좋겠어요",
        "테디베어 치즈케이크가 넘 귀여워서 먹기 아쉬웠지만 아들은 잘 먹었어요",
        "곰 케익은 빵 보다는 초코 무스로 되어있는데 당 충전이 제대로입니다",
    ]:
        assert request_in(text) is None, text


def test_a_joke_is_not_a_request():
    """부탁처럼 들려도 매장이 바꿀 수 있는 게 없으면 요청이 아니다."""
    assert request_in("제발 절 딸래미로 받아주세요♡") is None


def test_empty_input_is_safe():
    assert request_in("") is None
    assert request_in(None) is None
    assert find_requests([]) == []
    assert find_requests(None) == []


# --- 목록·단톡방 양식 -----------------------------------------------------

from datetime import datetime as _dt

_TODAY = _dt.now().strftime("%Y-%m-%d")

_REVIEWS = [
    {"id": 1, "platform": "baemin", "author": "한입에와앙", "rating": 5,
     "written_date": "2026-08-22", "collected_at": _TODAY,
     "content": "스테이플러로 포장해주시는 것보단 스티커가 조아요 열 때 위험해요"},
    {"id": 2, "platform": "coupang", "author": "김*슬", "rating": 4,
     "written_date": "2026-04-25", "collected_at": _TODAY,
     "content": "배달오면서 많이 샜는데 뚜껑 마개같은게 있으면 더 좋을것 같아요"},
    {"id": 3, "platform": "baemin", "author": "행복이", "rating": 5,
     "written_date": "2026-08-25", "content": "너무 맛있어요 잘 먹었습니다"},
]


def test_list_is_newest_first_and_skips_plain_praise():
    items = find_requests(_REVIEWS)
    assert [i["id"] for i in items] == [1, 2], "칭찬만 남긴 리뷰는 빠져야 한다"
    assert items[0]["platform"] == "배민"


def test_kakao_text_is_ready_to_paste():
    text = format_for_kakao(find_requests(_REVIEWS), today="8/28")
    assert text.startswith("📌 고객 요청사항 2건 (8/28)")
    assert "1. [포장]" in text
    assert "배민 한입에와앙님 ★5 · 08/22" in text
    # 단톡방에 붙일 글이라 HTML·링크 같은 군더더기가 없어야 한다
    assert "<" not in text and "http" not in text


def test_kakao_text_when_there_is_nothing():
    assert "없어요" in format_for_kakao([])


# --- 공유 완료 체크 (2026-08-28) ------------------------------------------
# 단톡방에 올린 이야기가 목록에 계속 남아 있으면 무엇이 새것인지 알 수 없다.

def test_period_label_reads_like_a_person_wrote_it():
    import service.app as app
    assert app._period_label(7) == "최근 1주일"
    assert app._period_label(14) == "최근 2주일"
    assert app._period_label(30) == "최근 1개월"
    assert app._period_label(5) == "최근 5일"


def test_shared_items_drop_off_the_list(monkeypatch):
    """공유 완료한 건은 목록에서 빠진다."""
    import service.app as app
    monkeypatch.setattr(app.db, "search_reviews", lambda **k: (_REVIEWS, len(_REVIEWS)))
    monkeypatch.setattr(app.db, "get_setting", lambda key, default=None: [1])
    app._customer_requests.cache_clear()
    got, more = app._customer_requests()
    assert [i["id"] for i in got] == [2], "공유한 1번은 빠지고 2번만 남아야 한다"
    assert more == 0
    app._customer_requests.cache_clear()


def test_late_collected_reviews_still_get_their_turn(monkeypatch):
    """작성일이 창 밖이어도 **방금 수집**됐으면 한 번은 화면에 뜬다.

    작성일로만 자르면 늦게 수집된 리뷰의 요청은 화면에 한 번도 못 뜨고
    영구 소멸했다(2026-08-30 감사)."""
    import service.app as app
    rows = [{"id": 9, "platform": "baemin", "author": "손님", "rating": 5,
             "written_date": "2026-01-01",          # 옛 작성일
             "collected_at": _TODAY,                 # 방금 수집
             "content": "포장 스테이플러 대신 스티커로 해주세요"}]
    monkeypatch.setattr(app.db, "search_reviews", lambda **k: (rows, 1))
    monkeypatch.setattr(app.db, "get_setting", lambda key, default=None: [])
    app._customer_requests.cache_clear()
    got, _ = app._customer_requests()
    assert [i["id"] for i in got] == [9]
    app._customer_requests.cache_clear()


def test_cache_can_be_cleared_so_the_screen_updates_at_once():
    """체크 직후엔 캐시가 남아 '안 바뀐 것처럼' 보이면 안 된다."""
    import service.app as app
    assert callable(getattr(app._customer_requests, "cache_clear", None))
