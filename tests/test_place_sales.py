"""플레이스 ↔ 매장 매출 연결 — 순수 로직.

네이버 플레이스는 **매장 방문**을 만드는 채널이다. 배달(배민·쿠팡)을 섞으면
노출과 매출의 관계가 희석되므로 store 만 센다(사장님 확정 2026-08-30).
"""
from database.mkt_store import store_only_sum


DAILY = {
    "2026-07-20": {"store": 400_000, "delivery": 600_000, "partial": False},
    "2026-07-21": {"store": 350_000, "delivery": 500_000, "partial": False},
    "2026-07-22": {"store": 0, "delivery": 700_000, "partial": True},   # 장부 미반영
    "2026-07-27": {"store": 900_000, "delivery": 100_000, "partial": False},
}


def test_배달은_빼고_매장만_센다():
    r = store_only_sum(DAILY, "2026-07-20", "2026-07-21")
    assert r["amount"] == 750_000        # 배달 1,100,000 은 안 들어간다
    assert r["days"] == 2


def test_구간_밖의_날은_안_센다():
    r = store_only_sum(DAILY, "2026-07-20", "2026-07-21")
    assert 900_000 != r["amount"]        # 7/27 은 구간 밖


def test_장부_미반영일은_빼고_몇일인지_알려준다():
    """0원으로 세면 매출이 폭락한 것처럼 보인다 — 표본에서 빼야 한다."""
    r = store_only_sum(DAILY, "2026-07-20", "2026-07-22")
    assert r["amount"] == 750_000        # 7/22 의 0원이 안 섞였다
    assert r["days"] == 2
    assert r["missingDays"] == 1


def test_전부_미반영이면_days가_0이다():
    """화면이 '0원'이 아니라 '장부 미반영'으로 표시할 수 있게 하는 신호."""
    r = store_only_sum(DAILY, "2026-07-22", "2026-07-22")
    assert r["days"] == 0 and r["amount"] == 0 and r["missingDays"] == 1


def test_빈_입력에도_안_터진다():
    assert store_only_sum({}, "2026-07-20", "2026-07-21")["days"] == 0
    assert store_only_sum(None, "2026-07-20", "2026-07-21")["amount"] == 0
