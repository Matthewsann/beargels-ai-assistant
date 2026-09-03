"""스마트플레이스 진단 — 순수 로직(브라우저 없음).

목표 1단계 '최적화': 지금 무엇이 비었는지 화면에서 바로 보이게 하는 게 목적이라,
"비었는데 통과로 표시되는" 실수가 가장 위험하다. 그 방향을 집중적으로 지킨다.
"""
from crawler.place_audit import MENU_MIN, diagnose


def _raw(**over):
    base = {
        "name": "베어글스 송도 타임스페이스점",
        "category": "카페,디저트",
        "visitorReviewsScore": 4.96,
        "visitorReviewsTotal": 291,
        "cafeBlogReviewsTotal": 46,
        "conveniences": ["포장", "주차"],
        "phone": None,
        "virtualPhone": None,
        "talktalkUrl": None,
        "naverBlog": None,
        "missingInfo": {"isBizHourMissing": True, "isDescriptionMissing": False,
                        "isMenuImageMissing": False, "isAccessorMissing": False},
    }
    base.update(over.pop("base", {}))
    raw = {"placeId": "2023997350", "base": base,
           "menus": over.pop("menus", ["메뉴1"]),
           "feed_recent": over.pop("feed_recent", ["오늘"])}
    raw.update(over)
    return raw


def test_실제_현황_그대로_판정된다():
    """2026-08-30 실측: 메뉴 1건·영업시간/대표번호/톡톡/블로그 미설정."""
    d = diagnose(_raw())
    by = {c["key"]: c for c in d["checks"]}
    assert by["menu"]["ok"] is False and by["menu"]["value"] == "1건"
    assert by["hours"]["ok"] is False
    assert by["phone"]["ok"] is False
    assert by["talk"]["ok"] is False
    assert by["blog"]["ok"] is False
    # 이미 잘 된 것은 통과로 남아야 한다(과잉 경고 금지).
    assert by["desc"]["ok"] and by["road"]["ok"] and by["feed"]["ok"]


def test_고칠것_목록이_실패항목과_일치한다():
    d = diagnose(_raw())
    failed = [c["label"] for c in d["checks"] if not c["ok"]]
    assert d["todo"] == failed
    assert "정보 탭 메뉴" in d["todo"]


def test_메뉴는_문턱을_넘어야_통과():
    assert diagnose(_raw(menus=["m"] * (MENU_MIN - 1)))["todo"].count("정보 탭 메뉴") == 1
    ok = diagnose(_raw(menus=["m"] * MENU_MIN))
    assert "정보 탭 메뉴" not in ok["todo"]


def test_대표번호는_스마트콜만_있어도_통과():
    d = diagnose(_raw(base={"virtualPhone": "0507-1234-5678"}))
    by = {c["key"]: c for c in d["checks"]}
    assert by["phone"]["ok"] is True


def test_소식을_못_읽으면_통과로_치지_않는다():
    """크롤 실패를 '소식 없음'이 아니라 '확인 실패'로 두되, ok 는 False."""
    by = {c["key"]: c for c in diagnose(_raw(feed_recent=[]))["checks"]}
    assert by["feed"]["ok"] is False
    assert by["feed"]["value"] == "확인 실패"


def test_빈_응답이어도_터지지_않고_전부_미달로_나온다():
    """크롤이 통째로 실패했을 때 '전부 통과'처럼 보이면 최악이다."""
    d = diagnose({"placeId": "x"})
    assert d["score"]["done"] == 0
    assert len(d["todo"]) == d["score"]["total"]


def test_점수는_통과개수와_일치한다():
    d = diagnose(_raw(menus=["m"] * MENU_MIN))
    assert d["score"]["done"] == sum(1 for c in d["checks"] if c["ok"])
    assert d["score"]["total"] == len(d["checks"])


def test_기록용_필드가_채워진다():
    d = diagnose(_raw())
    assert d["checkedAt"] and d["name"] and d["placeId"] == "2023997350"
    assert d["stats"]["visitorReviews"] == 291
