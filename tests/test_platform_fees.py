"""배달 플랫폼 수수료 집계 (2026-09-23) — 쿠팡 주문 원본 → 일별 합계.

실측 근거: 2026-09-22 포털 주문 2D1LD0 을 펼친 화면
  매출액 15,600 / 상점부담 쿠폰 −1,500 / 중개 이용료 −1,217 / 결제대행사 수수료 −423
  / 배달비 −3,300 / 부가세 −494 / 기본 정산 예정 8,666
과 같은 주문의 응답 orderSettlement.
"""
import json

from database import platform_fees as pf

RAW = {
    "abbrOrderId": "2D1LD0", "salePrice": 15600, "totalAmount": 14100,
    "actuallyAmount": 8666, "discountPrice": 1500, "canceledAmount": 0,
    "status": "COMPLETED",
    "orderSettlement": {
        "commissionTotal": 5434, "commissionVat": 494,
        "serviceSupplyPrice": {"basicSupplyPrice": 1217, "appliedSupplyPrice": 1217},
        "paymentSupplyPrice": {"basicSupplyPrice": 423, "appliedSupplyPrice": 423},
        "deliverySupplyPrice": {"basicSupplyPrice": 3400, "appliedSupplyPrice": 3300},
        "advertisingSupplyPrice": {"basicSupplyPrice": 0, "appliedSupplyPrice": 210},
        "storePromotionAmount": 1500, "subtractAmount": 6934,
    },
}
CANCEL = {"abbrOrderId": "X", "salePrice": 14500, "actuallyAmount": 14500,
          "canceledAmount": 14500, "status": "CANCELLED",
          "orderSettlement": {"commissionVat": 0, "storePromotionAmount": 0,
                              "deliverySupplyPrice": {"basicSupplyPrice": 3400, "appliedSupplyPrice": 0}}}


def test_한_주문의_항목이_포털_화면과_같다():
    f = pf.order_fees(RAW)
    assert f["sales"] == 15600 and f["coupon"] == 1500
    assert f["service_fee"] == 1217 and f["payment_fee"] == 423
    assert f["delivery_fee"] == 3300      # basic 3,400 이 아니라 적용가 3,300
    assert f["vat"] == 494 and f["ad_fee"] == 210 and f["net"] == 8666
    assert f["orders"] == 1 and f["cancelled"] == 0
    # 매출 − 쿠폰 − 수수료 − 배달비 − 부가세 = 정산 (광고비는 정산에서 따로 빠짐)
    assert f["sales"] - f["coupon"] - f["service_fee"] - f["payment_fee"] - f["delivery_fee"] - f["vat"] == f["net"]


def test_raw가_문자열이어도_읽는다():
    assert pf.order_fees(json.dumps(RAW))["net"] == 8666


def test_취소_주문은_건수만_세고_금액은_넣지_않는다():
    f = pf.order_fees(CANCEL)
    assert f["cancelled"] == 1 and f["orders"] == 0
    assert f["sales"] == 0 and f["delivery_fee"] == 0 and f["net"] == 0


def test_날짜별로_합치고_배민은_아직_안_섞는다():
    rows = [
        {"platform": "coupang", "ordered_date": "2026-09-22", "status": "COMPLETED", "raw": RAW},
        {"platform": "coupang", "ordered_date": "2026-09-22", "status": "COMPLETED", "raw": json.dumps(RAW)},
        {"platform": "coupang", "ordered_date": "2026-09-22", "status": "CANCELLED", "raw": CANCEL},
        {"platform": "coupang", "ordered_date": "2026-09-21", "status": "COMPLETED", "raw": RAW},
        {"platform": "baemin", "ordered_date": "2026-09-22", "status": "COMPLETED", "raw": {"x": 1}},
    ]
    agg = pf.summarize(rows)
    assert set(agg) == {("coupang", "2026-09-22"), ("coupang", "2026-09-21")}
    d = agg[("coupang", "2026-09-22")]
    assert d["orders"] == 2 and d["cancelled"] == 1
    assert d["sales"] == 31200 and d["delivery_fee"] == 6600 and d["net"] == 17332


def test_rebuild는_읽은_것을_날짜별로_upsert한다(monkeypatch):
    from datetime import date
    seen = {}

    class T:
        def __init__(self, name): self.name = name
        def select(self, *a): return self
        def eq(self, *a): return self
        def gte(self, *a): return self
        def lte(self, *a): return self
        def limit(self, *a): return self
        def upsert(self, rows, on_conflict=None):
            seen["rows"], seen["conflict"] = rows, on_conflict; return self
        def execute(self):
            class R: data = [{"platform": "coupang", "ordered_date": "2026-09-22", "status": "COMPLETED", "raw": RAW}]
            return R() if self.name == "orders" else type("R", (), {"data": []})()

    class C:
        def table(self, n): return T(n)

    monkeypatch.setattr(pf, "get_client", lambda: C())
    assert pf.rebuild(date(2026, 9, 20), date(2026, 9, 22)) == 1
    assert seen["conflict"] == "platform,day"
    assert seen["rows"][0]["day"] == "2026-09-22" and seen["rows"][0]["net"] == 8666
