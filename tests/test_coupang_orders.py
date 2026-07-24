"""쿠팡 주문 정규화·날짜변환 순수로직 테스트 (브라우저 없음)."""

from crawler.coupang import CoupangCrawler


SAMPLE_ORDER = {
    "orderId": 1023048559190102016,
    "uniqueOrderId": "1023048559190102016",
    "abbrOrderId": "2MWL50",
    "storeId": 889230,
    "totalAmount": 15700,
    "salePrice": 15700,
    "actuallyAmount": 10204,
    "status": "COMPLETED",
    "createdAt": 1784852830844,  # KST epoch ms
    "items": [
        {"name": "베이컨 에그 샌드위치", "quantity": 1},
        {"name": "플레인 베이글", "quantity": 2},
    ],
}


def test_normalize_order_maps_core_fields():
    o = CoupangCrawler._normalize_order(SAMPLE_ORDER)
    assert o["platform"] == "coupang"
    assert o["order_no"] == "2MWL50"          # abbrOrderId 우선
    assert o["price"] == 15700                # salePrice = 매출
    assert o["status"] == "COMPLETED"
    assert o["menu"] == "베이컨 에그 샌드위치, 플레인 베이글"
    assert o["menus"] == ["베이컨 에그 샌드위치", "플레인 베이글"]


def test_normalize_order_falls_back_to_totalamount():
    order = dict(SAMPLE_ORDER)
    del order["salePrice"]
    o = CoupangCrawler._normalize_order(order)
    assert o["price"] == order["totalAmount"]


def test_normalize_order_createdat_to_kst_date():
    o = CoupangCrawler._normalize_order(SAMPLE_ORDER)
    # 1784852830844 ms → KST 로 2026-07-24
    assert o["ordered_date"] == "2026-07-24"
    assert o["ordered_at"].startswith("2026. 07. 24.")


def test_date_range_ms_explicit_kst_boundaries():
    # 정찰 시 실제 요청과 일치한 값(2026-07-24 기준 days=1 구간).
    start, end = CoupangCrawler._date_range_ms(1, "2026-07-23", "2026-07-24")
    assert start == 1784732400000   # 2026-07-23 00:00:00 KST
    assert end == 1784905199999     # 2026-07-24 23:59:59.999 KST


def test_page_size_clamp_constant():
    # pageSize>10 이면 쿠팡이 빈 응답을 주므로 상한 10.
    assert CoupangCrawler.MAX_PAGE_SIZE == 10
