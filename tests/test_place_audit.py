"""스마트플레이스 진단 — 순수 로직(브라우저·네트워크 없음).

판정 기준은 '손님 화면에 무엇이 보이나'다(사장님 2026-09-23). 도구 값과 손님 화면이
다르면 손님 화면이 진실이다. 그리고 "비었는데 통과로 표시되는" 실수가 가장 위험하다.
"""
from crawler.place_audit import MENU_MIN, _apollo, _hours, _menu_dups, diagnose, hours_summary

HOURS = [{"day": d, "start": "07:20", "end": "23:00" if d != "일" else "22:00",
          "last_order": "22:50", "off": False} for d in "월화수목금토일"]


def _raw(**over):
    base = {
        "name": "베어글스 송도 타임스페이스점", "category": "카페,디저트",
        "visitorReviewsScore": 4.97, "visitorReviewsTotal": 296, "cafeBlogReviewsTotal": 47,
        "conveniences": ["포장", "주차"],
        "phone": None, "virtualPhone": None, "hasMobilePhoneNumber": True,
        "talktalkUrl": None, "naverBlog": None,
        # 2026-09-23 실측: 영업시간이 화면에 보이는데도 이 플래그는 true 로 남아 있다.
        "missingInfo": {"isBizHourMissing": True, "isDescriptionMissing": False,
                        "isMenuImageMissing": False, "isAccessorMissing": False},
    }
    base.update(over.pop("base", {}))
    raw = {"placeId": "2023997350", "base": base,
           "hours": over.pop("hours", HOURS),
           "phone_info": over.pop("phone_info", {"hasMobilePhoneNumber": True, "isVirtualPhone": False}),
           "menu_items": over.pop("menu_items", [{"id": "a", "name": "든든한 한끼 베이글 샌드위치 + 음료",
                                                  "price": "8,700원"}]),
           "menu_dups": over.pop("menu_dups", ["든든한 한끼 베이글 샌드위치 + 음료"]),
           "menu_meta": over.pop("menu_meta", {"count": 1, "updatedAt": "26.08.22", "images": 3}),
           "naver_order": over.pop("naver_order", {"items": [1, 2], "isPickup": True, "isPreOrder": True}),
           "feed_recent": over.pop("feed_recent", ["2026.09.22"])}
    raw.update(over)
    return raw


def _by(raw):
    return {c["key"]: c for c in diagnose(raw)["checks"]}


def test_손님_화면_실측_2026_09_23_그대로_판정된다():
    """영업시간·전화·주문은 화면에 있고, 메뉴 탭은 1건이 두 번 보이고, 톡톡·블로그는 없다."""
    by = _by(_raw())
    assert by["hours"]["ok"] is True and "07:20–23:00" in by["hours"]["value"]
    assert by["phone"]["ok"] is True and "전화 버튼" in by["phone"]["value"]
    assert by["order"]["ok"] is True and "포장" in by["order"]["value"]
    assert by["menu"]["ok"] is False and by["menu"]["value"].startswith("1건")
    assert "2번" in by["menu"]["value"] and "26.08.22" in by["menu"]["value"]
    assert by["talk"]["ok"] is False and by["blog"]["ok"] is False
    assert by["desc"]["ok"] and by["road"]["ok"] and by["feed"]["ok"]


def test_영업시간은_missingInfo_플래그가_아니라_화면의_시간표로_판정한다():
    assert _by(_raw())["hours"]["ok"] is True                       # 플래그 true 여도 통과
    by = _by(_raw(hours=[]))
    assert by["hours"]["ok"] is False and by["hours"]["value"] == "손님 화면에 없음"
    off = [dict(h, off=True, start=None, end=None) for h in HOURS]
    assert _by(_raw(hours=off))["hours"]["ok"] is False


def test_영업시간_요약은_같은_시간을_묶는다():
    assert hours_summary(HOURS) == "월~토 07:20–23:00 · 일 07:20–22:00 · 라스트오더 22:50"
    assert hours_summary([]) == ""
    h = HOURS[:6] + [dict(HOURS[6], off=True, start=None, end=None, last_order=None)]
    assert hours_summary(h).startswith("월~토 07:20–23:00 · 일 휴무")


def test_전화는_번호_비공개여도_버튼이_있으면_있음():
    assert _by(_raw())["phone"]["ok"] is True
    by = _by(_raw(base={"virtualPhone": "0507-1234-5678", "hasMobilePhoneNumber": False}, phone_info={}))
    assert by["phone"]["ok"] is True and by["phone"]["value"] == "0507-1234-5678"
    assert by["phone"]["label"] == "전화"
    by = _by(_raw(base={"hasMobilePhoneNumber": False}, phone_info={"hasMobilePhoneNumber": False}))
    assert by["phone"]["ok"] is False and by["phone"]["value"] == "없음"
    assert "스마트콜 아님" in _by(_raw())["phone"]["label"]     # 있지만 스마트콜은 아님


def test_메뉴는_문턱을_넘고_중복이_없어야_통과():
    many = [{"id": str(i), "name": f"m{i}"} for i in range(MENU_MIN)]
    assert _by(_raw(menu_items=many, menu_dups=[]))["menu"]["ok"] is True
    assert _by(_raw(menu_items=many[:-1], menu_dups=[]))["menu"]["ok"] is False
    by = _by(_raw(menu_items=many, menu_dups=["m0"]))
    assert by["menu"]["ok"] is False and "2번" in by["menu"]["value"]


def test_중복_메뉴는_여러_카테고리에_같은_ID_가_들어간_것():
    state = {"PlaceMenuItem:a": {"__typename": "PlaceMenuItem", "id": "a", "name": "세트",
                                 "price": {"displayText": "8,700원"}},
             "PlaceMenuCategory:recommend": {"__typename": "PlaceMenuCategory", "itemIds": ["a"]},
             "PlaceMenuCategory:u.p.1": {"__typename": "PlaceMenuCategory", "itemIds": ["a"]}}
    assert _menu_dups(state) == ["세트"]
    state["PlaceMenuCategory:u.p.1"]["itemIds"] = []
    assert _menu_dups(state) == []


def test_화면_데이터에서_영업시간을_읽는다():
    detail = {"newBusinessHours": [{"businessHours": [
        {"day": "일", "businessHours": {"start": "07:20", "end": "22:00"}, "lastOrderTimes": [{"time": "21:50"}]},
        {"day": "월", "businessHours": {"start": "07:20", "end": "23:00"}, "lastOrderTimes": []},
    ]}]}
    h = _hours(detail)
    assert [x["day"] for x in h] == ["월", "일"]                    # 요일 순으로 정렬
    assert h[1]["last_order"] == "21:50" and h[0]["last_order"] is None and not h[0]["off"]
    assert _hours({}) == []


def test_소식_날짜는_createdString_기준이라_오늘_글도_잡는다():
    from crawler.place_audit import _feed_dates
    state = {"Feed:1": {"__typename": "Feed", "createdString": "20260923", "relativeCreated": "오늘"},
             "Feed:2": {"__typename": "Feed", "createdString": "20260912", "relativeCreated": "2026.09.12."},
             "Feed:3": {"__typename": "Feed", "createdString": "20260901", "isDeleted": True},
             "Feed:4": {"__typename": "Feed", "date": "2026-08-12"}}
    assert _feed_dates(state) == ["2026.09.23", "2026.09.12", "2026.08.12"]


def test_apollo_는_스크립트_경계에서_끊는다():
    html = '<script>window.__APOLLO_STATE__ = {"a": {"b": "x}; y"}};</script><script>window.z=1;</script>'
    assert _apollo(html) == {"a": {"b": "x}; y"}}
    assert _apollo("no state") == {}


def test_고칠것_목록이_실패항목과_일치한다():
    d = diagnose(_raw())
    assert d["todo"] == [c["label"] for c in d["checks"] if not c["ok"]]
    assert "메뉴 탭" in d["todo"] and "영업시간" not in d["todo"]


def test_소식을_못_읽으면_통과로_치지_않는다():
    by = _by(_raw(feed_recent=[]))
    assert by["feed"]["ok"] is False and by["feed"]["value"] == "확인 실패"


def test_빈_응답이어도_터지지_않고_전부_미달로_나온다():
    """크롤이 통째로 실패했을 때 '전부 통과'처럼 보이면 최악이다."""
    d = diagnose({"placeId": "x"})
    assert d["score"]["done"] == 0
    assert len(d["todo"]) == d["score"]["total"]
    by = {c["key"]: c for c in d["checks"]}
    assert by["hours"]["value"] == "확인 실패" and by["phone"]["value"] == "확인 실패"


def test_옛_원자료_형식도_받는다():
    """menus 가 이름 목록뿐인 옛 저장분."""
    raw = _raw()
    raw.pop("menu_items")
    raw["menus"] = ["m"] * MENU_MIN
    raw["menu_dups"] = []
    assert _by(raw)["menu"]["ok"] is True


def test_점수는_통과개수와_일치한다():
    d = diagnose(_raw())
    assert d["score"]["done"] == sum(1 for c in d["checks"] if c["ok"])
