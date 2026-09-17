"""매출 데이터 계층 (schema_v7.sql · v11).

이름은 옛 마케팅 캘린더에서 왔다 — 캘린더(행사 기록·효과 계산·발행 자동 기록)는
2026-09-17 사장님 지시로 전부 걷어냈고, 여기엔 **매출 읽기·쓰기**만 남았다.
경영 대시보드·플레이스 화면·기획 프롬프트(first_party)·집 PC 일꾼(pos_import)이
같이 쓰므로 모듈 이름은 그대로 둔다. Supabase 의 mkt_campaigns 표는 지우지
않고 남겨 뒀다(아무도 읽지 않는다).

  · sales_daily         — 포스 장부에서 온 일별 채널별 매출
  · product_sales_daily — 일별 상품별 매출
  · pos_files           — 장부 파일 반영 로그 (같은 파일 재파싱 방지)
  · sales_hourly        — 시간대별 채널별 매출 (schema_v11)
  · menu_settings.sales_goals — 월 목표(매장/배달)
"""

import logging
import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from .supabase_client import get_client, get_setting, menu_set_setting

logger = logging.getLogger(__name__)

# 서버(PA)는 UTC — 날짜 판단은 전부 매장 시간(KST)으로 (2026-08-30 감사 #17)
KST = timezone(timedelta(hours=9))


def _today_kst():
    return datetime.now(KST).date()

SALES = "sales_daily"
PRODUCTS = "product_sales_daily"
POS_FILES = "pos_files"
HOURLY = "sales_hourly"            # schema_v11 — 시간대별 (매출 대시보드)
GOALS_KEY = "sales_goals"          # menu_settings 키 — 월 목표 {"YYYY-MM": {store, delivery}}


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


def hourly_between(d1, d2):
    """[d1,d2] sales_hourly 원본 행들 (매출 대시보드 요일×시간대 히트맵용)."""
    return _fetch_all(lambda: (
        get_client().table(HOURLY)
        .select("sale_date,hour,channel,amount,orders_count,source")
        .gte("sale_date", str(_d(d1))).lte("sale_date", str(_d(d2)))
        .order("sale_date")))


def sales_goals() -> dict:
    """월 목표 전부 — {"2026-09": {"store": 15000000, "delivery": 20000000}}."""
    return get_setting(GOALS_KEY, {}) or {}


def set_sales_goal(ym, store=None, delivery=None):
    """한 달 목표 저장(원 단위). None/0 은 '목표 없음'으로 지운다."""
    if not re.fullmatch(r"\d{4}-\d{2}", str(ym or "")):
        raise ValueError("ym 은 YYYY-MM")
    goals = sales_goals()
    cur = {}
    if store:
        cur["store"] = int(store)
    if delivery:
        cur["delivery"] = int(delivery)
    if cur:
        goals[ym] = cur
    else:
        goals.pop(ym, None)
    menu_set_setting(GOALS_KEY, goals)
    return goals


def last_pos_date():
    """장부(포스)가 반영된 마지막 날짜. 없으면 None."""
    res = (get_client().table(SALES).select("sale_date")
           .in_("source", ["tos", "imu"])
           .order("sale_date", desc=True).limit(1).execute())
    return _d(res.data[0]["sale_date"]) if res.data else None


# 포스에 '상품'으로 찍히지만 메뉴가 아닌 것 — 타겟 후보에서 뺀다.
# ("배달비 무료 이벤트" 같은 제목이 '배달비'를 타겟으로 잡는 오탐, 감사 #1-⑥)
_NON_MENU = ("배달비", "배달료", "포장비", "봉투", "일회용")



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


def upsert_sales_hourly(rows):
    """[{sale_date, hour, channel, amount, orders_count, source}] 일괄 반영."""
    if not rows:
        return 0
    for r in rows:
        r["imported_at"] = _now()
    for i in range(0, len(rows), 500):
        get_client().table(HOURLY).upsert(
            rows[i:i + 500], on_conflict="sale_date,hour,channel,source").execute()
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


def store_only_sum(daily_totals, d1, d2):
    """[d1,d2] 구간의 **매장(포스) 매출만** 합산한다. 순수 로직.

    네이버 플레이스는 **매장 방문**을 만드는 채널이다. 배달(배민·쿠팡)은
    플레이스와 무관하게 움직이므로 섞으면 신호가 희석된다 — 그래서 store 만
    본다(사장님 확정 2026-08-30).

    `partial`(배달만 잡히고 매장이 0인 날 = 장부 미반영)은 **빼고** 센다.
    그 날을 0원으로 세면 매출이 급락한 것처럼 보이기 때문이다. 대신 며칠을
    뺐는지 함께 돌려줘, 화면이 "장부 미반영 N일 제외"를 밝힐 수 있게 한다.
    """
    d1, d2 = str(_d(d1)), str(_d(d2))
    amount = days = missing = 0
    for d, row in (daily_totals or {}).items():
        if not (d1 <= d <= d2):
            continue
        if row.get("partial"):
            missing += 1
            continue
        amount += row.get("store", 0) or 0
        days += 1
    return {"amount": amount, "days": days, "missingDays": missing}


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
