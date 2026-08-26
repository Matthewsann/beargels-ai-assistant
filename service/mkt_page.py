"""마케팅 캘린더 화면 조립 (service/app.py 의 /mkt 라우트가 쓴다).

DB 접근은 database/mkt_store.py, 여기는 '달력 한 달치 화면에 필요한 것'을
한 덩어리로 계산한다:
  · 주(week) 단위 격자 + 캠페인 기간 막대(lane 배치) + ◆ 변수 마커
  · 날짜별 신호점(같은 요일 4주 평균 ±10%)
  · 장부 미반영 구간은 크롤러(orders) 잠정치로 보완
  · 리마인드 3종(장부 업로드 / 진행중 2주 / 기록 없는 급등락)
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

from database import mkt_store

# 카테고리 표기 (키는 DB 값)
CATEGORIES = [
    ("delivery", "배달앱", "c1"),
    ("sns", "SNS·콘텐츠", "c2"),
    ("place", "플레이스", "c3"),
    ("store", "매장 이벤트", "c4"),
]
CAT_LABEL = {k: v for k, v, _ in CATEGORIES}
CAT_CLASS = {k: c for k, _, c in CATEGORIES}
CAT_LABEL["var"] = "변수"


def _safe(fn, default):
    """schema_v7 미적용 등으로 테이블이 없어도 페이지는 뜨게."""
    try:
        return fn(), True
    except Exception:  # noqa: BLE001
        return default, False


def month_range(y, m):
    first = date(y, m, 1)
    last = date(y, m, calendar.monthrange(y, m)[1])
    return first, last


def _sales_with_provisional(d1, d2, today=None):
    """장부 매출 + (장부 미반영 구간은) 크롤러 잠정치."""
    today = today or date.today()
    sales = mkt_store.sales_between(d1, d2)
    last_pos, _ = _safe(mkt_store.last_pos_date, None)
    start_prov = (last_pos + timedelta(days=1)) if last_pos else d1
    if start_prov <= min(d2, today):
        crawler, _ = _safe(
            lambda: mkt_store.crawler_daily_sales(start_prov, min(d2, today)), [])
        sales = sales + (crawler or [])
    return sales


def build_month_view(y: int, m: int, today: date | None = None) -> dict:
    today = today or date.today()
    first, last = month_range(y, m)
    # 일요일 시작 격자
    grid_start = first - timedelta(days=(first.weekday() + 1) % 7)
    grid_end = last + timedelta(days=(5 - last.weekday()) % 7)

    fetch_from = grid_start - timedelta(days=56)   # 요일 베이스라인용 여유
    sales, db_ready = _safe(
        lambda: mkt_store.sales_between(fetch_from, grid_end), [])
    last_pos, _ = _safe(mkt_store.last_pos_date, None)

    # 장부 미반영 구간은 크롤러 잠정치(배달만)로 보완
    prov_from = None
    if db_ready:
        start_prov = (last_pos + timedelta(days=1)) if last_pos else fetch_from
        if start_prov <= min(grid_end, today):
            crawler, _ = _safe(
                lambda: mkt_store.crawler_daily_sales(
                    start_prov, min(grid_end, today)), [])
            if crawler:
                sales = sales + crawler
                prov_from = start_prov

    daily = mkt_store.totals_by_date(sales)

    camps, _ = _safe(
        lambda: mkt_store.campaigns_overlapping(grid_start, grid_end), [])
    bars = [c for c in camps if c["category"] != "var"]
    markers = [c for c in camps if c["category"] == "var"]

    def covered(d):
        s = str(d)
        for c in camps:
            if c["start_date"] <= s and (not c.get("end_date") or s <= c["end_date"]):
                return True
        return False

    # 주 격자
    weeks = []
    d = grid_start
    while d <= grid_end:
        week_days = []
        for i in range(7):
            day = d + timedelta(days=i)
            row = daily.get(str(day))
            total = (row or {}).get("total", 0)
            has_data = row is not None and total > 0
            closed = (row is None or total == 0) and day <= today \
                and (last_pos and day <= last_pos)
            sig = mkt_store.day_signal(daily, day) if has_data else 0
            week_days.append({
                "date": str(day), "num": day.day,
                "in_month": day.month == m,
                "today": day == today,
                "closed": bool(closed and day.month == m),
                "sig": sig,
            })
        # 캠페인 막대 lane 배치 (시작일 순 → 겹치면 다음 줄)
        week_end = d + timedelta(days=6)
        week_bars, lanes = [], []
        for c in sorted(bars, key=lambda c: (c["start_date"], c["id"])):
            cs = date.fromisoformat(c["start_date"])
            ce = date.fromisoformat(c["end_date"]) if c.get("end_date") \
                else min(today, grid_end)
            if ce < d or cs > week_end:
                continue
            col_a = max((cs - d).days, 0)
            col_b = min((ce - d).days, 6)
            lane = next((i for i, busy in enumerate(lanes)
                         if all(not (a <= col_b and col_a <= b) for a, b in busy)),
                        None)
            if lane is None:
                lanes.append([])
                lane = len(lanes) - 1
            lanes[lane].append((col_a, col_b))
            week_bars.append({
                "id": c["id"], "title": c["title"],
                "cls": CAT_CLASS.get(c["category"], "c4"),
                "row": lane + 2,
                "col_a": col_a + 1, "col_b": col_b + 2,
                "cont_l": cs < d, "cont_r": ce > week_end,
                "live": c.get("status") == "live",
            })
        week_markers = []
        mk_lane = len(lanes) + 2
        for c in markers:
            cs = date.fromisoformat(c["start_date"])
            if d <= cs <= week_end:
                week_markers.append({
                    "id": c["id"], "title": c["title"], "row": mk_lane,
                    "col": (cs - d).days + 1,
                })
        weeks.append({"days": week_days, "bars": week_bars,
                      "markers": week_markers,
                      "lanes": max(len(lanes), 1 if week_markers else 0)})
        d += timedelta(days=7)

    # 이달 목록 (카테고리별, 빈 카테고리 숨김) — 이 달과 겹치는 것만
    month_camps = [c for c in camps
                   if c["start_date"] <= str(last)
                   and (not c.get("end_date") or c["end_date"] >= str(first))]
    by_cat = []
    for key, label, cls in CATEGORIES:
        items = [c for c in month_camps if c["category"] == key]
        if items:
            by_cat.append({"key": key, "label": label, "cls": cls,
                           "items": items})
    var_items = [c for c in month_camps if c["category"] == "var"]

    # ---------------- 리마인드 3종 ----------------
    reminders = []
    if db_ready:
        # ① 지난달 장부(TOS) 미반영
        prev_last = date(today.year, today.month, 1) - timedelta(days=1)
        if not last_pos or last_pos < prev_last:
            need = f"{prev_last.month}월" if last_pos else "장부"
            reminders.append({
                "kind": "tos",
                "text": (f"📂 {need} 포스 장부가 아직 반영 안 됐어요 — 토스 매출리포트"
                         f" 엑셀을 드라이브 장부관리 폴더에 올려주세요."
                         f" (반영: ~{last_pos} )" if last_pos else
                         "📂 장부가 아직 없어요 — schema_v7 적용 후 [지금 반영]을 눌러주세요."),
            })
        # ② 진행중 2주 넘은 캠페인
        for c in camps:
            if c.get("status") == "live" and c["category"] != "var":
                started = date.fromisoformat(c["start_date"])
                if (today - started).days >= 14:
                    reminders.append({
                        "kind": "stale", "id": c["id"],
                        "text": (f"⏳ '{c['title']}' 이(가) {(today - started).days}일째 "
                                 f"진행중이에요 — 끝났으면 종료 처리해주세요."),
                    })
        # ③ 최근 급등락인데 기록 없는 날 (최근 21일)
        check_from = max(today - timedelta(days=21), first - timedelta(days=7))
        d = check_from
        flagged = 0
        while d <= min(today, grid_end) and flagged < 2:
            row = daily.get(str(d))
            if row and row.get("total", 0) > 0 and not covered(d):
                sig = mkt_store.day_signal(daily, d, threshold=0.18)
                if sig:
                    updown = "크게 올랐어요" if sig > 0 else "많이 내렸어요"
                    reminders.append({
                        "kind": "unexplained", "date": str(d),
                        "text": (f"❓ {d.month}/{d.day} 매출이 평소보다 {updown} — "
                                 f"그날 한 일이 있으면 기록해두면 나중에 분석에 남아요."),
                    })
                    flagged += 1
            d += timedelta(days=1)

    products, _ = _safe(lambda: mkt_store.distinct_products(days=120), [])

    return {
        "y": y, "m": m, "weeks": weeks,
        "by_cat": by_cat, "var_items": var_items,
        "categories": CATEGORIES,
        "cat_label": CAT_LABEL,
        "db_ready": db_ready,
        "last_pos": str(last_pos) if last_pos else None,
        "prov_from": str(prov_from) if prov_from else None,
        "reminders": reminders,
        "products": products[:120],
        "prev": ((y - 1, 12) if m == 1 else (y, m - 1)),
        "next": ((y + 1, 1) if m == 12 else (y, m + 1)),
        "today": str(today),
    }


def day_detail(day: str) -> dict:
    """날짜 클릭 → 채널별 매출 + 상품 TOP."""
    d = date.fromisoformat(day)
    sales = _sales_with_provisional(d - timedelta(days=56), d)
    daily = mkt_store.totals_by_date(sales)
    row = daily.get(day) or {}
    base = mkt_store.weekday_baseline(daily, d)
    total = row.get("total", 0)
    pct = (total / base - 1) if base else None
    channels = [{"channel": ch, "amount": v}
                for ch, v in sorted(row.items(), key=lambda x: -x[1])
                if ch not in ("total", "store", "delivery") or ch == "store"]
    # store 는 row 에 채널로도 있으니 중복 제거
    seen, chan_out = set(), []
    for c in channels:
        if c["channel"] in seen or c["channel"] in ("total", "delivery"):
            continue
        seen.add(c["channel"])
        chan_out.append(c)
    prows = mkt_store.product_sales_between(d, d)
    agg = {}
    for r in prows:
        cur = agg.setdefault(r["product"], {"qty": 0, "amount": 0})
        cur["qty"] += r.get("qty") or 0
        cur["amount"] += r.get("amount") or 0
    top = sorted(({"product": p, **v} for p, v in agg.items()),
                 key=lambda x: -x["amount"])[:10]
    return {"date": day, "total": total, "pct": pct,
            "channels": chan_out, "top": top}


def campaign_effect(cid: int) -> dict:
    camp = mkt_store.get_campaign(cid)
    if not camp:
        return {"error": "not_found"}
    start = date.fromisoformat(camp["start_date"])
    end = date.fromisoformat(camp["end_date"]) if camp.get("end_date") \
        else date.today()
    fetch_from = start - timedelta(days=56)
    sales = _sales_with_provisional(fetch_from, end)
    targets = camp.get("target_products") or []
    prows = mkt_store.product_sales_between(fetch_from, end, targets) \
        if targets else []
    eff = mkt_store.campaign_effect(camp, sales, prows)
    # 겹침 경고
    others = mkt_store.campaigns_overlapping(camp["start_date"],
                                             camp.get("end_date") or str(end))
    overlap = [{"id": c["id"], "title": c["title"]}
               for c in others if c["id"] != cid and c["category"] != "var"]
    eff["overlap"] = overlap
    eff["cost"] = camp.get("cost")
    eff["category"] = camp.get("category")
    eff["memo"] = camp.get("memo")
    return eff
