"""배달 플랫폼 수수료 일별 집계 → platform_fees_daily (schema_v16, 2026-09-23).

사장님(2026-09-22): "매출액 대비 수수료가 얼마나 되고, 배달료가 얼마나 되는지,
광고비는 얼마나 되는지, 기간별로 파악하고 싶어." 쿠팡이츠 포털에서 엑셀을
내려받지 않아도 된다 — 매출 관리 화면이 처음 뜰 때 받는 주문 목록 응답에
건별 정산 항목(`orderSettlement`)이 다 들어 있고, 집 PC 일꾼이 이미 그 원본을
`orders.raw` 에 저장하고 있다(`crawler/coupang.py fetch_orders`, 매일).

여기서는 그 원본을 날짜별로 합친다. 순수 함수(`order_fees`, `summarize`)와
DB 를 읽고 쓰는 함수(`rebuild`)를 나눠 테스트가 DB 없이 돈다.

실측(2026-09-22, 주문 2D1LD0): 매출 15,600 · 쿠폰 −1,500 · 중개 −1,217 ·
결제 −423 · 배달비 −3,300 · 부가세 −494 → 정산 8,666. 응답의 이름은
  serviceSupplyPrice.appliedSupplyPrice  = 중개 이용료
  paymentSupplyPrice.appliedSupplyPrice  = 결제대행사 수수료
  deliverySupplyPrice.appliedSupplyPrice = 배달비 (basic 3,400 → 상생요금제 3,300)
  commissionVat                          = 부가세
  storePromotionAmount                   = 상점부담 쿠폰
  advertisingSupplyPrice.appliedSupplyPrice = 광고비(그 주문에 붙은 CPC)
  actuallyAmount                         = 기본 정산 예정 금액
취소 주문(status CANCELLED)은 수수료가 0이고 salePrice 는 남아 있다 —
건수만 세고 금액엔 넣지 않는다.

배민은 보류(2026-09-22) — 같은 표에 platform='baemin' 으로 들어올 자리만 둔다.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from database.supabase_client import get_client

logger = logging.getLogger(__name__)

COLS = ("orders", "cancelled", "sales", "coupon", "service_fee", "payment_fee",
        "delivery_fee", "vat", "ad_fee", "net")
FEE_COLS = ("coupon", "service_fee", "payment_fee", "delivery_fee", "vat", "ad_fee")


def _applied(d) -> int:
    if not isinstance(d, dict):
        return int(d or 0)
    return int(d.get("appliedSupplyPrice") or 0)


def order_fees(raw, status=None) -> dict:
    """쿠팡 주문 원본 하나 → 항목별 원 단위 dict. 취소면 금액 0, cancelled=1."""
    if isinstance(raw, str):
        raw = json.loads(raw)
    raw = raw or {}
    status = status or raw.get("status") or ""
    s = raw.get("orderSettlement") or {}
    if "CANCEL" in str(status).upper() or (raw.get("canceledAmount") or 0) >= (raw.get("salePrice") or 0) > 0:
        return {c: 0 for c in COLS} | {"cancelled": 1}
    sale = int(raw.get("salePrice") or raw.get("totalAmount") or 0) - int(raw.get("canceledAmount") or 0)
    return {
        "orders": 1, "cancelled": 0,
        "sales": sale,
        "coupon": int(s.get("storePromotionAmount") or 0),
        "service_fee": _applied(s.get("serviceSupplyPrice")),
        "payment_fee": _applied(s.get("paymentSupplyPrice")),
        "delivery_fee": _applied(s.get("deliverySupplyPrice")),
        "vat": int(s.get("commissionVat") or 0),
        "ad_fee": _applied(s.get("advertisingSupplyPrice")),
        "net": int(raw.get("actuallyAmount") or 0),
    }


def _items(lst, code) -> int:
    """배민 settle 항목 목록에서 code 의 amount(없으면 0)."""
    for it in lst or []:
        if it.get("code") == code:
            return int(it.get("amount") or 0)
    return 0


def order_fees_baemin(raw) -> dict | None:
    """배민 주문 원본({order, settle}, 2026-09-23 crawler.fetch_orders_api) → 항목별 원.

    settle 이 아직 없으면(notDisplayReason=NOT_READY — 거래 다음날부터) None:
    그 주문은 이날 합계에 아직 안 넣는다. 매일 수집(최근 3일)이 원본을 다시
    올리면 채워진다. 옛 배민 raw(표 텍스트)도 None.
      수수료 쪽: 중개이용료(ADVERTISE_FEE) · 결제정산수수료(etc SERVICE_FEE) ·
                부가세(deductionAmountTotalVat) · 배달비(DELIVERY_SUPPLY_PRICE)
      광고비 쪽(가게 부담 할인): 고객할인비용(DISCOUNT_AMOUNT) + 배달팁 할인비용 +
                배민클럽 할인비용(가게 몫만 — 우아한형제들 지원분은 상쇄돼 0)
      CPC 광고(우리가게클릭)는 주문에 안 붙는다 → ad_fee 0 (월 청구서로 따로).
    """
    if isinstance(raw, str):
        if not raw.startswith("{"):
            return None
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict) or "settle" not in raw:
        return None
    o, s = raw.get("order") or {}, raw.get("settle") or {}
    if s.get("notDisplayReason") or not s.get("orderBrokerageItems"):
        return None
    st = str(o.get("status") or "").upper()
    if "CANCEL" in st:
        return {c: 0 for c in COLS} | {"cancelled": 1}
    bro, dlv, etc = s.get("orderBrokerageItems"), s.get("deliveryItems"), s.get("etcItems")
    sales = _items(bro, "ORDER_AMOUNT") or (int(o.get("payAmount") or 0) + int(o.get("orderInstantDiscountAmount") or 0))
    service = -_items(bro, "ADVERTISE_FEE")
    coupon = -(_items(bro, "DISCOUNT_AMOUNT") + _items(dlv, "DEVLIERY_TIP_INSTANT_DISCOUNT")
               + _items(dlv, "BAEMIN_CLUB_INSTANT_DISCOUNT"))
    delivery = -_items(dlv, "DELIVERY_SUPPLY_PRICE")
    payment = -sum(int(it.get("amount") or 0) for it in (etc or []) if it.get("code") == "SERVICE_FEE")
    vat = -int(s.get("deductionAmountTotalVat") or 0)
    return {"orders": 1, "cancelled": 0, "sales": sales, "coupon": coupon, "service_fee": service,
            "payment_fee": payment, "delivery_fee": delivery, "vat": vat, "ad_fee": 0,
            "net": sales - coupon - service - payment - delivery - vat}


def summarize(rows) -> dict:
    """orders 행들([{platform, ordered_date, status, raw}]) → {(platform, day): 합계}.

    쿠팡은 order_fees, 배민은 order_fees_baemin(정산이 아직 없는 주문·옛 텍스트
    raw 는 건너뛴다)."""
    out = defaultdict(lambda: {c: 0 for c in COLS})
    for r in rows:
        p = r.get("platform")
        if p not in ("coupang", "baemin") or not r.get("ordered_date"):
            continue
        f = order_fees(r.get("raw"), r.get("status")) if p == "coupang" else order_fees_baemin(r.get("raw"))
        if f is None:
            continue
        acc = out[(p, r["ordered_date"][:10])]
        for c in COLS:
            acc[c] += f[c]
    return dict(out)


def rebuild(start: date, end: date) -> int:
    """orders 에서 [start, end] 를 읽어 platform_fees_daily 에 upsert. 행 수 반환.

    주문이 하나도 없는 날은 행을 안 만든다(장부 없는 날은 빈 채로 — 대시보드
    원칙). 취소만 있는 날은 orders=0 인 행이 생기고 그건 그대로 둔다.
    """
    rows = (get_client().table("orders").select("platform,ordered_date,status,raw")
            .in_("platform", ["coupang", "baemin"])
            .gte("ordered_date", start.isoformat()).lte("ordered_date", end.isoformat())
            .limit(8000).execute().data) or []
    agg = summarize(rows)
    now = datetime.now(timezone.utc).isoformat()
    payload = [{"platform": p, "day": d, "updated_at": now} | v for (p, d), v in agg.items()]
    if payload:
        get_client().table("platform_fees_daily").upsert(payload, on_conflict="platform,day").execute()
    logger.info("플랫폼 수수료 집계 %s~%s: %d일", start, end, len(payload))
    return len(payload)


def rebuild_recent(days: int = 7) -> int:
    end = date.today()
    return rebuild(end - timedelta(days=days), end)


def fees_daily(start: date, end: date, platform="coupang") -> list:
    """대시보드용 읽기: [start, end] 의 일별 행(날짜 오름차순)."""
    return (get_client().table("platform_fees_daily").select("*")
            .eq("platform", platform)
            .gte("day", start.isoformat()).lte("day", end.isoformat())
            .order("day").limit(2000).execute().data) or []


def missing_week(today: date | None = None, lookback_days: int = 120, min_missing: int = 2,
                 platform: str = "coupang"):
    """orders 에 그 플랫폼 주문이 없는 날이 min_missing 일 이상인 가장 오래된 한 주(월~일).

    배민은 정산 항목이 있는 새 원본(raw 가 '{"order"' 로 시작)만 '있음'으로 친다 —
    옛 표 텍스트 raw 로는 수수료를 못 만든다.

    (start, end) 또는 None. 어제까지만 본다(오늘은 아직 쌓이는 중). 가게가 문을
    연 날엔 쿠팡 주문이 0건인 날이 거의 없으므로 '행이 없는 날 = 안 긁은 날'.
    일꾼 maybe_fee_backfill 이 2시간마다 이걸 하나 골라 채운다 — 한 번에 몰아
    긁으면 포털이 막는다(2026-09-23 실측: 10056 레이트리밋 뒤 Akamai 403).
    """
    today = today or date.today()
    end = today - timedelta(days=1)
    start = end - timedelta(days=lookback_days)
    q = (get_client().table("orders").select("ordered_date").eq("platform", platform)
         .gte("ordered_date", start.isoformat()).lte("ordered_date", end.isoformat()))
    if platform == "baemin":
        q = q.like("raw", '{"order"%')
    rows = q.limit(20000).execute().data or []
    have = {r["ordered_date"][:10] for r in rows if r.get("ordered_date")}
    monday = start - timedelta(days=start.weekday())
    while monday <= end:
        sunday = min(monday + timedelta(days=6), end)
        days = [(monday + timedelta(days=i)) for i in range((sunday - monday).days + 1)]
        missing = [d for d in days if d.isoformat() not in have]
        if len(missing) >= min_missing:
            return monday, sunday
        monday += timedelta(days=7)
    return None


def request_backfill(start: date, end: date, by=None):
    """집 PC 일꾼에게 '이 기간 쿠팡 주문을 되긁고 집계하라' 잡(orders_backfill).

    포털은 페이지당 10건씩 페이지를 리로드하며 받으므로 두 달이면 10분 넘게
    걸린다 — 웹 요청이 아니라 잡으로 돌린다. 같은 잡이 살아 있으면 그걸 돌려준다.
    """
    live = (get_client().table("jobs").select("*").eq("kind", "orders_backfill")
            .in_("status", ["pending", "running"]).limit(1).execute().data)
    if live:
        return live[0]
    # jobs 표엔 payload 열이 없다 — 다른 잡들처럼 message 에 인자를 싣는다
    row = {"kind": "orders_backfill", "status": "pending", "requested_by": by or "",
           "message": f"{start.isoformat()}..{end.isoformat()}"}
    return (get_client().table("jobs").insert(row).execute().data or [None])[0]
