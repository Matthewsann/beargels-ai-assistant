"""/place 화면 — 진단 현황 패널.

스마트플레이스 목표 1단계('최적화')는 "무엇이 비었는지 사람이 네이버를 뒤지지
않고 화면에서 바로 안다"가 성립해야 달성이다. 그래서 이 테스트가 지키는 것은
① 비어 있는 항목이 실제로 화면에 뜬다 ② 진단이 없거나 DB 가 죽어도 가이드
자체는 계속 보인다(현황 패널이 가이드를 인질로 잡지 않는다).
"""
import os

import pytest

os.environ.setdefault("SERVICE_PATH", "testkey")


@pytest.fixture()
def client(monkeypatch):
    from service import app as A

    A.app.config["TESTING"] = True
    return A, A.app.test_client()


SNAP = {
    "checkedAt": "2026-08-30T09:40:11",
    "placeId": "2023997350",
    "name": "베어글스 송도 타임스페이스점",
    "score": {"done": 5, "total": 10},
    "stats": {"rating": 4.96, "visitorReviews": 291, "blogReviews": 46},
    "todo": ["메뉴 등록", "영업시간"],
    "checks": [
        {"key": "menu", "label": "메뉴 등록", "value": "1건", "ok": False},
        {"key": "hours", "label": "영업시간", "value": "미등록", "ok": False},
        {"key": "desc", "label": "소개글", "value": "등록됨", "ok": True},
    ],
}


def test_비어있는_항목이_화면에_뜬다(client):
    A, c = client
    A.db.get_setting = lambda k, d=None: SNAP if k == "place_audit" else d
    html = c.get("/testkey/place").get_data(as_text=True)
    assert "지금 우리 플레이스 현황" in html
    assert "메뉴 등록" in html and "1건" in html
    assert "고칠 것 2개" in html
    assert "5/10 통과" in html


def test_진단이_아직_없으면_가이드만_보인다(client):
    A, c = client
    A.db.get_setting = lambda k, d=None: d
    r = c.get("/testkey/place")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "베어글스 플레이스 루틴" in html      # 가이드는 그대로
    assert "지금 우리 플레이스 현황" not in html   # 패널만 조용히 빠짐


def test_DB가_죽어도_가이드는_계속_보인다(client):
    """현황 패널 때문에 직원이 가이드를 못 보는 일은 없어야 한다."""
    A, c = client

    def boom(*a, **k):
        raise RuntimeError("supabase down")

    A.db.get_setting = boom
    r = c.get("/testkey/place")
    assert r.status_code == 200
    assert "베어글스 플레이스 루틴" in r.get_data(as_text=True)


def test_값은_이스케이프된다(client):
    A, c = client
    evil = dict(SNAP, todo=["<script>alert(1)</script>"],
                checks=[{"label": "<img src=x onerror=1>", "value": "x", "ok": False}])
    A.db.get_setting = lambda k, d=None: evil if k == "place_audit" else d
    html = c.get("/testkey/place").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "<img src=x onerror=1>" not in html


KW = {
    "checkedAt": "2026-08-31T09:00:00",
    "period": "2026-08-24 ~ 2026-08-30",
    "prevPeriod": "2026-08-17 ~ 2026-08-23",
    "total": 581, "totalDelta": -370,
    "mapPv": 401, "mapDelta": -291,
    "channels": [{"name": "네이버지도", "count": 401},
                 {"name": "네이버검색", "count": 144}],
    "keywords": [
        {"name": "송도베이글", "count": 130, "delta": 30},
        {"name": "인천대입구역카페", "count": 40, "delta": -5},
        {"name": "송도브런치", "count": 20, "delta": None},
    ],
}


def test_네이버지도_유입이_주인공으로_보인다(client):
    """목표 문구가 '네이버지도 노출'이라 이 숫자가 가장 크게 떠야 한다."""
    A, c = client
    A.db.get_setting = lambda k, d=None: KW if k == "place_keywords" else d
    html = c.get("/testkey/place").get_data(as_text=True)
    assert "네이버지도 노출" in html
    assert "네이버지도 유입" in html and "401" in html
    assert "▼ 291" in html                      # 지난주보다 줄었다


def test_유입_키워드가_증감과_함께_보인다(client):
    A, c = client
    A.db.get_setting = lambda k, d=None: KW if k == "place_keywords" else d
    html = c.get("/testkey/place").get_data(as_text=True)
    assert "어떤 검색어로 들어왔나" in html
    assert "송도베이글" in html and "130" in html
    assert "▲ 30" in html          # 늘어난 것
    assert "▼ 5" in html           # 줄어든 것
    assert "첫 기록" in html        # 이번에 처음 잡힌 것


def test_채널별_유입도_보인다(client):
    A, c = client
    A.db.get_setting = lambda k, d=None: KW if k == "place_keywords" else d
    html = c.get("/testkey/place").get_data(as_text=True)
    assert "어디서 들어왔나" in html and "네이버검색" in html


def test_비교할_지난주가_없으면_첫_기록으로_표시(client):
    A, c = client
    first = dict(KW, totalDelta=None, mapDelta=None, prevPeriod=None)
    A.db.get_setting = lambda k, d=None: first if k == "place_keywords" else d
    html = c.get("/testkey/place").get_data(as_text=True)
    assert "첫 기록" in html


def test_키워드_수집_전이면_패널이_안_뜬다(client):
    A, c = client
    A.db.get_setting = lambda k, d=None: d
    html = c.get("/testkey/place").get_data(as_text=True)
    # '네이버지도 노출'은 가이드의 목표 문구에도 있어 프로브로 못 쓴다.
    assert "네이버지도 유입" not in html
    assert "어떤 검색어로 들어왔나" not in html
    assert "베어글스 플레이스 루틴" in html


def test_키워드도_이스케이프된다(client):
    A, c = client
    evil = dict(KW, keywords=[{"name": "<script>x</script>", "count": 1, "delta": 1}])
    A.db.get_setting = lambda k, d=None: evil if k == "place_keywords" else d
    html = c.get("/testkey/place").get_data(as_text=True)
    assert "<script>x</script>" not in html


WEEKLY = [
    {"period": "2026-07-20 ~ 2026-07-26", "mapPv": 700,
     "storeSales": {"amount": 2_930_000, "days": 7, "missingDays": 0}},
    {"period": "2026-07-27 ~ 2026-08-02", "mapPv": 650,
     "storeSales": {"amount": 2_100_000, "days": 5, "missingDays": 2}},
    {"period": "2026-08-24 ~ 2026-08-30", "mapPv": 401,
     "storeSales": {"amount": 0, "days": 0, "missingDays": 0}},
]


def test_매장매출과_지도유입이_나란히_보인다(client):
    A, c = client
    A.db.get_setting = lambda k, d=None: WEEKLY if k == "place_weekly" else d
    html = c.get("/testkey/place").get_data(as_text=True)
    assert "노출이 매출로 이어졌나" in html
    assert "2,930,000원" in html and "700" in html


def test_배달_제외를_화면이_밝힌다(client):
    """플레이스는 매장 방문 채널이라 배달을 섞으면 안 된다 — 그 사실을 화면이 말해야 한다."""
    A, c = client
    A.db.get_setting = lambda k, d=None: WEEKLY if k == "place_weekly" else d
    html = c.get("/testkey/place").get_data(as_text=True)
    assert "매장 매출만" in html
    assert "배달" in html and "뺐어요" in html


def test_장부_미반영은_0원이_아니라_그렇게_표시된다(client):
    """0원으로 보이면 매출이 폭락한 걸로 오해한다 — 이 화면의 최악 실패."""
    A, c = client
    A.db.get_setting = lambda k, d=None: WEEKLY if k == "place_weekly" else d
    html = c.get("/testkey/place").get_data(as_text=True)
    assert "장부 미반영" in html
    # '0원'만으로는 못 잡는다 — '2,930,000원' 에도 들어 있다. 셀이 통째로
    # '0원'인 경우만 잡는다.
    assert ">0원<" not in html


def test_일부만_반영된_주는_며칠을_뺐는지_밝힌다(client):
    A, c = client
    A.db.get_setting = lambda k, d=None: WEEKLY if k == "place_weekly" else d
    html = c.get("/testkey/place").get_data(as_text=True)
    assert "장부 미반영 2일 제외" in html


def test_주간기록_전이면_매출패널이_안_뜬다(client):
    A, c = client
    A.db.get_setting = lambda k, d=None: d
    html = c.get("/testkey/place").get_data(as_text=True)
    assert "노출이 매출로 이어졌나" not in html
    assert "베어글스 플레이스 루틴" in html
