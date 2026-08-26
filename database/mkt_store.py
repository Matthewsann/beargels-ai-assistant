"""마케팅 캘린더 데이터 계층 (schema_v7.sql).

웹(service/app.py)과 집 PC 일꾼(worker/pos_import.py)이 같이 쓴다.
연결은 database/supabase_client.py 의 get_client() 를 재사용한다.

구성:
  · mkt_campaigns       — 마케팅 실행 기록 (기간/카테고리/타겟 상품/비용)
  · sales_daily         — 포스 장부에서 온 일별 채널별 매출
  · product_sales_daily — 일별 상품별 매출 (타겟 상품 효과 분석용)
  · pos_files           — 장부 파일 반영 로그 (같은 파일 재파싱 방지)

효과 계산(요일 보정)은 순수 함수로 아래에 같이 둔다 — 테스트 대상.
"""

import logging
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from .supabase_client import get_client

logger = logging.getLogger(__name__)

CAMPAIGNS = "mkt_campaigns"
SALES = "sales_daily"
PRODUCTS = "product_sales_daily"
POS_FILES = "pos_files"

# 카테고리 (화면 표기와 색은 service 쪽에서)
CATEGORIES = ("delivery", "sns", "place", "store", "var")

_DELIVERY_CHANNELS = ("baemin", "coupang", "yogiyo", "ddangyo")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _d(v):
    """date | 'YYYY-MM-DD' → date"""
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


# ---------------------------------------------------------------------------
# 캠페인
# ---------------------------------------------------------------------------

def create_campaign(title, category, start_date, end_date=None,
                    target_products=None, cost=None, memo=None):
    row = {
        "title": (title or "").strip(),
        "category": category if category in CATEGORIES else "store",
        "start_date": str(_d(start_date)),
        "end_date": str(_d(end_date)) if end_date else None,
        "target_products": target_products or None,
        "cost": cost,
        "memo": (memo or "").strip() or None,
        "status": "done" if end_date else "live",
    }
    res = get_client().table(CAMPAIGNS).insert(row).execute()
    return res.data[0]["id"] if res.data else None


def update_campaign(cid, **fields):
    allowed = {"title", "category", "start_date", "end_date",
               "target_products", "cost", "memo", "status"}
    patch = {k: v for k, v in fields.items() if k in allowed}
    if "end_date" in patch and patch["end_date"]:
        patch["end_date"] = str(_d(patch["end_date"]))
        patch.setdefault("status", "done")
    if patch:
        get_client().table(CAMPAIGNS).update(patch).eq("id", cid).execute()


def delete_campaign(cid):
    get_client().table(CAMPAIGNS).delete().eq("id", cid).execute()


def get_campaign(cid):
    res = get_client().table(CAMPAIGNS).select("*").eq("id", cid).execute()
    return res.data[0] if res.data else None


def campaigns_overlapping(d1, d2):
    """[d1, d2] 와 기간이 겹치는 캠페인 전부 (진행중=end null 포함)."""
    d1, d2 = str(_d(d1)), str(_d(d2))
    q = (get_client().table(CAMPAIGNS).select("*")
         .lte("start_date", d2)
         .or_(f"end_date.gte.{d1},end_date.is.null")
         .order("start_date"))
    return q.execute().data or []


def live_campaigns():
    res = (get_client().table(CAMPAIGNS).select("*")
           .eq("status", "live").order("start_date").execute())
    return res.data or []


# ---------------------------------------------------------------------------
# 매출 (읽기)
# ---------------------------------------------------------------------------

def sales_between(d1, d2):
    """[d1,d2] sales_daily 원본 행들."""
    res = (get_client().table(SALES).select("sale_date,channel,amount,orders_count,source")
           .gte("sale_date", str(_d(d1))).lte("sale_date", str(_d(d2)))
           .limit(20000).execute())
    return res.data or []


def product_sales_between(d1, d2, products=None):
    q = (get_client().table(PRODUCTS)
         .select("sale_date,product,qty,amount,source")
         .gte("sale_date", str(_d(d1))).lte("sale_date", str(_d(d2))))
    if products:
        q = q.in_("product", list(products))
    return q.limit(50000).execute().data or []


def last_pos_date():
    """장부(포스)가 반영된 마지막 날짜. 없으면 None."""
    res = (get_client().table(SALES).select("sale_date")
           .in_("source", ["tos", "imu"])
           .order("sale_date", desc=True).limit(1).execute())
    return _d(res.data[0]["sale_date"]) if res.data else None


def distinct_products(days=180):
    """최근 N일 판매된 상품명 목록 (타겟 자동 인식·자동완성용)."""
    since = str(date.today() - timedelta(days=days))
    res = (get_client().table(PRODUCTS).select("product,qty")
           .gte("sale_date", since).limit(50000).execute())
    agg = defaultdict(int)
    for r in (res.data or []):
        agg[r["product"]] += r.get("qty") or 0
    return [p for p, _ in sorted(agg.items(), key=lambda x: -x[1])]


def crawler_daily_sales(d1, d2):
    """orders(크롤러) 기반 일별 채널 매출 — 장부 미반영 기간의 잠정치."""
    res = (get_client().table("orders").select("ordered_date,platform,price")
           .gte("ordered_date", str(_d(d1))).lte("ordered_date", str(_d(d2)))
           .limit(20000).execute())
    agg = defaultdict(lambda: [0, 0])   # (date, platform) -> [amount, count]
    for r in (res.data or []):
        if not r.get("ordered_date"):
            continue
        key = (r["ordered_date"], r.get("platform") or "etc")
        agg[key][0] += r.get("price") or 0
        agg[key][1] += 1
    return [{"sale_date": k[0], "channel": k[1], "amount": v[0],
             "orders_count": v[1], "source": "crawler"} for k, v in agg.items()]


# ---------------------------------------------------------------------------
# 매출 (쓰기 — 일꾼 전용)
# ---------------------------------------------------------------------------

def upsert_sales(rows):
    """[{sale_date, channel, amount, orders_count, source}] 일괄 반영(대체)."""
    if not rows:
        return 0
    for r in rows:
        r["imported_at"] = _now()
    get_client().table(SALES).upsert(
        rows, on_conflict="sale_date,channel,source").execute()
    return len(rows)


def upsert_product_sales(rows):
    if not rows:
        return 0
    for r in rows:
        r["imported_at"] = _now()
    # PostgREST 페이로드 한도를 피해 나눠 보낸다
    for i in range(0, len(rows), 500):
        get_client().table(PRODUCTS).upsert(
            rows[i:i + 500], on_conflict="sale_date,product,source").execute()
    return len(rows)


def pos_file_done(file_name, file_mtime):
    """이 (파일, 수정시각)을 이미 반영했나?"""
    res = (get_client().table(POS_FILES).select("id,status")
           .eq("file_name", file_name).eq("file_mtime", file_mtime)
           .execute())
    return bool(res.data and res.data[0].get("status") == "done")


def log_pos_file(file_name, file_mtime, file_size=None, kind=None,
                 date_from=None, date_to=None, status="done", note=None):
    row = {
        "file_name": file_name, "file_mtime": file_mtime,
        "file_size": file_size, "kind": kind,
        "date_from": str(_d(date_from)) if date_from else None,
        "date_to": str(_d(date_to)) if date_to else None,
        "status": status, "note": (note or "")[:300] or None,
        "imported_at": _now(),
    }
    get_client().table(POS_FILES).upsert(
        row, on_conflict="file_name,file_mtime").execute()


def request_pos_import(by=None):
    """웹 '장부 지금 반영' → 집 PC 일꾼에게 잡 요청 (연타 방지 포함)."""
    live = (get_client().table("jobs").select("*")
            .eq("kind", "pos_import")
            .in_("status", ["pending", "running"])
            .order("requested_at", desc=True).limit(1).execute().data)
    if live:
        return live[0]
    row = {"kind": "pos_import", "status": "pending", "requested_by": by or ""}
    return (get_client().table("jobs").insert(row).execute().data or [None])[0]


# ---------------------------------------------------------------------------
# 집계·효과 계산 (순수 함수 — 테스트 대상)
# ---------------------------------------------------------------------------

# 배달 채널은 같은 날 여러 출처가 있으면 이중계상 — 우선순위가 높은 출처만 쓴다.
# (매장은 반대: IMU=키오스크 + TOS=포스가 서로 다른 몫이라 '합산'이 맞다 —
#  2026-08-27 검증: 1월 매장 IMU 6,417,300 + TOS 4,851,645 = 장부와 원 단위 일치)
_SOURCE_RANK = {"tos": 3, "imu": 3, "baemin_xls": 2, "coupang_xls": 2,
                "crawler": 1}


def totals_by_date(sales_rows):
    """sales_daily 행들 → {date: {'total':, 'store':, 'delivery':, 채널별...}}"""
    # (date, channel, source) 별로 먼저 모은다 (같은 키 중복행은 합산)
    per = defaultdict(int)
    for r in sales_rows:
        d = str(r["sale_date"])[:10]
        per[(d, r.get("channel") or "etc", r.get("source") or "?")] += \
            r.get("amount") or 0
    days = defaultdict(lambda: defaultdict(dict))   # date -> ch -> {source: amt}
    for (d, ch, src), amt in per.items():
        days[d][ch][src] = amt
    out = {}
    for d, chans in days.items():
        flat = {}
        for ch, by_src in chans.items():
            if ch == "store":
                flat[ch] = sum(by_src.values())
            else:
                best = max(_SOURCE_RANK.get(s, 0) for s in by_src)
                flat[ch] = sum(v for s, v in by_src.items()
                               if _SOURCE_RANK.get(s, 0) == best)
        total = sum(flat.values())
        delivery = sum(v for c, v in flat.items() if c in _DELIVERY_CHANNELS)
        out[d] = {"total": total, "store": flat.get("store", 0),
                  "delivery": delivery, **flat}
    return out


def weekday_baseline(daily_totals, target_day, weeks=4, key="total"):
    """target_day 와 같은 요일의 직전 `weeks`주 평균 (0원=휴무 제외).
    daily_totals: totals_by_date() 결과. target_day: date."""
    target_day = _d(target_day)
    vals = []
    for w in range(1, weeks + 1):
        d = target_day - timedelta(days=7 * w)
        v = (daily_totals.get(str(d)) or {}).get(key, 0)
        if v > 0:
            vals.append(v)
    return (sum(vals) / len(vals)) if vals else None


def day_signal(daily_totals, day, threshold=0.10):
    """그날 총매출이 요일 평균 대비 ±threshold 이상이면 +1/-1, 아니면 0."""
    v = (daily_totals.get(str(_d(day))) or {}).get("total", 0)
    if v <= 0:
        return 0
    base = weekday_baseline(daily_totals, day)
    if not base:
        return 0
    diff = v / base - 1
    if diff >= threshold:
        return 1
    if diff <= -threshold:
        return -1
    return 0


def _period_days(start, end):
    start, end = _d(start), _d(end)
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def campaign_effect(camp, sales_rows, product_rows, today=None):
    """캠페인 효과 요약 (요일 보정).

    sales_rows / product_rows 는 [시작-56일, 종료] 범위를 담아 호출한다.
    반환: dict(기간, 실제/기대 매출, 채널, 타겟 상품, 증분, ROAS, 주의 플래그)
    """
    today = _d(today or date.today())
    start = _d(camp["start_date"])
    end = _d(camp["end_date"]) if camp.get("end_date") else today
    end = min(end, today)
    if end < start:
        end = start
    days = _period_days(start, end)
    n_days = len(days)

    daily = totals_by_date(sales_rows)

    def sum_actual_expected(key):
        actual = expected = 0
        covered = 0
        for d in days:
            row = daily.get(str(d))
            v = (row or {}).get(key, 0)
            base = weekday_baseline(daily, d, key=key)
            if row is None and base is None:
                continue
            actual += v
            if base:
                expected += base
                covered += 1
        return actual, expected, covered

    out = {"id": camp["id"], "title": camp["title"],
           "start": str(start), "end": str(end), "days": n_days,
           "short": n_days < 7}

    for key, name in (("total", "total"), ("store", "store"),
                      ("delivery", "delivery")):
        actual, expected, covered = sum_actual_expected(key)
        pct = (actual / expected - 1) if expected else None
        out[name] = {"actual": actual, "expected": round(expected),
                     "pct": pct, "covered_days": covered}

    # 증분 ROAS (총매출 기준)
    cost = camp.get("cost") or 0
    uplift = out["total"]["actual"] - out["total"]["expected"]
    out["uplift"] = uplift
    out["roas"] = round(uplift / cost, 1) if cost > 0 else None

    # 타겟 상품 — 기간 일평균 vs 직전 4주 일평균 (0판매일 포함, 휴무 제외)
    targets = camp.get("target_products") or []
    out["targets"] = []
    if targets:
        open_days = {str(d) for d in days
                     if (daily.get(str(d)) or {}).get("total", 0) > 0}
        pre_start, pre_end = start - timedelta(days=28), start - timedelta(days=1)
        pre_open = {str(d) for d in _period_days(pre_start, pre_end)
                    if (daily.get(str(d)) or {}).get("total", 0) > 0}
        agg = defaultdict(lambda: [0, 0, 0, 0])   # product -> [qty, amt, pre_qty, pre_amt]
        for r in product_rows:
            p = r["product"]
            if p not in targets:
                continue
            d = str(r["sale_date"])[:10]
            if d in open_days:
                agg[p][0] += r.get("qty") or 0
                agg[p][1] += r.get("amount") or 0
            elif d in pre_open:
                agg[p][2] += r.get("qty") or 0
                agg[p][3] += r.get("amount") or 0
        for p in targets:
            qty, amt, pre_qty, pre_amt = agg.get(p, [0, 0, 0, 0])
            n1, n0 = max(len(open_days), 1), max(len(pre_open), 1)
            avg, pre_avg = qty / n1, pre_qty / n0
            out["targets"].append({
                "product": p,
                "qty": qty, "amount": amt,
                "qty_per_day": round(avg, 1),
                "pre_qty_per_day": round(pre_avg, 1),
                "qty_pct": (avg / pre_avg - 1) if pre_avg else None,
            })
    return out


def extract_targets(title, product_names):
    """제목에서 상품명 자동 인식 — 긴 이름 우선, 공백 무시 부분일치."""
    norm_title = re.sub(r"\s+", "", title or "")
    hits = []
    for p in sorted(product_names, key=len, reverse=True):
        np = re.sub(r"\s+", "", p)
        if len(np) >= 2 and np in norm_title:
            if not any(re.sub(r"\s+", "", h) in np or np in re.sub(r"\s+", "", h)
                       for h in hits):
                hits.append(p)
    return hits[:3]
