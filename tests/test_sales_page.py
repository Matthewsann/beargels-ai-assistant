"""매출 대시보드 순수 로직 + 장부 시간대 파서 (2026-09-03).

사장님 인터뷰로 정한 계약:
  · 이번 달 누적은 장부만 — partial(매장 장부 없는 날)은 매장·표본일에서 뺀다
  · 지난달 '같은 날짜까지' 비교, 작년 데이터 없으면 None
  · 요일 평균은 같은 요일 8주 — 기준선 없는 날은 양쪽에서 뺀다
  · 상품 TOP 은 지난달 순위와 견줘 오르내림, 배달비 같은 비메뉴는 제외
  · 히트맵은 (요일, 시) 평균 — 그 요일이 장부에 있는 날수로 나눈다
  · TOS '결제 상세내역' 은 취소(음수 행)를 부호째 합산, 배달은 매입사로 채널, 나머지는 매장
  · 2026-08 장부의 '한 줄 헤더' 양식에서도 배달 채널을 읽어야 한다
"""
from datetime import date, datetime

import openpyxl
import pytest

from service import sales_page as sp
from worker import pos_import


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------

def day(store, delivery, partial=False, **chans):
    row = {"store": store, "delivery": delivery, "total": store + delivery,
           "partial": partial}
    row.update(chans)
    return row


def month_daily(y, m, store=300_000, delivery=500_000, days=None, skip=()):
    """한 달치 daily(totals_by_date 결과 모양). days 없으면 말일까지."""
    import calendar
    n = days or calendar.monthrange(y, m)[1]
    out = {}
    for d in range(1, n + 1):
        if d in skip:
            continue
        out[str(date(y, m, d))] = day(store, delivery, baemin=delivery)
    return out


# ---------------------------------------------------------------------------
# 누적 · 비교
# ---------------------------------------------------------------------------

def test_누적은_장부_있는_날까지만():
    daily = month_daily(2026, 9, days=10)
    r = sp.month_to_date(daily, 2026, 9)
    assert r["days"] == 10 and r["upto"] == 10
    assert r["store"] == 3_000_000 and r["delivery"] == 5_000_000


def test_partial_날은_매장에서_빠지고_배달만_센다():
    daily = month_daily(2026, 9, days=3)
    daily["2026-09-04"] = day(0, 400_000, partial=True)
    r = sp.month_to_date(daily, 2026, 9)
    assert r["days"] == 3 and r["partial_days"] == 1
    assert r["store"] == 900_000                 # 0원이 안 섞였다
    assert r["delivery"] == 1_500_000 + 400_000
    assert r["upto"] == 4


def test_지난달_같은_날짜까지_비교():
    daily = month_daily(2026, 9, days=10, store=330_000)
    daily.update(month_daily(2026, 8, store=300_000))
    cur = sp.month_to_date(daily, 2026, 9)
    prev = sp.month_to_date(daily, 2026, 8, cur["upto"])
    assert prev["days"] == 10                   # 8월 1~10일만
    assert sp.pct(cur["store"], prev["store"]) == pytest.approx(0.10)


def test_기준이_없으면_pct는_None():
    assert sp.pct(100, 0) is None
    assert sp.pct(100, None) is None


def test_요일평균은_기준선_있는_날만_양쪽에_넣는다():
    # 7~8월을 매일 20만/30만으로 채우고, 9월 1~7일만 매장 24만(+20%)
    daily = {}
    for m in (7, 8):
        daily.update(month_daily(2026, m, store=200_000, delivery=300_000))
    daily.update(month_daily(2026, 9, store=240_000, delivery=300_000, days=7))
    r = sp.weekday_pace(daily, 2026, 9, 7)
    assert r["store"]["days"] == 7
    assert r["store"]["pct"] == pytest.approx(0.20)
    assert r["delivery"]["pct"] == pytest.approx(0.0)


def test_요일평균_이력이_없으면_비교불가():
    daily = month_daily(2026, 9, days=5)
    r = sp.weekday_pace(daily, 2026, 9, 5)
    assert r["total"]["days"] == 0 and r["total"]["pct"] is None


# ---------------------------------------------------------------------------
# 채널 · 상품
# ---------------------------------------------------------------------------

def test_채널_비중과_지난달_대비():
    daily = {}
    for d in range(1, 6):
        daily[str(date(2026, 9, d))] = day(400_000, 600_000, baemin=400_000, coupang=200_000)
        daily[str(date(2026, 8, d))] = day(400_000, 500_000, baemin=300_000, coupang=200_000)
    out = sp.channel_mix(daily, 2026, 9, 5, prev=(2026, 8))
    by = {c["channel"]: c for c in out}
    assert [c["channel"] for c in out] == ["store", "baemin", "coupang"]
    assert by["store"]["share"] == pytest.approx(0.4)
    assert by["baemin"]["pct"] == pytest.approx(2_000_000 / 1_500_000 - 1)
    assert by["coupang"]["pct"] == pytest.approx(0.0)
    assert by["baemin"]["delivery"] and not by["store"]["delivery"]


def test_상품_순위와_오르내림():
    cur = [{"product": "플레인 베이글", "qty": 50, "amount": 150_000},
           {"product": "연어 샌드위치", "qty": 20, "amount": 200_000},
           {"product": "배달비", "qty": 90, "amount": 270_000},      # 메뉴 아님
           {"product": "신메뉴 크루아상", "qty": 10, "amount": 60_000}]
    prev = [{"product": "플레인 베이글", "qty": 60, "amount": 180_000},
            {"product": "연어 샌드위치", "qty": 10, "amount": 100_000}]
    out = sp.product_rank(cur, prev)
    names = [p["product"] for p in out]
    assert "배달비" not in names
    assert names[0] == "연어 샌드위치" and out[0]["delta"] == 1     # 2위→1위
    assert out[1]["product"] == "플레인 베이글" and out[1]["delta"] == -1
    assert out[2]["new"] is True and out[2]["delta"] is None


# ---------------------------------------------------------------------------
# 목표 · 월말 예상
# ---------------------------------------------------------------------------

def test_목표_달성률과_이_속도면_월말():
    mtd = {"store": 3_000_000, "delivery": 5_000_000, "days": 10, "upto": 10}
    goals = {"2026-09": {"store": 10_000_000, "delivery": 12_000_000}}
    g = sp.goal_view(goals, "2026-09", mtd, 30)
    assert g["store"]["pct"] == pytest.approx(0.3)
    assert g["store"]["pace"] == 9_000_000
    assert g["delivery"]["pace"] == 15_000_000
    assert g["delivery"]["pace_pct"] == pytest.approx(1.25)


def test_목표_없으면_pct_None_예상은_그대로():
    mtd = {"store": 600_000, "delivery": 1_000_000, "days": 2, "upto": 2}
    g = sp.goal_view({}, "2026-09", mtd, 30)
    assert g["store"]["goal"] is None and g["store"]["pct"] is None
    assert g["store"]["pace"] is None          # 2일치로는 예상 안 함


def test_won_short():
    assert sp.won_short(1_234_567) == "123만"
    assert sp.won_short(123_456_789) == "1.2억"
    assert sp.won_short(9_800) == "9,800"
    assert sp.won_short(None) == "0"


# ---------------------------------------------------------------------------
# 히트맵
# ---------------------------------------------------------------------------

def test_히트맵은_그_요일이_있는_날수로_나눈다():
    rows = [
        # 월요일 2번(9/7, 9/14) 12시 매장 — 평균 15만
        {"sale_date": "2026-09-07", "hour": 12, "channel": "store", "amount": 100_000, "orders_count": 10, "source": "tos"},
        {"sale_date": "2026-09-14", "hour": 12, "channel": "store", "amount": 200_000, "orders_count": 20, "source": "tos"},
        # 월요일 18시 배달 — 한 날만 있어도 월요일 표본은 2일
        {"sale_date": "2026-09-07", "hour": 18, "channel": "baemin", "amount": 60_000, "orders_count": 3, "source": "tos"},
    ]
    h = sp.heatmap(rows)
    assert h["hours"] == list(range(12, 19))
    mon = h["modes"]["store"][0]
    assert mon[0]["a"] == 150_000 and mon[0]["c"] == 15
    assert h["modes"]["delivery"][0][6]["a"] == 30_000
    assert h["modes"]["total"][0][0]["a"] == 150_000
    assert h["dow_total"]["total"][0] == 180_000
    assert h["days"] == 2


def test_히트맵_매장은_출처합산_배달은_우선출처만():
    rows = [
        {"sale_date": "2026-01-10", "hour": 10, "channel": "store", "amount": 100, "orders_count": 1, "source": "imu"},
        {"sale_date": "2026-01-10", "hour": 10, "channel": "store", "amount": 200, "orders_count": 1, "source": "tos"},
        {"sale_date": "2026-01-10", "hour": 10, "channel": "baemin", "amount": 500, "orders_count": 1, "source": "tos"},
        {"sale_date": "2026-01-10", "hour": 10, "channel": "baemin", "amount": 480, "orders_count": 1, "source": "crawler"},
    ]
    h = sp.heatmap(rows)
    sat = date(2026, 1, 10).weekday()
    assert h["modes"]["store"][sat][0]["a"] == 300
    assert h["modes"]["delivery"][sat][0]["a"] == 500


def test_히트맵_빈_입력():
    assert sp.heatmap([])["hours"] == []


# ---------------------------------------------------------------------------
# month_days — 그래프용 상태
# ---------------------------------------------------------------------------

def test_날짜_상태_구분():
    daily = month_daily(2026, 9, days=3, skip=(2,))
    days = sp.month_days(daily, 2026, 9, today=date(2026, 9, 6), last_pos=date(2026, 9, 3))
    st = {d["day"]: d["state"] for d in days}
    assert st[1] == "data" and st[2] == "closed" and st[3] == "data"
    assert st[4] == "pending" and st[6] == "pending" and st[7] == "future"


# ---------------------------------------------------------------------------
# 장부 파서 — 시간대
# ---------------------------------------------------------------------------

def _ws(rows, title="s"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = title
    for r in rows:
        ws.append(r)
    return ws


def test_to_hour():
    assert pos_import._to_hour(datetime(2026, 8, 31, 22, 42, 1)) == 22
    assert pos_import._to_hour("2026-08-31 22:42:01") == 22
    assert pos_import._to_hour("2026-08-31 09:05") == 9
    assert pos_import._to_hour("2026-08-31") is None
    assert pos_import._to_hour(date(2026, 8, 31)) is None
    assert pos_import._to_hour(None) is None


def test_결제_상세내역_시간대_집계():
    ws = _ws([
        ["결제기준일자", "결제시각", "주문채널", "주문번호", "결제건수", "결제금액",
         "부가세", "결제수단", "매입사", "결제상태", "결제취소시각"],
        [None, None, None, "설명행", None, None, None, None, None, None, None],
        ["2026-08-31", "2026-08-31 22:42:01", "배달", "쿠팡이츠", 1, 16700, 1519, "기타", "쿠팡이츠", "승인", None],
        ["2026-08-31", "2026-08-31 22:36:22", "배달", "배달의민족", 1, 15400, 1400, "기타", "배달의민족", "승인", None],
        ["2026-08-31", "2026-08-31 22:04:29", "포스", "007", 1, 4875, 443, "QR결제", "토스계좌", "승인", None],
        [datetime(2026, 8, 31), datetime(2026, 8, 31, 22, 10), "키오스크", "키오-1", 1, 6000, 545, "카드", "KB카드", "승인", None],
        ["2026-08-31", "2026-08-31 22:50:00", "배달", "배달의민족", 1, -9000, -800, "기타", "배달의민족", "취소", "2026-08-31 22:55:00"],
        ["2026-08-30", "2026-08-30 11:15:00", "배달", "요기요", 1, 12000, 1000, "기타", "요기요", "승인", None],
    ])
    rows = pos_import._parse_tos_payment_detail(ws)
    by = {(r["sale_date"], r["hour"], r["channel"]): r for r in rows}
    assert by[("2026-08-31", 22, "coupang")]["amount"] == 16700
    assert by[("2026-08-31", 22, "baemin")]["amount"] == 15400 - 9000  # 취소는 음수 행 — 부호째 합산
    assert by[("2026-08-31", 22, "baemin")]["orders_count"] == 0      # 승인 1 − 취소 1
    assert by[("2026-08-31", 22, "store")]["amount"] == 4875 + 6000  # 포스+키오스크
    assert by[("2026-08-31", 22, "store")]["orders_count"] == 2
    assert by[("2026-08-30", 11, "yogiyo")]["amount"] == 12000
    assert all(r["source"] == "tos" for r in rows)


def test_TOS_한줄_헤더_양식에서_배달채널을_읽는다():
    """2026-08 장부: 헤더와 매입사 이름이 같은 줄. 예전엔 배달 0 → 매장 부풀림."""
    ws = _ws([
        [None, None, None, None, "결제수단별", None, "매입사별", None, None],
        ["기간", "결제금액", "부가세", "결제건수", "", "카드", "", "배달의민족", "쿠팡이츠"],
        ["2026-08-31", 952_183, 86_560, 68, None, 357_988, None, 316_520, 225_000],
    ])
    rows = pos_import._parse_tos_daily(ws)
    by = {r["channel"]: r for r in rows}
    assert by["baemin"]["amount"] == 316_520
    assert by["coupang"]["amount"] == 225_000
    assert by["store"]["amount"] == 952_183 - 316_520 - 225_000
    assert by["store"]["orders_count"] == 68


def test_TOS_두줄_헤더_양식도_그대로():
    ws = _ws([
        ["기간", "결제금액", "부가세", "결제건수", "결제수단별", None, "매입사별", None],
        ["", "", "", "", "", "카드", "", "배달의민족"],
        ["2026-07-31", 1_232_000, 111_998, 87, "", 504_500, "", 401_700],
    ])
    by = {r["channel"]: r for r in pos_import._parse_tos_daily(ws)}
    assert by["baemin"]["amount"] == 401_700
    assert by["store"]["amount"] == 1_232_000 - 401_700


def test_IMU_시간대는_영수증_첫행_매출금액만():
    ws = _ws([
        ["매출일시", "영수증번호", "메뉴 이름", "수량", "메뉴별 판매가", "매출금액"],
        [datetime(2026, 3, 1, 9, 30), "1", "플레인", 1, 3500, 8000],
        [datetime(2026, 3, 1, 9, 30), "1", "라떼", 1, 4500, None],
        [datetime(2026, 3, 1, 9, 50), "2", "플레인", 2, 7000, 7000],
        [datetime(2026, 3, 1, 14, 5), "3", "샌드위치", 1, 9000, 9000],
    ])
    wb = ws.parent
    sales, products, hourly = pos_import.parse_imu(wb)
    by = {r["hour"]: r for r in hourly}
    assert by[9]["amount"] == 15_000 and by[9]["orders_count"] == 2
    assert by[14]["amount"] == 9_000
    assert all(r["channel"] == "store" and r["source"] == "imu" for r in hourly)
    assert sales[0]["amount"] == 24_000
