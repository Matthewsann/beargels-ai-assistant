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
import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from .supabase_client import get_client

logger = logging.getLogger(__name__)

# 서버(PA)는 UTC — 날짜 판단은 전부 매장 시간(KST)으로 (2026-08-30 감사 #17)
KST = timezone(timedelta(hours=9))


def _today_kst():
    return datetime.now(KST).date()

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
        # 종료일이 있어도 미래면 아직 진행중이다 — 가이드가 권하는 "기간을
        # 미리 적는" 사용법에서 시작 전부터 '종료'로 찍히던 버그(2026-08-30).
        "status": ("done" if end_date and _d(end_date) < _today_kst()
                   else "live"),
    }
    res = get_client().table(CAMPAIGNS).insert(row).execute()
    return res.data[0]["id"] if res.data else None


def update_campaign(cid, **fields):
    allowed = {"title", "category", "start_date", "end_date",
               "target_products", "cost", "memo", "status"}
    patch = {k: v for k, v in fields.items() if k in allowed}
    if "end_date" in patch:
        if patch["end_date"]:
            patch["end_date"] = str(_d(patch["end_date"]))
            # 미래 종료일은 '예정된 끝'이지 종료가 아니다
            patch.setdefault(
                "status",
                "done" if _d(patch["end_date"]) < _today_kst() else "live")
        else:
            # 종료일을 지우면 진행중으로 되돌린다 (수정 모달에서 비운 경우)
            patch["end_date"] = None
            patch.setdefault("status", "live")
    if patch:
        get_client().table(CAMPAIGNS).update(patch).eq("id", cid).execute()


def auto_record(title, source_ref, day=None, category="sns", memo=None):
    """앱이 이미 아는 마케팅(블로그 발행·릴스 업로드)을 캘린더에 자동 기록.

    왜: 블로그를 이 앱에서 발행하고 33분 뒤 같은 내용을 사장님이 손으로
    다시 치고 있었다(2026-08-30 감사 — '기록이 안 쌓이는 근본 원인').
    발행하는 순간 여기서 한 줄 만들어 두면, 기록의 절반은 저절로 쌓인다.

    · source_ref("blog#12", "reel#abc")가 memo 마커로 남아 중복 생성을 막는다
    · 타겟 상품은 제목에서 자동 인식(실패해도 기록은 남긴다)
    · 당일 1일짜리 — 기간을 늘리고 싶으면 사장님이 [수정]으로
    반환: 새 캠페인 id, 이미 있으면 None. 예외를 밖으로 던지지 않는다 —
    발행 흐름을 기록 실패가 막으면 안 된다(호출부는 이 함수만 부르면 됨).
    """
    try:
        marker = f"[자동:{source_ref}]"
        dup = (get_client().table(CAMPAIGNS).select("id")
               .like("memo", f"%{marker}%").limit(1).execute().data)
        if dup:
            return None
        targets = []
        try:
            targets = extract_targets(title, distinct_products(days=120))
        except Exception:  # noqa: BLE001 — 타겟 인식 실패가 기록을 막지 않게
            pass
        day = str(_d(day)) if day else str(_today_kst())
        return create_campaign(
            title=(title or "").strip() or "제목 없음",
            category=category, start_date=day, end_date=day,
            target_products=targets or None,
            memo=f"{marker} {memo or ''}".strip())
    except Exception as e:  # noqa: BLE001
        logger.warning("마케팅 자동 기록 실패(%s): %s", source_ref, e)
        return None


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

_PAGE = 1000


def _fetch_all(make_query):
    """PostgREST 는 요청 limit 과 무관하게 **서버가 1000행에서 자른다** —
    실측(2026-08-30): 5~7월 상품 매출 7,015행을 limit(50000)으로 요청해도
    앞 1,000행(5/1~5/14)만 왔고, 그 부분값으로 캠페인 효과가 계산되고
    있었다. range 페이지네이션으로 끝까지 받는다."""
    out, off = [], 0
    while True:
        rows = (make_query().range(off, off + _PAGE - 1).execute().data) or []
        out.extend(rows)
        if len(rows) < _PAGE:
            return out
        off += _PAGE


def sales_between(d1, d2):
    """[d1,d2] sales_daily 원본 행들."""
    return _fetch_all(lambda: (
        get_client().table(SALES)
        .select("sale_date,channel,amount,orders_count,source")
        .gte("sale_date", str(_d(d1))).lte("sale_date", str(_d(d2)))
        .order("sale_date")))


def product_sales_between(d1, d2, products=None):
    def q():
        base = (get_client().table(PRODUCTS)
                .select("sale_date,product,qty,amount,source")
                .gte("sale_date", str(_d(d1))).lte("sale_date", str(_d(d2)))
                .order("sale_date"))
        return base.in_("product", list(products)) if products else base
    return _fetch_all(q)


def last_pos_date():
    """장부(포스)가 반영된 마지막 날짜. 없으면 None."""
    res = (get_client().table(SALES).select("sale_date")
           .in_("source", ["tos", "imu"])
           .order("sale_date", desc=True).limit(1).execute())
    return _d(res.data[0]["sale_date"]) if res.data else None


# 포스에 '상품'으로 찍히지만 메뉴가 아닌 것 — 타겟 후보에서 뺀다.
# ("배달비 무료 이벤트" 같은 제목이 '배달비'를 타겟으로 잡는 오탐, 감사 #1-⑥)
_NON_MENU = ("배달비", "배달료", "포장비", "봉투", "일회용")


# 상품명 목록 캐시 — 6,951행을 7왕복으로 내려받는 무거운 조회인데
# /mkt 페이지뷰마다 돌고 있었다(2026-08-30 비용 감사). 상품명은 분 단위로
# 바뀌는 게 아니므로 10분이면 충분히 신선하다. (프로세스 메모리 — PA 웹과
# 일꾼이 각자 하나씩 갖는다.)
_PRODUCTS_CACHE: dict = {}
_PRODUCTS_TTL_SEC = int(os.getenv("MKT_PRODUCTS_TTL_SEC", "600"))


def distinct_products(days=180):
    """최근 N일 판매된 상품명 목록 (타겟 자동 인식·자동완성용, 10분 캐시)."""
    import time as _time
    hit = _PRODUCTS_CACHE.get(days)
    if hit and _time.time() - hit[0] < _PRODUCTS_TTL_SEC:
        return hit[1]
    since = str(_today_kst() - timedelta(days=days))
    rows = _fetch_all(lambda: (
        get_client().table(PRODUCTS).select("product,qty")
        .gte("sale_date", since).order("sale_date")))
    agg = defaultdict(int)
    for r in rows:
        agg[r["product"]] += r.get("qty") or 0
    out = [p for p, _ in sorted(agg.items(), key=lambda x: -x[1])
           if not any(w in p for w in _NON_MENU)]
    _PRODUCTS_CACHE[days] = (_time.time(), out)
    return out


def crawler_daily_sales(d1, d2):
    """orders(크롤러) 기반 일별 채널 매출 — 장부 미반영 기간의 잠정치."""
    rows = _fetch_all(lambda: (
        get_client().table("orders").select("ordered_date,platform,price")
        .gte("ordered_date", str(_d(d1))).lte("ordered_date", str(_d(d2)))
        .order("ordered_date")))
    agg = defaultdict(lambda: [0, 0])   # (date, platform) -> [amount, count]
    for r in rows:
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
        # partial: 배달만 잡히고 매장이 0인 날 — 장부(매장 포스)가 빠진 날이다.
        # (이 가게는 휴무면 배달도 같이 쉬므로 '매장만 휴무'와 혼동은 없다.)
        # 2025-12 가 통째로 이랬는데, 이 부분값 날들이 요일 평균 표본에 들어가
        # 2026-01 캘린더가 ▲ 27개로 도배됐다(2026-08-30 실측). 비교 계산은
        # 이 플래그가 선 날을 표본·집계 양쪽에서 빼야 한다.
        out[d] = {"total": total, "store": flat.get("store", 0),
                  "delivery": delivery,
                  "partial": flat.get("store", 0) == 0 and delivery > 0,
                  **flat}
    return out


def weekday_baseline(daily_totals, target_day, weeks=4, key="total"):
    """target_day 와 같은 요일의 직전 `weeks`주 평균.
    표본 자격: 매출 > 0 이고, 부분 데이터(partial — 매장 장부 없이 배달만
    잡힌 날)가 아닐 것. daily_totals: totals_by_date() 결과."""
    target_day = _d(target_day)
    vals = []
    for w in range(1, weeks + 1):
        d = target_day - timedelta(days=7 * w)
        row = daily_totals.get(str(d)) or {}
        v = row.get(key, 0)
        if v > 0 and not row.get("partial"):
            vals.append(v)
    return (sum(vals) / len(vals)) if vals else None


def day_signal(daily_totals, day, threshold=0.10):
    """그날 총매출이 요일 평균 대비 ±threshold 이상이면 +1/-1, 아니면 0.
    당일이 부분 데이터(partial)면 비교 자체가 무의미하므로 0."""
    row = daily_totals.get(str(_d(day))) or {}
    v = row.get("total", 0)
    if v <= 0 or row.get("partial"):
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


def campaign_effect(camp, sales_rows, product_rows, today=None, last_pos=None):
    """캠페인 효과 요약 (요일 보정, like-for-like).

    sales_rows / product_rows 는 [시작-56일, 종료] 범위를 담아 호출한다.
    last_pos: 장부(TOS/IMU)가 반영된 마지막 날짜.

    비교 원칙 — **비교할 수 없는 날은 실제·기대 양쪽에서 통째로 뺀다**:
      · 장부 미반영(잠정) 날            → '매출 0 ▼100%' 허수 (2026-08-27)
      · 휴무/매출 0/부분 데이터 날      → 기대치만 청구돼 효과가 깎임·부호 반전
      · 요일 기준선이 없는 날           → 실제만 더해져 ▲166% 뻥튀기 (2026-08-30)
    그래서 pct 는 '비교 가능한 날(covered_days)'끼리의 정직한 비율이고,
    gross 가 기간 전체 실제 매출 합(참고용)이다.
    반환: dict(기간, gross, 채널별 actual/expected/pct/covered_days,
              uplift, roas, targets, top_products, 제외 일수 카운트)
    """
    today = _d(today or _today_kst())
    start = _d(camp["start_date"])
    end = _d(camp["end_date"]) if camp.get("end_date") else today
    end = min(end, today)
    if end < start:
        end = start
    days = _period_days(start, end)
    n_days = len(days)
    last_pos = _d(last_pos) if last_pos else None
    ledger_days = [d for d in days if last_pos is None or d <= last_pos]

    daily = totals_by_date(sales_rows)

    def comparable(d):
        """이날을 비교 집합에 넣어도 되나 — 휴무·부분 데이터 제외."""
        row = daily.get(str(d))
        return (row is not None and row.get("total", 0) > 0
                and not row.get("partial"))

    closed_days = sum(1 for d in ledger_days if not comparable(d))

    def sum_actual_expected(key):
        actual = expected = 0
        covered = uncovered = 0
        for d in ledger_days:
            if not comparable(d):
                continue
            v = daily[str(d)].get(key, 0)
            base = weekday_baseline(daily, d, key=key)
            if not base:
                uncovered += 1      # 이력이 없어 비교 불가 — 양쪽 제외
                continue
            actual += v
            expected += base
            covered += 1
        return actual, expected, covered, uncovered

    out = {"id": camp["id"], "title": camp["title"],
           "start": str(start), "end": str(end), "days": n_days,
           "short": n_days < 7,
           "provisional_days": n_days - len(ledger_days),
           "closed_days": closed_days,
           # 기간 전체 실제 매출 합 (비교 여부와 무관 — '총 얼마 팔았나')
           "gross": sum((daily.get(str(d)) or {}).get("total", 0)
                        for d in ledger_days)}

    for key, name in (("total", "total"), ("store", "store"),
                      ("delivery", "delivery")):
        actual, expected, covered, uncovered = sum_actual_expected(key)
        pct = (actual / expected - 1) if expected else None
        out[name] = {"actual": actual, "expected": round(expected),
                     "pct": pct, "covered_days": covered,
                     "uncovered_days": uncovered}

    # 증분 매출/ROAS — 비교 가능한 날 기준(정직한 값)
    cost = camp.get("cost") or 0
    uplift = out["total"]["actual"] - out["total"]["expected"]
    out["uplift"] = uplift if out["total"]["expected"] else None
    out["roas"] = (round(uplift / cost, 1)
                   if cost > 0 and out["total"]["expected"] else None)

    # ---- 상품 효과 --------------------------------------------------------
    # 분모는 '상품 데이터가 실제로 있는 날'만 — product_sales_daily 는 포스
    # 장부에서만 오므로, 매출 총액이 있어도 상품 행이 없는 날(크롤러 잠정 등)
    # 을 분모에 넣으면 일평균이 희석돼 ▲118% 같은 허수가 난다(2026-08-30).
    product_days = {str(r["sale_date"])[:10] for r in product_rows}
    open_days = {str(d) for d in ledger_days if comparable(d)} & product_days
    pre_start, pre_end = start - timedelta(days=28), start - timedelta(days=1)
    pre_open = {str(d) for d in _period_days(pre_start, pre_end)
                if (last_pos is None or d <= last_pos) and comparable(d)
                } & product_days

    def per_day_stats(match_fn, label):
        qty = amt = pre_qty = pre_amt = 0
        for r in product_rows:
            if not match_fn(r["product"]):
                continue
            d = str(r["sale_date"])[:10]
            if d in open_days:
                qty += r.get("qty") or 0
                amt += r.get("amount") or 0
            elif d in pre_open:
                pre_qty += r.get("qty") or 0
                pre_amt += r.get("amount") or 0
        n1, n0 = max(len(open_days), 1), max(len(pre_open), 1)
        avg, pre_avg = qty / n1, pre_qty / n0
        return {"product": label, "qty": qty, "amount": amt,
                "qty_per_day": round(avg, 1),
                "pre_qty_per_day": round(pre_avg, 1),
                "qty_pct": (avg / pre_avg - 1) if pre_avg else None}

    targets = camp.get("target_products") or []
    out["targets"] = [
        per_day_stats(lambda p, t=t: product_matches(t, p), t)
        for t in targets
    ] if targets else []

    # 타겟이 없어도 '이 기간 뭐가 팔렸나'는 보여준다 — 기간 판매 TOP (금액순)
    out["top_products"] = []
    if not targets and product_rows and open_days:
        by_amt = defaultdict(int)
        for r in product_rows:
            if str(r["sale_date"])[:10] in open_days:
                by_amt[r["product"]] += r.get("amount") or 0
        top5 = sorted(by_amt, key=lambda p: -by_amt[p])[:5]
        out["top_products"] = [
            per_day_stats(lambda p, name=name: p == name, name)
            for name in top5
        ]
    return out


# ---------------------------------------------------------------------------
# 상품명 매칭 — 사장님 언어("버터떡")와 포스 상품명("상하이 버터떡 1BOX")을 잇는다
# ---------------------------------------------------------------------------

# 포스 상품명에 붙는 포장/옵션 장식 — 매칭 전에 걷어낸다
_DECOR_RE = re.compile(
    r"\[[^\]]*\]"                 # [SET], [Original] ...
    r"|\([^)]*\)"                 # (1인), (기본), (500ml) ...
    r"|\b\d+(BOX|L|ml|g|개입|인분|인)\b"
    r"|^E\)"                      # E)아메리카노
    , re.IGNORECASE)


def _norm_product(name):
    s = _DECOR_RE.sub("", str(name or ""))
    return re.sub(r"[\s\-·+_/,.!?]", "", s).lower()


def product_matches(keyword, product_name):
    """타겟 키워드가 이 상품을 가리키나 — 정규화 후 양방향 부분일치.

    '버터떡' ↔ '상하이 버터떡 1BOX', '풀드포크 샌드위치' ↔
    '베이글-풀드포크 샌드위치' 처럼 사장님이 적는 말과 포스 표기가 달라도
    잡힌다. (기존 완전일치는 실상품명 115종 기준 거의 전부 미스였다.)
    """
    k, p = _norm_product(keyword), _norm_product(product_name)
    if len(k) < 2 or len(p) < 2:
        return False
    return k in p or p in k


def extract_targets(title, product_names, max_products_per_kw=20):
    """제목에서 타겟 키워드 자동 인식.

    제목의 연속 낱말 묶음(긴 것 우선)이 실제 상품명 어느 것에든 걸리면
    그 묶음을 키워드로 삼는다. 단, 걸리는 상품이 너무 많은 낱말('베이글'
    한 단어 등)은 타겟이라 보기 어려워 버린다.
    """
    words = [w for w in re.split(r"\s+", str(title or "").strip()) if w]
    if not words or not product_names:
        return []
    hits, used = [], set()
    # 3→2→1 어절 묶음, 긴 것 우선 — "베이글 산도"가 "베이글"보다 먼저 잡힌다
    for size in (3, 2, 1):
        for i in range(len(words) - size + 1):
            span = set(range(i, i + size))
            if span & used:
                continue
            kw = " ".join(words[i:i + size])
            nk = _norm_product(kw)
            if len(nk) < 2:
                continue
            # 여기서는 '키워드가 상품명 안에 등장' 방향만 본다 — 양방향으로
            # 하면 "버터떡 인스타 릴스" 전체가 (상품명 '버터떡'을 품는다는
            # 이유로) 키워드가 돼버린다. 집계 쪽(product_matches)은 양방향.
            n = sum(1 for p in product_names if nk in _norm_product(p))
            if 1 <= n <= max_products_per_kw:
                hits.append(kw)
                used |= span
                if len(hits) >= 3:
                    return hits
    return hits
