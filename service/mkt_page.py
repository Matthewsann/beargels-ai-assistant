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
            # 신호점은 장부가 반영된 날까지만 — 잠정(배달만) 구간은 총매출이
            # 원래 작아서 전부 '▼'로 물들어 버린다 (사장님 지적 2026-08-27)
            sig = (mkt_store.day_signal(daily, day)
                   if has_data and last_pos and day <= last_pos else 0)
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

    # 이달 목록 (카테고리별, 빈 카테고리 숨김) — 이 달과 겹치는 것만.
    # 뱃지는 저장된 status 가 아니라 날짜로 판정한다 — 종료일을 미리 적으면
    # 시작도 전에 '종료'로 찍히던 버그(2026-08-30 감사).
    month_camps = [c for c in camps
                   if c["start_date"] <= str(last)
                   and (not c.get("end_date") or c["end_date"] >= str(first))]
    for c in month_camps:
        if c["start_date"] > str(today):
            c["st"] = "planned"
        elif c.get("end_date") and c["end_date"] < str(today):
            c["st"] = "done"
        else:
            c["st"] = "live"
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
        # ② 진행중 2주 넘은 캠페인 — 종료일을 미리 적어둔 것은 제외
        #    (종료일이 있으면 그 날짜에 알아서 끝난다. '종료 처리하라'고
        #     조르면 잘 쓴 사람에게 잔소리가 된다.)
        for c in camps:
            if (c.get("status") == "live" and c["category"] != "var"
                    and not c.get("end_date")):
                started = date.fromisoformat(c["start_date"])
                if (today - started).days >= 14:
                    reminders.append({
                        "kind": "stale", "id": c["id"],
                        "text": (f"⏳ '{c['title']}' 이(가) {(today - started).days}일째 "
                                 f"진행중이에요 — 끝났으면 종료 처리해주세요."),
                    })
        # ③ 매출이 크게 튀었는데 기록이 없는 날 — "그날 뭐 하셨어요?"
        #
        # 범위는 **지금 보고 있는 달**이다(예전엔 today-21일로 묶여 있어,
        # 사장님이 지난달을 펼쳐 봐도 아무것도 안 떴다 — 정작 그때가 빠진
        # 기록을 보충할 시점인데. 2026-08-30 테스트로 발견).
        # ⚠️ 장부가 반영된 날까지만 본다. 잠정(배달 크롤러만) 구간은 총매출이
        #    매장 몫만큼 작아 장부가 든 지난주와 비교하면 전부 '급락'으로 잡혀
        #    매일 허위 경보가 뜬다(캘린더 신호점은 이미 게이트돼 있었는데
        #    여기만 빠져 있었다).
        # 월초부터 앞의 2건이 아니라 **변동이 큰 순** 2건을 고른다 — 사장님이
        # 기억해낼 만한 날은 '가장 크게 움직인 날'이지 '달력에서 먼저 오는 날'이
        # 아니다.
        check_to = min(today, last, last_pos) if last_pos else None
        spikes = []
        d = first
        while check_to and d <= check_to:
            row = daily.get(str(d))
            if row and row.get("total", 0) > 0 and not covered(d):
                base = mkt_store.weekday_baseline(daily, d)
                if base:
                    diff = row["total"] / base - 1
                    if abs(diff) >= 0.18:
                        spikes.append((abs(diff), diff, d))
            d += timedelta(days=1)
        for _, diff, day in sorted(spikes, key=lambda x: -x[0])[:2]:
            updown = "크게 올랐어요" if diff > 0 else "많이 내렸어요"
            reminders.append({
                "kind": "unexplained", "date": str(day),
                "text": (f"❓ {day.month}/{day.day} 매출이 평소보다 "
                         f"{abs(diff) * 100:.0f}% {updown} — 그날 한 일이 있으면 "
                         f"기록해두면 나중에 분석에 남아요."),
            })

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
    """날짜 클릭 → 채널별 매출 + 상품 TOP.

    장부 미반영(잠정) 구간은 배달 크롤러치뿐이라 요일 평균과 비교하면
    무조건 '▼' — pct 를 아예 주지 않는다. 데이터가 전혀 없으면 no_data.
    """
    d = date.fromisoformat(day)
    sales = _sales_with_provisional(d - timedelta(days=56), d)
    daily = mkt_store.totals_by_date(sales)
    last_pos, _ = _safe(mkt_store.last_pos_date, None)
    provisional = (last_pos is None) or (d > last_pos)
    row = daily.get(day) or {}
    total = row.get("total", 0)
    if provisional:
        pct = None
    else:
        base = mkt_store.weekday_baseline(daily, d)
        pct = (total / base - 1) if base else None
    channels = [{"channel": ch, "amount": v}
                for ch, v in sorted(row.items(),
                                    key=lambda x: -(x[1] if isinstance(x[1], int) else 0))
                if ch not in ("total", "store", "delivery", "partial")
                or ch == "store"]
    # store 는 row 에 채널로도 있으니 중복 제거 (partial 은 플래그라 제외)
    seen, chan_out = set(), []
    for c in channels:
        if c["channel"] in seen or c["channel"] in ("total", "delivery", "partial"):
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
    # 이날 진행 중이던 마케팅 — 매출을 보다가 "왜 이랬지?"에 바로 답하려면
    # 기록(캠페인)과 이 화면이 연결돼 있어야 한다(기록→가시화 목표 그 자체).
    camps, _ = _safe(lambda: mkt_store.campaigns_overlapping(d, d), [])
    campaigns = [{"id": c["id"], "title": c["title"],
                  "category": c["category"],
                  "cls": CAT_CLASS.get(c["category"], c["category"]),
                  "label": CAT_LABEL.get(c["category"], c["category"])}
                 for c in camps]
    return {"date": day, "total": total, "pct": pct,
            "provisional": provisional,
            "no_data": total <= 0 and not chan_out,
            "channels": chan_out, "top": top, "campaigns": campaigns}


def campaign_effect(cid: int) -> dict:
    camp = mkt_store.get_campaign(cid)
    if not camp:
        return {"error": "not_found"}
    start = date.fromisoformat(camp["start_date"])
    end = date.fromisoformat(camp["end_date"]) if camp.get("end_date") \
        else date.today()
    fetch_from = start - timedelta(days=56)
    sales = _sales_with_provisional(fetch_from, end)
    # 타겟 유무와 무관하게 상품 매출을 통째로 가져온다 — 타겟이 있으면
    # 부분일치 매칭에 전체 상품명이 필요하고("버터떡" ↔ "상하이 버터떡 1BOX"),
    # 없으면 '이 기간 뭐가 팔렸나' TOP 을 보여줘야 하니까(사장님의 유일한
    # 첫 기록이 정확히 타겟 없는 경우였다, 2026-08-30).
    prows, _ = _safe(
        lambda: mkt_store.product_sales_between(fetch_from, end), [])
    last_pos, _ = _safe(mkt_store.last_pos_date, None)
    eff = mkt_store.campaign_effect(camp, sales, prows, last_pos=last_pos)
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
