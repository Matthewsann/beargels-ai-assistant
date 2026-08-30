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
    assert eff["uplift"] is None                 # 비교 자체가 없으면 증분도 없음


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
    """반환값은 '사장님이 적은 키워드'다 — 상품명 원문이 아니라.
    (집계는 product_matches 가 키워드↔상품명을 부분일치로 잇는다.)"""
    products = ["상하이 버터떡 1BOX", "두쫀쿠", "플레인 베이글",
                "E)아메리카노", "베이글-풀드포크 샌드위치"]
    assert mkt_store.extract_targets("버터떡 인스타 릴스", products) == ["버터떡"]
    assert mkt_store.extract_targets("플레인베이글 1+1", products) == ["플레인베이글"]
    got = mkt_store.extract_targets("풀드포크 샌드위치 릴스 밀기", products)
    assert got == ["풀드포크 샌드위치"]
    assert mkt_store.extract_targets("가을 대비 매장 정비", products) == []


def test_product_matches_bridges_owner_words_and_pos_names():
    """'버터떡' ↔ '상하이 버터떡 1BOX' 같은 표기 차이를 넘는다 (실사고:
    완전일치라 실상품 115종 기준 거의 전부 미스, 타겟 칸이 거짓 0)."""
    assert mkt_store.product_matches("버터떡", "상하이 버터떡 1BOX")
    assert mkt_store.product_matches("풀드포크 샌드위치", "베이글-풀드포크 샌드위치")
    assert mkt_store.product_matches("아메리카노", "E)아메리카노")
    assert not mkt_store.product_matches("버터떡", "플레인 베이글")


def test_partial_day_excluded_from_baseline_and_signal():
    """매장 장부 없이 배달만 잡힌 날(partial)은 표본도, 신호점도 안 된다.
    실사고: 2025-12 가 통째로 배달-only 라 2026-01 캘린더가 ▲27개 도배."""
    rows = []
    # 4주간 정상(매장+배달 = 120만), 그 다음 주 같은 요일은 배달만(70만)
    for w in range(4):
        d = date(2026, 6, 3) + timedelta(days=7 * w)   # 수요일들
        rows.append({"sale_date": str(d), "channel": "store",
                     "amount": 500000, "source": "tos"})
        rows.append({"sale_date": str(d), "channel": "baemin",
                     "amount": 700000, "source": "tos"})
    partial_day = date(2026, 7, 1)
    rows.append({"sale_date": str(partial_day), "channel": "baemin",
                 "amount": 700000, "source": "baemin_xls"})
    daily = mkt_store.totals_by_date(rows)
    assert daily[str(partial_day)]["partial"] is True
    # partial 날 자신은 신호점 없음 (▼ 허수 방지)
    assert mkt_store.day_signal(daily, partial_day) == 0
    # partial 날은 다음 주의 표본에서도 빠진다 (기준선 오염 방지)
    next_wed = partial_day + timedelta(days=7)
    assert mkt_store.weekday_baseline(daily, next_wed) == 1200000


def test_campaign_effect_like_for_like_with_adhoc_closure():
    """평소 열던 요일의 임시휴무가 껴도 효과가 깎이지 않는다 (실사고:
    +20% 캠페인이 +0.0%로, 극단에선 부호 반전)."""
    rows = []
    d = date(2026, 6, 1)
    while d <= date(2026, 7, 31):
        if d.weekday() != 0 and str(d) != "2026-07-22":   # 7/22(수) 임시휴무
            amt = 1200000 if "2026-07-21" <= str(d) <= "2026-07-27" else 1000000
            rows.append({"sale_date": str(d), "channel": "store",
                         "amount": amt, "source": "tos"})
        d += timedelta(days=1)
    camp = {"id": 9, "title": "임시휴무 낀 캠페인", "start_date": "2026-07-21",
            "end_date": "2026-07-27", "cost": None, "target_products": []}
    eff = mkt_store.campaign_effect(camp, rows, [], today=date(2026, 8, 5))
    assert abs(eff["total"]["pct"] - 0.20) < 0.01
    assert eff["closed_days"] == 2                  # 정기휴무(월) + 임시휴무(수)
    assert eff["gross"] == eff["total"]["actual"] + 0   # 이 시나리오에선 동일


def test_campaign_effect_no_baseline_means_no_uplift():
    """비교 이력이 전혀 없으면 uplift/ROAS 를 만들지 않는다 (실사고:
    actual 만 합산돼 '증분 700만원'처럼 보이는 뻥튀기)."""
    rows = [{"sale_date": str(date(2026, 7, 1) + timedelta(days=i)),
             "channel": "store", "amount": 1000000, "source": "tos"}
            for i in range(7)]
    camp = {"id": 10, "title": "이력 없는 첫 주", "start_date": "2026-07-01",
            "end_date": "2026-07-07", "cost": 100000, "target_products": []}
    eff = mkt_store.campaign_effect(camp, rows, [], today=date(2026, 8, 5))
    assert eff["total"]["pct"] is None
    assert eff["uplift"] is None
    assert eff["roas"] is None
    assert eff["gross"] == 7000000                  # 총액은 그대로 참고용


def test_campaign_effect_top_products_when_no_target():
    """타겟이 없으면 기간 판매 TOP 을 보여준다 — 사장님의 실제 첫 기록이
    타겟 없는 캠페인이었다."""
    sales = _make_daily(weeks=9, start=date(2026, 6, 1))
    prows = []
    for r in sales:
        prows.append({"sale_date": r["sale_date"], "product": "상하이 버터떡 1BOX",
                      "qty": 5, "amount": 25000, "source": "tos"})
        prows.append({"sale_date": r["sale_date"], "product": "E)아메리카노",
                      "qty": 10, "amount": 30000, "source": "tos"})
    camp = {"id": 11, "title": "타겟 없는 기록", "start_date": "2026-07-20",
            "end_date": "2026-07-26", "cost": None, "target_products": []}
    eff = mkt_store.campaign_effect(camp, sales, prows, today=date(2026, 8, 5))
    names = [t["product"] for t in eff["top_products"]]
    assert "E)아메리카노" in names and "상하이 버터떡 1BOX" in names
    assert eff["top_products"][0]["product"] == "E)아메리카노"   # 금액순


# ---------------------------------------------------------------------------
# 화면 조립 — 리마인드가 허위로 뜨지 않는가 (DB 는 monkeypatch 로 대체)
# ---------------------------------------------------------------------------

def _stub_store(monkeypatch, sales, last_pos, camps=()):
    """mkt_page 가 부르는 DB 함수만 갈아끼운다(순수 계산은 진짜를 그대로 쓴다)."""
    from service import mkt_page
    monkeypatch.setattr(mkt_store, "sales_between",
                        lambda d1, d2: list(sales))
    monkeypatch.setattr(mkt_store, "last_pos_date", lambda: last_pos)
    monkeypatch.setattr(mkt_store, "crawler_daily_sales",
                        lambda d1, d2: [])      # 잠정치는 sales 에 이미 섞어 넣는다
    monkeypatch.setattr(mkt_store, "campaigns_overlapping",
                        lambda d1, d2: list(camps))
    monkeypatch.setattr(mkt_store, "distinct_products", lambda days=120: [])
    return mkt_page


def _ledger_plus_provisional():
    """장부는 7/31까지 매장+배달, 8월은 배달만(크롤러 잠정치) — 실제 운영 모양."""
    rows = []
    d = date(2026, 6, 1)
    while d <= date(2026, 7, 31):
        if d.weekday() != 0:                  # 월요일 휴무
            rows.append({"sale_date": str(d), "channel": "store",
                         "amount": 500000, "source": "tos"})
            rows.append({"sale_date": str(d), "channel": "baemin",
                         "amount": 700000, "source": "tos"})
        d += timedelta(days=1)
    d = date(2026, 8, 1)
    while d <= date(2026, 8, 29):
        if d.weekday() != 0:
            rows.append({"sale_date": str(d), "channel": "baemin",
                         "amount": 700000, "source": "crawler"})
        d += timedelta(days=1)
    return rows


def test_unexplained_reminder_skips_provisional_days(monkeypatch):
    """잠정(장부 미반영) 구간은 '매출 급락' 리마인드를 띄우면 안 된다.

    실사고 경로(2026-08-30): 배달 주문 수집을 켜자 8월 총매출이 '배달만'이
    되는데, 기준선은 매장까지 든 7월 값이라 8월 거의 모든 날이 -40%로 잡혀
    "❓ 매출이 평소보다 많이 내렸어요"가 매일 떴다. 캘린더 신호점은 이미
    last_pos 로 게이트돼 있었는데 리마인드만 빠져 있었다.
    """
    mkt_page = _stub_store(monkeypatch, _ledger_plus_provisional(),
                           date(2026, 7, 31))
    v = mkt_page.build_month_view(2026, 8, today=date(2026, 8, 30))
    unexplained = [r for r in v["reminders"] if r["kind"] == "unexplained"]
    assert unexplained == [], f"잠정 구간에 허위 리마인드: {unexplained}"


def test_unexplained_reminder_still_fires_on_ledger_days(monkeypatch):
    """장부가 든 날의 진짜 급등은 여전히 잡아야 한다(게이트가 과하지 않은가)."""
    rows = _ledger_plus_provisional()
    spike = "2026-07-29"                      # 수요일, 장부 구간
    rows = [r for r in rows if r["sale_date"] != spike]
    rows.append({"sale_date": spike, "channel": "store",
                 "amount": 500000, "source": "tos"})
    rows.append({"sale_date": spike, "channel": "baemin",
                 "amount": 1400000, "source": "tos"})   # 총 1.9M vs 기대 1.2M
    mkt_page = _stub_store(monkeypatch, rows, date(2026, 7, 31))
    v = mkt_page.build_month_view(2026, 7, today=date(2026, 8, 30))
    unexplained = [r for r in v["reminders"] if r["kind"] == "unexplained"]
    assert any(r["date"] == spike for r in unexplained), \
        f"장부 구간의 진짜 급등을 놓쳤다: {unexplained}"
