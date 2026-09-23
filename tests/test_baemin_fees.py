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
