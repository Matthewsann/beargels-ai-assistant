"""장부 시트(구글 시트 '베어글스_장부' 요약) 파싱·파생 지표·진단 규칙 (2026-09-03).

계약:
  · 요약시트 CSV 에서 'N월' 열을 찾아 START_YM 부터 달을 붙인다(해 넘김 포함)
  · 영업이익 = 정산총액 − 원가 − 고정비 — 시트 값과 원 단위로 맞아야 한다
  · 배달 수수료(실질)는 정산총액에서 역산한다(시트의 '배달 수수료(+광고)' 행은
    정산총액과 안 맞아 참고값으로만)
  · 월이 끝나기 전에 적힌 값은 estimate, 끝난 뒤 갱신된 값은 confirmed
  · 진단: 손익분기 미달이면 '모자랐다' 판정, 할 일은 기대효과 큰 순 3개
"""
from datetime import date, datetime, timezone

import pytest

from database import ledger_store as ls
from service import dashboard_page as dp

CSV = """요약,,,,,,,,,,,,,,,,,
,목표,최저,누적,평균,10월,11월,12월,1월,2월,3월,4월,5월,6월,7월,8월,,,11월
매출총액,"40,000,000","35,000,000",x,x,"20,877,490","28,772,434","32,972,850","26,654,706","31,404,344","31,499,341","35,670,114","45,674,996","38,900,919","33,399,713","37,017,585",,,"28,772,434"
총주문건수,,,,,"1,582","1,978","1,865","1,767","2,163","2,210","2,740","2,877","2,483","2,310",,,,
매장매출,"18,000,000",,,,"12,770,245","14,223,800","14,795,105","11,268,945","17,322,740","13,583,665","16,378,127","18,498,543","16,848,391","13,895,026","15,562,429",,,
매장건수,,,,,"1,191","1,147",851,947,"1,372","1,261","1,663","1,424","1,273","1,232",,,,
배달매출,"22,000,000",,,,"8,107,245","14,548,634","18,177,745","15,385,761","14,609,304","17,915,676","19,291,987","27,176,453","22,052,528","19,504,687","21,455,156",,,
배달건수,,,,,391,831,"1,014",820,791,949,"1,077","1,453","1,210","1,078",,,,
정산총액(실입금금액),"30,660,000",,,,"16,000,516","19,763,568","24,071,673","19,837,512","24,607,961","22,901,314","27,129,226","31,147,993","28,551,795","25,659,250","28,824,051",0,,
매입원가 총액,"14,000,000",,,,"9,131,893","11,350,004","14,168,110","12,691,739","12,437,571","12,092,940","12,186,020","17,549,321","15,275,440","14,174,530","12,956,155",0,,
원가율,35%,35%,,40.54%,43.74%,39.45%,42.97%,47.62%,39.60%,38.39%,34.16%,38.42%,39.27%,42.44%,35.00%,35.00%,,
배달 수수료(+광고),"8,800,000",,,,"3,485,676","5,824,244","9,587,265","8,805,215","8,165,617","9,602,906","11,095,040","13,037,919","12,057,220","12,056,020",#DIV/0!,#DIV/0!,,
배달 수수료(+광고)_%,40%,40%,,49.00%,57%,60%,47%,43%,44%,46%,42%,52%,45%,38.19%,#DIV/0!,#DIV/0!,,
고정비_총액,"13,850,000",,,,"8,368,080","9,418,560","9,852,980","8,375,440","9,597,035","9,547,505","10,676,348","10,554,240","12,442,530","11,903,489","13,156,144",,,
임대료율,9.63%,11.00%,,12.71%,18.44%,13.38%,11.68%,14.44%,12.26%,12.22%,10.79%,8.43%,9.90%,11.53%,10.40%,#DIV/0!,,
인건비,"8,000,000","7,000,000",,137558485.83%,17.24%,14.94%,14.24%,11.92%,13.94%,14.38%,5560028,5444650,7332940,6726790,7905625,#DIV/0!,,
인건비율,20%,20%,,,,,,,,,15.59%,11.92%,18.85%,20.14%,21.36%,#DIV/0!,,
영업이익,"1,210,000",,,,"-1,499,457","-1,004,996","50,583","-1,229,667","2,573,355","1,260,869","4,266,858","3,044,432","833,825","-418,769","2,711,753",0,,
순이익,"1,210,000",,,,"-1,554,457","-1,172,350","50,583","-1,242,313","2,340,709","1,130,869","4,266,858","2,494,432","778,825","-418,769","2,711,753",0,,
"""
MOD = datetime(2026, 8, 22, 13, 28, 31, tzinfo=timezone.utc)


@pytest.fixture()
def parsed():
    return ls.parse_summary_csv(CSV, MOD)


def test_월_열은_START_YM부터_해를_넘겨_붙인다(parsed):
    rows, _ = parsed
    yms = [r["ym"] for r in rows]
    assert yms[:4] == ["2025-10", "2025-11", "2025-12", "2026-01"]
    assert yms[-1] == "2026-08"
    assert len(yms) == 11                       # 뒤쪽 '11월'(전월 대비 표)은 안 딸려온다


def test_금액_비율_오류셀_파싱(parsed):
    rows, targets = parsed
    jul = next(r for r in rows if r["ym"] == "2026-07")
    assert jul["sales_total"] == 33_399_713
    assert jul["settlement"] == 25_659_250
    assert jul["rent_rate"] == pytest.approx(0.1153)
    assert jul["labor_cost"] == 6_726_790          # 금액 달
    assert jul["labor_rate"] == pytest.approx(0.2014)
    oct_ = next(r for r in rows if r["ym"] == "2025-10")
    assert oct_["labor_rate"] == pytest.approx(0.1724)   # 비율만 있는 달
    assert "labor_cost" not in oct_
    aug = next(r for r in rows if r["ym"] == "2026-08")
    assert "delivery_fees" not in aug              # #DIV/0! 은 비운다
    assert targets["sales_total"] == 40_000_000 and targets["cogs_rate"] == pytest.approx(0.35)
    assert targets["labor_rate"] == pytest.approx(0.20)


def test_월말_전에_적힌_달은_예상치(parsed):
    rows, _ = parsed
    st = {r["ym"]: r["status"] for r in rows}
    assert st["2026-07"] == "confirmed"           # 8/22 수정 > 7/31
    assert st["2026-08"] == "estimate"            # 8/22 수정 ≤ 8/31


def test_영업이익은_정산총액_원가_고정비(parsed):
    rows, _ = parsed
    jul = ls.derive(next(r for r in rows if r["ym"] == "2026-07"))
    assert jul["settlement"] - jul["cogs"] - jul["fixed_cost"] == -418_769 == jul["op_profit"]


def test_배달수수료는_정산총액에서_역산(parsed):
    """시트 '배달 수수료(+광고)' 12,056,020 은 정산총액과 안 맞는다 — 역산 7,448,667 이 진실."""
    rows, _ = parsed
    jul = ls.derive(next(r for r in rows if r["ym"] == "2026-07"))
    assert jul["fees_total"] == 33_399_713 - 25_659_250
    assert jul["card_fees"] == round(13_895_026 * 0.021)
    assert jul["delivery_fees"] == jul["fees_total"] - jul["card_fees"]
    assert jul["delivery_fees_sheet"] == 12_056_020
    assert jul["delivery_fee_rate"] == pytest.approx(0.382, abs=0.001)   # 사양서 §5 '실질 38.2%'
    assert jul["variable_rate"] == pytest.approx(0.656, abs=0.001)
    assert jul["contribution_rate"] == pytest.approx(0.344, abs=0.001)
    assert jul["breakeven"] == round(11_903_489 / jul["contribution_rate"])


def test_파생_객단가와_고정비_분해(parsed):
    rows, _ = parsed
    jul = ls.derive(next(r for r in rows if r["ym"] == "2026-07"))
    assert jul["store_ticket"] == pytest.approx(13_895_026 / 1232)
    assert jul["rent"] == round(0.1153 * 33_399_713)
    assert jul["other_fixed"] == jul["fixed_cost"] - jul["labor"] - jul["rent"]


# ---------------------------------------------------------------------------
# 진단 규칙
# ---------------------------------------------------------------------------

def _month(**over):
    base = {"ym": "2026-07", "sales_total": 33_399_713, "store_sales": 13_895_026, "store_orders": 1232,
            "delivery_sales": 19_504_687, "delivery_orders": 1078, "orders_total": 2310,
            "settlement": 25_659_250, "cogs": 14_174_530, "fixed_cost": 11_903_489,
            "labor_cost": 6_726_790, "labor_rate": 0.2014, "rent_rate": 0.1153, "op_profit": -418_769,
            "status": "confirmed"}
    base.update(over)
    return ls.derive(base)


def test_손익분기_미달_판정():
    L = _month()
    v = dp.verdict(L, {"sales_total": 40_000_000})
    assert v["tone"] == "bad"
    assert "모자랐습니다" in v["body"]
    assert f"{dp.man(L['breakeven']):,}만원" in v["title"]


def test_흑자_판정():
    L = _month(op_profit=2_000_000, settlement=28_000_000)
    v = dp.verdict(L, {"op_rate": 0.03})
    assert v["tone"] in ("ok", "warn") and "남았습니다" in v["title"]


def test_워터폴은_1만원을_다_나눈다():
    L = _month()
    f = dp.waterfall(L)
    names = [r["name"] for r in f["rows"]]
    assert names == ["식자재", "배달 수수료", "카드 수수료", "인건비", "임대료", "기타 고정비"]
    assert f["groups"]["변동비"] == pytest.approx(65.6, abs=0.1)
    assert f["groups"]["고정비"] == pytest.approx(35.6, abs=0.1)
    assert f["remain"] == 10_000 - sum(r["won"] for r in f["rows"])
    assert f["remain"] < 0                        # 7월은 적자


def test_할일은_기대효과_큰_순_3개():
    L = _month()
    P = _month(ym="2026-06", cogs=15_275_440, sales_total=38_900_919, store_sales=16_848_391,
               store_orders=1273, settlement=28_551_795, fixed_cost=12_442_530, op_profit=833_825)
    acts = dp.actions(L, P, {"cogs_rate": 0.35, "labor_rate": 0.20})
    assert len(acts) == 3
    assert [a["rank"] for a in acts] == [1, 2, 3]
    assert acts[0]["gain"] >= acts[1]["gain"] >= acts[2]["gain"]
    titles = " ".join(a["title"] for a in acts)
    assert "원가율" in titles and "객단가" in titles


def test_세부지표_전월대비_방향():
    L, P = _month(), _month(ym="2026-06", sales_total=38_900_919, cogs=15_275_440)
    d = {x["name"]: x for x in dp.details(L, P)}
    assert d["매출 총액"]["chg"] < 0 and d["매출 총액"]["unit"] == "%"
    assert d["원가율"]["unit"] == "%p" and d["원가율"]["inverse"] is True
    assert len(d) == 12


def test_수수료_분해는_매출_비중으로_나눈다():
    L = _month()
    split = dp.fee_split(L, {"baemin": 10_020_000, "coupang": 8_610_000}, {"baemin": 560, "coupang": 479})
    by = {s["key"]: s for s in split}
    assert by["baemin"]["base_pct"] == pytest.approx(11.9, abs=0.05)
    assert by["baemin"]["total_pct"] == pytest.approx(by["coupang"]["total_pct"])   # 같은 실질 수수료율
    assert by["baemin"]["ad"] > by["coupang"]["ad"]


def test_카테고리는_시트값_우선_없으면_이름규칙():
    rows = [{"product": "[SET] 베이글 샌드위치 + 음료", "category": "", "amount": 100},
            {"product": "[SET] 베이글 샌드위치 + 음료", "category": "세트", "amount": 50},
            {"product": "E)아메리카노", "category": "", "amount": 30},
            {"product": "배달비", "category": "", "amount": 999}]
    cats = {c["name"]: c["amount"] for c in dp.category_share(rows)}
    assert cats["세트"] == 150 and cats["커피"] == 30 and "배달비" not in str(cats)


def test_상품_분류_규칙():
    monthly = {"수박 주스": {"2026-07": 230_000, "2026-08": 910_000},
               "버터떡": {"2026-05": 2_160_000, "2026-06": 480_000, "2026-07": 110_000, "2026-08": 0},
               "옛 세트(이름 바뀜)": {"2026-05": 1_570_000, "2026-06": 890_000, "2026-07": 0, "2026-08": 0},
               "올나잇패스": {"2026-08": 840_000}}
    b = dp.product_buckets(monthly, ["2026-05", "2026-06", "2026-07", "2026-08"])
    assert b["push"][0]["product"] == "수박 주스"
    assert any(w["product"] == "올나잇패스" and w["new"] for w in b["watch"])
    cut = [c["product"] for c in b["cut"]]
    assert "버터떡" in cut and "옛 세트(이름 바뀜)" not in cut


def test_시간대_프로필은_날수로_나눈다():
    rows = [{"sale_date": "2026-08-03", "hour": 9, "channel": "store", "amount": 100_000, "source": "tos"},
            {"sale_date": "2026-08-04", "hour": 9, "channel": "store", "amount": 300_000, "source": "tos"},
            {"sale_date": "2026-08-04", "hour": 11, "channel": "baemin", "amount": 50_000, "source": "tos"}]
    hp = dp.hour_profile(rows)
    assert hp["hours"] == [9, 10, 11] and hp["days"] == 2
    assert hp["store"] == [200_000, 0, 0] and hp["delivery"] == [0, 0, 25_000]


def test_요일_프로필():
    daily = {"2026-08-03": {"total": 100, "partial": False}, "2026-08-10": {"total": 300, "partial": False},
             "2026-08-17": {"total": 0, "partial": False}, "2026-08-05": {"total": 500, "partial": True}}
    d = dp.dow_profile(daily, date(2026, 8, 1), date(2026, 8, 31))
    assert d[0] == 200 and d[2] == 0
