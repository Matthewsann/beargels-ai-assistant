"""스마트플레이스 유입 통계 — 순수 로직(브라우저·로그인 없음).

응답 모양은 2026-08-30 실측 그대로다:
    [{"mapped_channel_name": "네이버지도", "pv": 401.0}, ...]
    [{"ref_keyword": "송도베이글", "pv": 9.0}, ...]

목표 2단계('노출 상승')에서 제일 위험한 실패는 **로그인이 풀렸는데 '유입 0회'로
조용히 표시되는 것**이다. 그러면 노출이 떨어진 걸로 오해한다.
"""
from datetime import date

import pytest

from crawler.place_stats import (MAP_CHANNEL, NO_KEYWORD, NaverLoginRequired,
                                 is_logged_in, parse_rows, summarize,
                                 week_range)


class FakePage:
    def __init__(self, text="", url="https://new.smartplace.naver.com/bizes"):
        self._t, self.url = text, url

    def inner_text(self, _sel):
        return self._t


# ── 로그인 감지 ────────────────────────────────────────────────────
def test_로그인_안내문이_뜨면_로그인_안된_것():
    assert is_logged_in(FakePage("네이버 로그인이 필요한 기능입니다")) is False
    assert is_logged_in(FakePage("권한을 보유한 업체가 없습니다")) is False


def test_로그인창으로_튕기면_로그인_안된_것():
    assert is_logged_in(FakePage("아무 글", url="https://nid.naver.com/login")) is False


def test_정상화면이면_로그인된_것():
    assert is_logged_in(FakePage("베어글스 송도님 로그아웃 통계")) is True


def test_로그인_예외는_런타임에러다():
    """일꾼이 다른 실패와 구분해 알림함에 띄울 수 있어야 한다."""
    assert issubclass(NaverLoginRequired, RuntimeError)
    with pytest.raises(NaverLoginRequired):
        raise NaverLoginRequired("로그인 필요")


# ── 기간 ──────────────────────────────────────────────────────────
def test_직전_완결된_주를_쓴다():
    """2026-08-31(월)에 돌리면 8/24(월)~8/30(일)."""
    assert week_range(date(2026, 8, 31)) == ("2026-08-24", "2026-08-30")


def test_주중에_돌려도_지난주_전체를_쓴다():
    """진행 중인 주를 섞으면 며칠치 대 일주일치를 비교해 늘 줄어 보인다."""
    assert week_range(date(2026, 9, 2)) == ("2026-08-24", "2026-08-30")


# ── 응답 파싱(실측 모양) ────────────────────────────────────────────
def test_채널_응답을_파싱한다():
    payload = [{"mapped_channel_name": "네이버지도", "pv": 401.0},
               {"mapped_channel_name": "네이버검색", "pv": 144.0}]
    assert parse_rows(payload, "mapped_channel_name") == [
        {"name": "네이버지도", "count": 401}, {"name": "네이버검색", "count": 144}]


def test_키워드_응답을_파싱하고_많은_순으로_정렬한다():
    payload = [{"ref_keyword": "송도베이글", "pv": 9.0},
               {"ref_keyword": "베어글스송도", "pv": 21.0}]
    assert [r["name"] for r in parse_rows(payload, "ref_keyword")] == \
        ["베어글스송도", "송도베이글"]


def test_pv_실수를_정수로_바꾼다():
    assert parse_rows([{"ref_keyword": "a", "pv": 21.0}], "ref_keyword")[0]["count"] == 21


def test_모양이_다른_줄은_버린다():
    payload = [{"ref_keyword": "a", "pv": 3.0}, {"ref_keyword": None, "pv": 1.0},
               {"ref_keyword": "b"}, "쓰레기"]
    assert parse_rows(payload, "ref_keyword") == [{"name": "a", "count": 3}]


# ── 종합·변화 ──────────────────────────────────────────────────────
CH = [{"name": MAP_CHANNEL, "count": 401}, {"name": "네이버검색", "count": 144}]
KW = [{"name": "베어글스송도", "count": 21}, {"name": "송도베이글", "count": 9}]
DT = [{"name": "2026-08-24", "count": 114}, {"name": "2026-08-25", "count": 106}]


def test_검색어_없음은_키워드로_세지_않는다():
    kw = KW + [{"name": NO_KEYWORD, "count": 6}]
    names = [r["name"] for r in summarize(CH, kw, DT)["keywords"]]
    assert NO_KEYWORD not in names


def test_네이버지도_유입을_대표지표로_뽑는다():
    """목표 문구가 '네이버지도 노출'이라 이 값이 화면의 주인공이다."""
    assert summarize(CH, KW, DT)["mapPv"] == 401


def test_총유입은_일별합계를_쓴다():
    assert summarize(CH, KW, DT)["total"] == 220


def test_일별이_비면_채널합계로_갈음한다():
    assert summarize(CH, KW, [])["total"] == 545


def test_지난주_대비_증감을_계산한다():
    prev = {"checkedAt": "2026-08-24T09:00:00", "period": "2026-08-17 ~ 2026-08-23",
            "total": 951, "mapPv": 500,
            "keywords": [{"name": "송도베이글", "count": 4}]}
    now = summarize(CH, KW, DT, prev, period="2026-08-24 ~ 2026-08-30")
    assert now["totalDelta"] == 220 - 951
    assert now["mapDelta"] == 401 - 500
    by = {r["name"]: r for r in now["keywords"]}
    assert by["송도베이글"]["delta"] == 5
    assert by["베어글스송도"]["delta"] is None      # 이번에 새로 등장
    assert now["prevPeriod"] == "2026-08-17 ~ 2026-08-23"


def test_지난번이_없어도_동작한다():
    now = summarize(CH, KW, DT, None)
    assert now["totalDelta"] is None and now["mapDelta"] is None
    assert now["keywords"][0]["delta"] is None


# ── 주간 기록 관리(2026-09-03 점검에서 나온 구멍 3개) ─────────────────────
from crawler.place_stats import (done_this_week, done_today, is_recollection,
                                 merge_weekly, weekly_row)


def test_같은_주를_다시_받으면_재수집이다():
    """같은 주끼리 비교돼 '변화 없음'이 되던 원인. prev 를 덮으면 안 된다."""
    assert is_recollection("2026-08-24 ~ 2026-08-30", "2026-08-24 ~ 2026-08-30") is True
    assert is_recollection("2026-08-17 ~ 2026-08-23", "2026-08-24 ~ 2026-08-30") is False
    assert is_recollection(None, "2026-08-24 ~ 2026-08-30") is False


def test_주간표에_새_주가_붙는다():
    series = [{"period": "2026-08-17 ~ 2026-08-23", "start": "2026-08-17", "mapPv": 692}]
    out = merge_weekly(series, [{"period": "2026-08-24 ~ 2026-08-30",
                                 "start": "2026-08-24", "mapPv": 472}])
    assert [r["period"] for r in out] == ["2026-08-17 ~ 2026-08-23",
                                          "2026-08-24 ~ 2026-08-30"]
    assert series == [{"period": "2026-08-17 ~ 2026-08-23", "start": "2026-08-17",
                       "mapPv": 692}]      # 원본은 안 건드린다


def test_같은_주는_새_값으로_바뀐다_잠정치_보정():
    """월요일에 받은 401 이 하루 뒤 472 로 바뀌었다 — 나중 값이 이긴다."""
    series = [{"period": "2026-08-24 ~ 2026-08-30", "start": "2026-08-24",
               "mapPv": 401, "storeSales": {"amount": 0, "days": 0}}]
    out = merge_weekly(series, [{"period": "2026-08-24 ~ 2026-08-30",
                                 "start": "2026-08-24", "mapPv": 472}])
    assert len(out) == 1 and out[0]["mapPv"] == 472
    assert out[0]["storeSales"] == {"amount": 0, "days": 0}   # 없는 필드는 보존


def test_주간표는_기간순이다():
    out = merge_weekly([], [{"period": "2026-08-24 ~ 2026-08-30", "start": "2026-08-24"},
                            {"period": "2026-08-17 ~ 2026-08-23", "start": "2026-08-17"}])
    assert out[0]["start"] == "2026-08-17"


def test_스냅샷을_주간표_한_줄로_바꾼다():
    row = weekly_row({"period": "2026-08-24 ~ 2026-08-30", "mapPv": 472, "total": 690,
                      "storeSales": {"amount": 0, "days": 0, "missingDays": 0}})
    assert row["start"] == "2026-08-24" and row["end"] == "2026-08-30"
    assert row["mapPv"] == 472 and row["storeSales"]["days"] == 0


def test_재시작해도_오늘_이미_했으면_안_돈다():
    """같은 날 09:40, 19:25 두 번 돌았던 원인 — 메모리 표시가 재시작에 지워진다."""
    assert done_today("2026-09-03T19:25:00", date(2026, 9, 3)) is True
    assert done_today("2026-09-02T19:25:00", date(2026, 9, 3)) is False
    assert done_today(None, date(2026, 9, 3)) is False


def test_재시작해도_이번_주_이미_했으면_안_돈다():
    assert done_this_week("2026-08-31T16:58:01", date(2026, 8, 31)) is True   # 같은 주 월
    assert done_this_week("2026-08-31T16:58:01", date(2026, 9, 6)) is True    # 같은 주 일
    assert done_this_week("2026-08-31T16:58:01", date(2026, 9, 7)) is False   # 다음 주
    assert done_this_week("이상한값", date(2026, 9, 7)) is False
