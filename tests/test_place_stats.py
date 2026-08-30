"""스마트플레이스 유입 키워드 — 순수 로직(브라우저·로그인 없음).

목표 2단계('노출 상승')에서 제일 위험한 실패는 **로그인이 풀렸는데 '키워드
0개'로 조용히 표시되는 것**이다. 그러면 노출이 떨어진 걸로 오해한다.
그래서 "빈 값과 사고를 구분한다"를 집중적으로 지킨다.
"""
import pytest

from crawler.place_stats import (NaverLoginRequired, is_logged_in,
                                 parse_keywords, summarize)


class FakePage:
    def __init__(self, text="", url="https://new.smartplace.naver.com/bizes"):
        self._t, self.url = text, url

    def inner_text(self, _sel):
        return self._t


def test_로그인_안내문이_뜨면_로그인_안된_것():
    assert is_logged_in(FakePage("네이버 로그인이 필요한 기능입니다")) is False
    assert is_logged_in(FakePage("권한을 보유한 업체가 없습니다")) is False


def test_로그인창으로_튕기면_로그인_안된_것():
    assert is_logged_in(FakePage("아무 글", url="https://nid.naver.com/login")) is False


def test_정상화면이면_로그인된_것():
    assert is_logged_in(FakePage("내 업체 베어글스 송도 통계")) is True


def test_키워드를_모양으로_찾는다():
    payload = {"data": {"result": [
        {"keyword": "송도 베이글", "count": 120},
        {"keyword": "인천대입구역 카페", "count": 80},
    ]}}
    assert parse_keywords(payload) == [
        {"keyword": "송도 베이글", "count": 120},
        {"keyword": "인천대입구역 카페", "count": 80},
    ]


def test_키_이름이_달라도_찾는다():
    """네이버가 키 이름을 바꿔도 견뎌야 한다 — 경로 대신 모양으로 찾는 이유."""
    payload = {"rows": [{"query": "송도 브런치", "pv": 45}]}
    assert parse_keywords(payload) == [{"keyword": "송도 브런치", "count": 45}]


def test_큰_값_기준으로_중복을_합친다():
    payload = [{"keyword": "송도 베이글", "count": 10},
               {"keyword": "송도 베이글", "count": 30}]
    assert parse_keywords(payload) == [{"keyword": "송도 베이글", "count": 30}]


def test_많은_순으로_정렬된다():
    payload = [{"keyword": "a", "count": 1}, {"keyword": "b", "count": 9}]
    assert [r["keyword"] for r in parse_keywords(payload)] == ["b", "a"]


def test_키워드가_없으면_빈_목록():
    assert parse_keywords({"data": {"nothing": True}}) == []


def test_지난번_대비_변화를_계산한다():
    prev = {"checkedAt": "2026-08-23T09:00:00",
            "keywords": [{"keyword": "송도 베이글", "count": 100}]}
    now = summarize([{"keyword": "송도 베이글", "count": 130},
                     {"keyword": "송도 브런치", "count": 20}], prev)
    by = {r["keyword"]: r for r in now["keywords"]}
    assert by["송도 베이글"]["delta"] == 30
    assert by["송도 브런치"]["delta"] is None      # 이번에 새로 등장
    assert now["total"] == 150
    assert now["prevAt"] == "2026-08-23T09:00:00"


def test_지난번이_없어도_동작한다():
    now = summarize([{"keyword": "송도 베이글", "count": 5}], None)
    assert now["keywords"][0]["delta"] is None
    assert now["prevAt"] is None


def test_로그인_예외는_런타임에러다():
    """일꾼이 다른 실패와 구분해 알림함에 띄울 수 있어야 한다."""
    assert issubclass(NaverLoginRequired, RuntimeError)
    with pytest.raises(NaverLoginRequired):
        raise NaverLoginRequired("로그인 필요")
