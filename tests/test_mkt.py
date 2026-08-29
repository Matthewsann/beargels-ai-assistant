"""마케팅 캘린더 순수 로직 회귀 테스트 (DB·브라우저 불필요).

핵심 계약:
  · totals_by_date — 매장은 출처 합산(IMU+TOS), 배달은 우선순위 출처만(이중계상 방지)
  · weekday_baseline / day_signal — 같은 요일 4주 평균, 휴무(0원) 제외
  · campaign_effect — 요일 보정 기대치 대비 증분, 짧은 기간 '참고용' 플래그
  · extract_targets — 제목에서 메뉴 이름 자동 인식
  · pos_import 셀 파싱 유틸
"""
from datetime import date, timedelta

from database import mkt_store
from worker import pos_import


# ---------------------------------------------------------------------------
# pos_import 유틸
# ---------------------------------------------------------------------------

def test_to_date_formats():
    assert pos_import._to_date("2026-07-31") == date(2026, 7, 31)
    assert pos_import._to_date("2026-07-31 22:48:35") == date(2026, 7, 31)
    assert pos_import._to_date("20251001") == date(2025, 10, 1)
    assert pos_import._to_date("합계") is None
    assert pos_import._to_date(None) is None


def test_to_int_formats():
    assert pos_import._to_int("1,232,000") == 1232000
    assert pos_import._to_int(" 5,800원") == 5800
    assert pos_import._to_int(14000.0) == 14000
    assert pos_import._to_int("-4,300") == -4300
    assert pos_import._to_int("") == 0


# ---------------------------------------------------------------------------
# totals_by_date — 출처 병합 규칙
# ---------------------------------------------------------------------------

def test_store_sums_across_sources():
    """매장 = IMU(키오스크) + TOS(포스) 합산 — 1월 장부 원 단위 일치 검증의 규칙."""
    rows = [
        {"sale_date": "2026-01-10", "channel": "store", "amount": 100, "source": "imu"},
        {"sale_date": "2026-01-10", "channel": "store", "amount": 200, "source": "tos"},
    ]
    daily = mkt_store.totals_by_date(rows)
    assert daily["2026-01-10"]["store"] == 300
    assert daily["2026-01-10"]["total"] == 300


def test_delivery_prefers_tos_over_other_sources():
    """배달 채널은 같은 날 tos 와 정산엑셀/크롤러가 겹치면 tos 만 (이중계상 방지)."""
    rows = [
        {"sale_date": "2026-01-10", "channel": "baemin", "amount": 500, "source": "tos"},
        {"sale_date": "2026-01-10", "channel": "baemin", "amount": 480, "source": "baemin_xls"},
        {"sale_date": "2026-01-10", "channel": "coupang", "amount": 300, "source": "crawler"},
    ]
    daily = mkt_store.totals_by_date(rows)
    assert daily["2026-01-10"]["baemin"] == 500          # tos 만
    assert daily["2026-01-10"]["coupang"] == 300         # 크롤러뿐이면 그대로
    assert daily["2026-01-10"]["delivery"] == 800


# ---------------------------------------------------------------------------
# 요일 베이스라인 / 신호
# ---------------------------------------------------------------------------

def _make_daily(base_amount=1000000, weeks=5, start=date(2026, 7, 1)):
    """월요일 휴무(0원 없음 = 행 자체 없음)인 5주치 가짜 매출."""
    rows = []
    for i in range(weeks * 7):
        d = start + timedelta(days=i)
        if d.weekday() == 0:      # 월요일 휴무
            continue
        rows.append({"sale_date": str(d), "channel": "store",
                     "amount": base_amount, "source": "tos"})
    return rows


def test_weekday_baseline_excludes_closed_days():
    daily = mkt_store.totals_by_date(_make_daily())
    target = date(2026, 7, 29)    # 수요일
    base = mkt_store.weekday_baseline(daily, target)
    assert base == 1000000
    # 휴무일(월)은 표본이 없으니 None
    assert mkt_store.weekday_baseline(daily, date(2026, 7, 27)) is None


def test_day_signal_threshold():
    rows = _make_daily()
    spike = date(2026, 7, 29)
    rows = [r for r in rows if r["sale_date"] != str(spike)]
    rows.append({"sale_date": str(spike), "channel": "store",
                 "amount": 1200000, "source": "tos"})     # +20%
    daily = mkt_store.totals_by_date(rows)
    assert mkt_store.day_signal(daily, spike) == 1
    assert mkt_store.day_signal(daily, spike - timedelta(days=1)) == 0


# ---------------------------------------------------------------------------
# 캠페인 효과
# ---------------------------------------------------------------------------

def test_campaign_effect_uplift_and_short_flag():
    rows = _make_daily(weeks=9, start=date(2026, 6, 1))
    # 캠페인 기간(7/20~7/26)만 매출 +30%
    camp_days = [date(2026, 7, 20) + timedelta(days=i) for i in range(7)]
    boosted = []
    for r in rows:
        if r["sale_date"] in {str(d) for d in camp_days}:
            r = dict(r, amount=1300000)
        boosted.append(r)
    camp = {"id": 1, "title": "테스트", "start_date": "2026-07-20",
            "end_date": "2026-07-26", "cost": 100000, "target_products": []}
    eff = mkt_store.campaign_effect(camp, boosted, [], today=date(2026, 8, 5))
    assert not eff["short"]                     # 7일이면 참고용 아님
    assert eff["total"]["pct"] and eff["total"]["pct"] > 0.25
    assert eff["uplift"] > 0
    assert eff["roas"] and eff["roas"] > 10     # 증분/비용

    camp2 = dict(camp, start_date="2026-07-25", end_date="2026-07-26")
    eff2 = mkt_store.campaign_effect(camp2, boosted, [], today=date(2026, 8, 5))
    assert eff2["short"]                        # 2일짜리는 참고용


def test_campaign_effect_hides_bogus_pct_for_provisional_days():
    """장부 미반영(잠정) 구간은 0원 대비 ▼100% 같은 허수를 내면 안 된다.

    실사고(2026-08-27/29): day_detail은 고쳤는데 campaign_effect가 그대로라,
    사장님이 오늘 만든 실제 캠페인(8/28, 장부는 7/31까지 반영)을 열어보니
    total.pct=-1.0(=▼100%)이 나왔다. last_pos 이후 날짜는 실제/기대 양쪽
    합계에서 통째로 빼야 한다.
    """
    rows = _make_daily(weeks=9, start=date(2026, 6, 1))
    camp = {"id": 3, "title": "오늘 시작한 캠페인", "start_date": "2026-08-28",
            "end_date": "2026-08-28", "cost": None, "target_products": []}
    # 8/28 매출 행이 아예 없다(당일 크롤러 미수집) — 장부는 7/31까지만.
    eff = mkt_store.campaign_effect(camp, rows, [], today=date(2026, 8, 28),
                                    last_pos=date(2026, 7, 31))
    assert eff["provisional_days"] == 1
    assert eff["total"]["pct"] is None           # -1.0 이 아니라 None
    assert eff["total"]["actual"] == 0
    assert eff["total"]["expected"] == 0         # 기대치도 같이 빠져야 함
    assert eff["uplift"] == 0


def test_campaign_effect_partial_provisional_only_counts_ledger_days():
    """캠페인 기간 일부만 장부 반영됐으면 반영된 날만 비교한다."""
    rows = _make_daily(weeks=9, start=date(2026, 6, 1))
    boosted = []
    for r in rows:
        if r["sale_date"] == "2026-07-30":
            r = dict(r, amount=1300000)          # +30%
        boosted.append(r)
    camp = {"id": 4, "title": "월말 캠페인", "start_date": "2026-07-30",
            "end_date": "2026-08-01", "cost": None, "target_products": []}
    eff = mkt_store.campaign_effect(camp, boosted, [], today=date(2026, 8, 1),
                                    last_pos=date(2026, 7, 31))
    assert eff["days"] == 3
    assert eff["provisional_days"] == 1          # 8/1 만 제외 (7/30·7/31은 반영)
    assert eff["total"]["pct"] is not None
    assert eff["total"]["pct"] > 0.1              # 7/30 부스트가 반영된 값(7/31은 평상시)


def test_campaign_effect_target_products():
    sales = _make_daily(weeks=9, start=date(2026, 6, 1))
    daily_dates = sorted({r["sale_date"] for r in sales})
    prows = []
    for d in daily_dates:
        qty = 14 if "2026-07-20" <= d <= "2026-07-26" else 6
        prows.append({"sale_date": d, "product": "버터떡", "qty": qty,
                      "amount": qty * 5000, "source": "tos"})
    camp = {"id": 2, "title": "버터떡 릴스", "start_date": "2026-07-20",
            "end_date": "2026-07-26", "cost": None,
            "target_products": ["버터떡"]}
    eff = mkt_store.campaign_effect(camp, sales, prows, today=date(2026, 8, 5))
    t = eff["targets"][0]
    assert t["product"] == "버터떡"
    assert t["qty_per_day"] > t["pre_qty_per_day"]
    assert t["qty_pct"] > 1.0                   # 6개 → 14개


# ---------------------------------------------------------------------------
# 타겟 자동 인식
# ---------------------------------------------------------------------------

def test_extract_targets_from_title():
    products = ["버터떡", "두쫀쿠", "플레인 베이글", "아메리카노"]
    assert mkt_store.extract_targets("버터떡 인스타 릴스", products) == ["버터떡"]
    # 공백 무시 부분일치
    assert mkt_store.extract_targets("플레인베이글 1+1", products) == ["플레인 베이글"]
    assert mkt_store.extract_targets("가을 신메뉴 홍보", products) == []
