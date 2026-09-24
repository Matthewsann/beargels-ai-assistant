"""배민 주문 원본({order, settle}) → 수수료 항목 (2026-09-23).

실측 근거: 셀프서비스 주문내역 API v4/orders 의 원소(주문 T2GD00017RBB, 9/17):
  주문금액 21,100 · 중개이용료 −1,412 · 고객할인비용 −3,000 · 배달비 −3,300 ·
  결제정산수수료 −543 · 부가세 −525 · 입금예정 12,320
"""
import json

from crawler.baemin import BaeminCrawler
from database import platform_fees as pf

ITEM = {
    "order": {"orderNumber": "T2GD00017RBB", "status": "CLOSED", "deliveryType": "DELIVERY", "payType": "BARO",
              "payAmount": 18100, "orderDateTime": "2026-09-17T20:18:03", "shopNumber": 14794435,
              "itemsSummary": "베이글 샌드위치 외 1", "items": [{"name": "베이글 샌드위치"}, {"name": "라떼"}],
              "adCampaign": {"id": "13227142", "key": "BAEMIN_1_PLUS"}, "orderInstantDiscountAmount": 3000},
    "settle": {"notDisplayReason": None, "orderBrokerageAmount": 16688,
               "orderBrokerageItems": [{"code": "ORDER_AMOUNT", "amount": 21100}, {"code": "ADVERTISE_FEE", "amount": -1412},
                                       {"code": "DISCOUNT_AMOUNT", "amount": -3000}],
               "deliveryItems": [{"code": "DELIVERY_SUPPLY_PRICE", "amount": -3300}, {"code": "DEVLIERY_TIP_INSTANT_DISCOUNT", "amount": 0},
                                 {"code": "BAEMIN_CLUB_INSTANT_DISCOUNT", "amount": 0}],
               "etcItems": [{"code": "SERVICE_FEE", "amount": -543}],
               "deductionAmountTotalVat": -525, "depositDueAmount": 12320, "total": 12320},
}
NOT_READY = {"order": dict(ITEM["order"], orderNumber="NEW"),
             "settle": {"notDisplayReason": "NOT_READY", "orderBrokerageItems": [], "total": 0}}


def test_정산_항목이_화면과_같다():
    f = pf.order_fees_baemin(ITEM)
    assert f["sales"] == 21100 and f["service_fee"] == 1412 and f["coupon"] == 3000
    assert f["delivery_fee"] == 3300 and f["payment_fee"] == 543 and f["vat"] == 525 and f["ad_fee"] == 0
    assert f["net"] == 12320            # = 포털 입금예정
    assert f["orders"] == 1 and f["cancelled"] == 0


def test_정산이_아직_없으면_None_이라_그날_합계에_안_들어간다():
    assert pf.order_fees_baemin(NOT_READY) is None
    assert pf.order_fees_baemin("배달완료 T2GJ0000 2026. 09. 23. ... 12,500원") is None   # 옛 표 텍스트 raw


def test_summarize는_두_플랫폼을_따로_합친다():
    rows = [
        {"platform": "baemin", "ordered_date": "2026-09-17", "status": "배달완료", "raw": json.dumps(ITEM, ensure_ascii=False)},
        {"platform": "baemin", "ordered_date": "2026-09-17", "status": "배달완료", "raw": NOT_READY},
        {"platform": "baemin", "ordered_date": "2026-09-16", "status": "배달완료", "raw": "옛 텍스트"},
        {"platform": "coupang", "ordered_date": "2026-09-17", "status": "COMPLETED",
         "raw": {"salePrice": 10000, "actuallyAmount": 7000, "orderSettlement": {"commissionVat": 300}}},
    ]
    agg = pf.summarize(rows)
    assert set(agg) == {("baemin", "2026-09-17"), ("coupang", "2026-09-17")}
    assert agg[("baemin", "2026-09-17")]["orders"] == 1 and agg[("baemin", "2026-09-17")]["net"] == 12320


def test_api_원소_정규화():
    o = BaeminCrawler._normalize_api_order(ITEM)
    assert o["platform"] == "baemin" and o["order_no"] == "T2GD00017RBB" and o["status"] == "배달완료"
    assert o["ordered_date"] == "2026-09-17" and o["ordered_at"] == "2026. 09. 17. 20:18:03"
    assert o["price"] == 18100 and o["pay_type"] == "바로결제" and o["ad_service"] == "배민배달"
    assert json.loads(o["raw"])["settle"]["total"] == 12320
    assert BaeminCrawler._normalize_api_order({"order": {}}) is None


SETTLE = {"give_id": 538297677, "start": "2026-09-18", "end": "2026-09-20", "deposit": 464541, "cpc_total": -6178,
          "cpc_daily": {"2026-09-20": 3796, "2026-09-19": 1716, "2026-09-18": 104}, "cpc_vat": 562,
          "raw": {"summary": {}}}


def test_정산_명세의_클릭광고를_날짜별로_나누고_부가세도_비율로():
    ads, sts = pf.settlements_to_ad_daily([SETTLE, dict(SETTLE, give_id=1, cpc_daily={}, cpc_total=None)])
    assert len(sts) == 2 and sts[0]["give_id"] == 538297677 and sts[0]["deposit"] == 464541
    assert [a["day"] for a in ads] == ["2026-09-18", "2026-09-19", "2026-09-20"]   # 클릭 없는 명세는 광고 행 없음
    by = {a["day"]: a for a in ads}
    assert by["2026-09-20"]["ad_fee"] == 3796 and by["2026-09-18"]["ad_fee"] == 104
    assert sum(a["ad_vat"] for a in ads) == 562                    # 부가세 합이 맞는다
    assert by["2026-09-20"]["ad_vat"] == round(562 * 3796 / 5616)


def test_rebuild는_배민_광고비를_그날_ad_fee에_얹는다(monkeypatch):
    from datetime import date
    seen = {}

    class T:
        def __init__(self, name): self.name = name
        def select(self, *a): return self
        def in_(self, *a): return self
        def eq(self, *a): return self
        def gte(self, *a): return self
        def lte(self, *a): return self
        def order(self, *a): return self
        def range(self, *a): return self
        def upsert(self, rows, on_conflict=None): seen["rows"] = rows; return self
        def execute(self):
            if self.name == "orders":
                return type("R", (), {"data": [{"platform": "baemin", "ordered_date": "2026-09-18", "status": "배달완료", "raw": ITEM}]})()
            if self.name == "platform_ad_daily":
                return type("R", (), {"data": [{"day": "2026-09-18", "ad_fee": 104, "ad_vat": 10, "support": 1000, "refund": 500},
                                               {"day": "2026-09-19", "ad_fee": 1716, "ad_vat": 172}]})()
            return type("R", (), {"data": []})()

    monkeypatch.setattr(pf, "get_client", lambda: type("C", (), {"table": lambda s, n: T(n)})())
    pf.rebuild(date(2026, 9, 18), date(2026, 9, 19))
    by = {r["day"]: r for r in seen["rows"] if r["platform"] == "baemin"}   # 가짜 표는 플랫폼을 안 가린다
    assert by["2026-09-18"]["ad_fee"] == 114 and by["2026-09-18"]["net"] == 12320 - 114 + 1000 - 500
    assert by["2026-09-18"]["coupon"] == 3000 - 1000 and by["2026-09-18"]["sales"] == 21100 - 500
    assert by["2026-09-19"]["orders"] == 0 and by["2026-09-19"]["ad_fee"] == 1888   # 주문 없는 날도 광고비는 남긴다


def test_지원금과_부분환불은_정산기간_날짜에_고르게_나눈다():
    s = dict(SETTLE, cpc_daily={}, cpc_vat=0, support=6312, refund=2500)
    ads, _ = pf.settlements_to_ad_daily([s])
    assert [a["day"] for a in ads] == ["2026-09-18", "2026-09-19", "2026-09-20"]
    assert sum(a["support"] for a in ads) == 6312 and sum(a["refund"] for a in ads) == 2500
    assert ads[0]["support"] == 2104 and ads[-1]["support"] == 2104 and ads[0]["ad_fee"] == 0   # 빠진 열은 0 으로


def test_쿠팡_보상은_보상_예정건만_날짜별로():
    from crawler.coupang import CoupangCrawler
    items = [CoupangCrawler._normalize_compensation({"transactionDate": "2026-09-20T17:32:00+09:00", "abbrOrderId": "A",
                                                      "orderType": "REGULAR", "cancelReason": "배달지연",
                                                      "compensationStatus": "WILL_BE_COMPENSATED",
                                                      "eventAmount": {"currencyCode": "KRW", "units": 31000}}),
             CoupangCrawler._normalize_compensation({"transactionDate": "2026-09-20T10:00:00+09:00", "abbrOrderId": "B",
                                                      "orderType": "REGULAR", "cancelReason": "고객 취소",
                                                      "compensationStatus": "NOT_ELIGIBLE_FOR_APPEAL",
                                                      "eventAmount": {"units": 15600}})]
    assert items[0]["paid"] and not items[1]["paid"]
    rows = pf.compensations_to_daily(items)
    assert rows == [{"platform": "coupang", "day": "2026-09-20", "comp": 31000, "source": "compensation", "updated_at": rows[0]["updated_at"]}]
