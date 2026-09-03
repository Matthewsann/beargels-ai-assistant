"""매출 대시보드 화면 조립 (service/app.py 의 /sales 라우트가 쓴다).

사장님 인터뷰(2026-09-03)로 정한 다섯 질문에 답하는 화면이다:
  1. 이번 달 잘 가고 있나 — 매장/배달 누적 + 목표 달성률,
     지난달 같은 날짜까지 · 작년 같은 달 · 요일 평균(8주) 대비
  2. 일별 그래프 — 매장·배달 쌓기, 지난달 선 겹침
  3. 채널별 어디서 버나 — 매장/배민/쿠팡/요기요 비중과 지난달 대비
  4. 뭐가 잘 팔리나 — 상품 TOP 10, 지난달 순위 대비
  5. 요일×시간대 패턴 — sales_hourly(schema_v11) 8주 평균 히트맵

원칙:
  · **장부(포스)만 쓴다.** 크롤러 잠정치는 섞지 않는다(사장님 확정 — "빈
    채로 두기"). 장부가 아직 없는 날은 '장부 미반영'으로 비워 둔다.
  · 이번 달 장부가 한 줄도 없으면(월초) 장부가 있는 마지막 달로 대신 열고
    그 사실을 화면에 밝힌다 — 빈 화면을 열어 주는 건 도움이 안 된다.
  · 계산은 순수 함수로 두고(테스트 대상), DB 는 build_view 에서만 만진다.
"""
from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, timedelta

from database import mkt_store

CHANNEL_LABEL = {"store": "매장", "baemin": "배민", "coupang": "쿠팡이츠",
                 "yogiyo": "요기요", "ddangyo": "땡겨요", "etc": "기타"}
CHANNEL_ORDER = ("store", "baemin", "coupang", "yogiyo", "ddangyo", "etc")
DELIVERY = set(mkt_store._DELIVERY_CHANNELS) | {"etc"}
WEEKDAYS = "월화수목금토일"
BASELINE_WEEKS = 8          # '요일 평균' = 같은 요일 직전 8주 평균 (사장님 선택)
HEATMAP_DAYS = 56           # 히트맵 표본 8주
MIN_PACE_DAYS = 3           # 월말 예상은 3일치는 있어야


def _safe(fn, default):
    """schema 미적용 등으로 표가 없어도 페이지는 뜨게."""
    try:
        return fn(), True
    except Exception:  # noqa: BLE001
        return default, False


def month_range(y, m):
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def prev_month(y, m):
    return (y - 1, 12) if m == 1 else (y, m - 1)


def next_month(y, m):
    return (y + 1, 1) if m == 12 else (y, m + 1)


def won_short(n) -> str:
    """1,234,567 → '123만' / 123,456,789 → '1.2억'. 화면의 큰 숫자용."""
    try:
        n = int(round(n or 0))
    except (TypeError, ValueError):
        return "-"
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= 100_000_000:
        v = n / 100_000_000
        return f"{sign}{v:.1f}억" if v < 10 else f"{sign}{v:,.0f}억"
    if n >= 10_000:
        return f"{sign}{n / 10_000:,.0f}만"
    return f"{sign}{n:,}"


def pct(cur, base):
    """증감률. 기준이 없으면 None."""
    if not base:
        return None
    return cur / base - 1


# ---------------------------------------------------------------------------
# 순수 계산
# ---------------------------------------------------------------------------

def month_days(daily, y, m, today, last_pos):
    """달의 날짜 하나하나 — 그래프와 '어디까지 장부가 있나' 표시용.

    state: data(장부 있음) / closed(장부 기간인데 0원 = 휴무) /
           pending(장부 아직 없음) / future(아직 안 온 날)
    """
    first, last = month_range(y, m)
    out = []
    d = first
    while d <= last:
        row = daily.get(str(d)) or {}
        total = row.get("total", 0) or 0
        if d > today:
            state = "future"
        elif total > 0:
            state = "data"
        elif last_pos and d <= last_pos:
            state = "closed"
        else:
            state = "pending"
        out.append({
            "date": str(d), "day": d.day, "dow": d.weekday(),
            "store": row.get("store", 0) or 0,
            "delivery": row.get("delivery", 0) or 0,
            "total": total,
            "partial": bool(row.get("partial")),
            "state": state,
        })
        d += timedelta(days=1)
    return out


def month_to_date(daily, y, m, upto_day=None):
    """1일~upto_day 누적. upto_day 가 None 이면 그 달 전체.

    partial(배달만 잡히고 매장 장부가 없는 날)은 매장 합계·표본일에서 뺀다 —
    0원으로 세면 매장 매출이 급락한 것처럼 보이기 때문(store_only_sum 과 동일).
    """
    first, last = month_range(y, m)
    if upto_day:
        last = min(last, date(y, m, min(upto_day, last.day)))
    store = delivery = total = 0
    days = partial_days = 0
    last_data_day = None
    d = first
    while d <= last:
        row = daily.get(str(d))
        if row and row.get("total", 0) > 0:
            if row.get("partial"):
                partial_days += 1
                delivery += row.get("delivery", 0)
                total += row.get("delivery", 0)
            else:
                days += 1
                store += row.get("store", 0)
                delivery += row.get("delivery", 0)
                total += row.get("total", 0)
            last_data_day = d.day
        d += timedelta(days=1)
    return {"store": store, "delivery": delivery, "total": total,
            "days": days, "partial_days": partial_days,
            "upto": last_data_day}


def weekday_pace(daily, y, m, upto_day=None, weeks=BASELINE_WEEKS):
    """이 달의 장부 있는 날들을 '같은 요일 직전 N주 평균'과 견준다.

    비교 가능한 날(요일 기준선이 있고 partial 아님)만 실제·기대 양쪽에
    넣는다 — mkt_store.campaign_effect 와 같은 정직한 비율.
    """
    first, last = month_range(y, m)
    if upto_day:
        last = min(last, date(y, m, min(upto_day, last.day)))
    out = {}
    for key in ("total", "store", "delivery"):
        actual = expected = 0.0
        covered = 0
        d = first
        while d <= last:
            row = daily.get(str(d))
            if row and row.get("total", 0) > 0 and not row.get("partial"):
                base = mkt_store.weekday_baseline(daily, d, weeks=weeks, key=key)
                if base:
                    actual += row.get(key, 0)
                    expected += base
                    covered += 1
            d += timedelta(days=1)
        out[key] = {"actual": round(actual), "expected": round(expected),
                    "pct": pct(actual, expected), "days": covered}
    return out


def channel_mix(daily, y, m, upto_day=None, prev=None):
    """채널별 금액·비중. prev 는 (py, pm) — 지난달 같은 날짜까지와 비교."""
    first, last = month_range(y, m)
    if upto_day:
        last = min(last, date(y, m, min(upto_day, last.day)))
    cur = defaultdict(int)
    d = first
    while d <= last:
        row = daily.get(str(d)) or {}
        for ch, v in row.items():
            if ch in ("total", "delivery", "partial") or not isinstance(v, int):
                continue
            if ch == "store" and row.get("partial"):
                continue
            cur[ch] += v
        d += timedelta(days=1)
    prev_amt = {}
    if prev:
        prev_amt = {c["channel"]: c["amount"]
                    for c in channel_mix(daily, prev[0], prev[1], upto_day)}
    total = sum(v for v in cur.values() if v > 0)
    out = []
    for ch in sorted(cur, key=lambda c: (CHANNEL_ORDER.index(c)
                                         if c in CHANNEL_ORDER else 99)):
        amt = cur[ch]
        if amt <= 0:
            continue
        out.append({"channel": ch, "label": CHANNEL_LABEL.get(ch, ch),
                    "amount": amt, "share": (amt / total) if total else 0,
                    "prev": prev_amt.get(ch), "pct": pct(amt, prev_amt.get(ch)),
                    "delivery": ch in DELIVERY})
    return out


def product_rank(prows, prev_prows=None, limit=10):
    """상품 TOP — 금액순. 지난달 순위와 견줘 오르내림을 붙인다."""
    def agg(rows):
        by = {}
        for r in rows or []:
            name = r.get("product") or ""
            if not name or any(w in name for w in mkt_store._NON_MENU):
                continue
            cur = by.setdefault(name, {"qty": 0, "amount": 0})
            cur["qty"] += r.get("qty") or 0
            cur["amount"] += r.get("amount") or 0
        return by

    cur, prev = agg(prows), agg(prev_prows)
    prev_rank = {p: i + 1 for i, p in enumerate(
        sorted(prev, key=lambda p: -prev[p]["amount"]))}
    total = sum(v["amount"] for v in cur.values()) or 0
    out = []
    for i, name in enumerate(sorted(cur, key=lambda p: -cur[p]["amount"])[:limit]):
        pr = prev_rank.get(name)
        out.append({"rank": i + 1, "product": name,
                    "qty": cur[name]["qty"], "amount": cur[name]["amount"],
                    "share": (cur[name]["amount"] / total) if total else 0,
                    "prev_rank": pr,
                    "delta": (pr - (i + 1)) if pr else None,   # +면 올라옴
                    "new": pr is None and bool(prev)})
    return out


def heatmap(hourly_rows):
    """sales_hourly 행들 → 요일×시간대 평균(그 요일이 장부에 있는 날수로 나눔).

    · 매장은 출처 합산(IMU+TOS), 배달은 출처 우선순위(TOS 만) — 일매출 규칙과 같다.
    · 시간 범위는 매출이 실제로 있는 시간대만(첫 시~끝 시, 연속).
    """
    rank = mkt_store._SOURCE_RANK
    # (date, hour, channel) -> {source: (amount, count)}
    per = defaultdict(dict)
    for r in hourly_rows or []:
        key = (str(r["sale_date"])[:10], int(r.get("hour") or 0),
               r.get("channel") or "etc")
        src = r.get("source") or "?"
        a, c = per[key].get(src, (0, 0))
        per[key][src] = (a + (r.get("amount") or 0),
                         c + (r.get("orders_count") or 0))
    cells = defaultdict(lambda: [0, 0])   # (mode, dow, hour) -> [amount, count]
    dates_by_dow = defaultdict(set)
    hours_seen = set()
    for (d, h, ch), by_src in per.items():
        if ch == "store":
            amt = sum(v[0] for v in by_src.values())
            cnt = sum(v[1] for v in by_src.values())
        else:
            best = max(rank.get(s, 0) for s in by_src)
            amt = sum(v[0] for s, v in by_src.items() if rank.get(s, 0) == best)
            cnt = sum(v[1] for s, v in by_src.items() if rank.get(s, 0) == best)
        if amt <= 0:
            continue
        dow = date.fromisoformat(d).weekday()
        dates_by_dow[dow].add(d)
        hours_seen.add(h)
        mode = "store" if ch == "store" else "delivery"
        for mkey in (mode, "total"):
            cells[(mkey, dow, h)][0] += amt
            cells[(mkey, dow, h)][1] += cnt
    if not hours_seen:
        return {"hours": [], "modes": {}, "max": {}, "days": 0}
    hours = list(range(min(hours_seen), max(hours_seen) + 1))
    modes, mx = {}, {}
    for mkey in ("total", "store", "delivery"):
        grid = []
        top = 0
        for dow in range(7):
            n = len(dates_by_dow.get(dow) or ())
            row = []
            for h in hours:
                a, c = cells.get((mkey, dow, h), (0, 0))
                avg = round(a / n) if n else 0
                row.append({"a": avg, "c": round(c / n, 1) if n else 0})
                top = max(top, avg)
            grid.append(row)
        modes[mkey] = grid
        mx[mkey] = top
    # 요일별 하루 평균(합계) — 히트맵 오른쪽 열
    dow_total = {}
    for mkey in ("total", "store", "delivery"):
        dow_total[mkey] = [sum(c["a"] for c in modes[mkey][dow]) for dow in range(7)]
    return {"hours": hours, "modes": modes, "max": mx, "dow_total": dow_total,
            "days": sum(len(v) for v in dates_by_dow.values())}


def goal_view(goals, ym, mtd, days_in_month):
    """월 목표 대비 — 매장/배달 따로(사장님 선택). 월말 예상은 '지금 속도면'."""
    g = (goals or {}).get(ym) or {}
    upto = mtd.get("upto") or 0
    out = {}
    for key in ("store", "delivery"):
        goal = g.get(key)
        done = mtd.get(key, 0)
        # 매장은 partial 날을 빼고 셌으니 속도도 실제 장부 일수로 나눈다
        n_days = mtd.get("days", 0) if key == "store" else upto
        pace = (round(done / n_days * days_in_month)
                if n_days >= MIN_PACE_DAYS else None)
        out[key] = {"goal": goal, "done": done,
                    "pct": (done / goal) if goal else None,
                    "pace": pace,
                    "pace_pct": (pace / goal) if (goal and pace) else None}
    return out


# ---------------------------------------------------------------------------
# 화면 한 장
# ---------------------------------------------------------------------------

def build_view(y: int, m: int, today: date | None = None,
               explicit: bool = False) -> dict:
    today = today or mkt_store._today_kst()
    last_pos, db_ready = _safe(mkt_store.last_pos_date, None)

    first, last = month_range(y, m)
    fetch_from = first - timedelta(days=7 * BASELINE_WEEKS + 7)   # 지난달 + 요일 기준선
    sales, ok = _safe(lambda: mkt_store.sales_between(fetch_from, last), [])
    db_ready = db_ready and ok
    daily = mkt_store.totals_by_date(sales)

    mtd = month_to_date(daily, y, m)
    fallback = None
    # 월초라 이번 달 장부가 한 줄도 없으면 장부가 있는 마지막 달로
    if (not explicit and mtd["days"] == 0 and mtd["partial_days"] == 0
            and last_pos and (last_pos.year, last_pos.month) != (y, m)):
        fallback = {"asked": f"{y}-{m:02d}", "asked_label": f"{m}월"}
        y, m = last_pos.year, last_pos.month
        first, last = month_range(y, m)
        fetch_from = first - timedelta(days=7 * BASELINE_WEEKS + 7)
        sales, _ = _safe(lambda: mkt_store.sales_between(fetch_from, last), [])
        daily = mkt_store.totals_by_date(sales)
        mtd = month_to_date(daily, y, m)

    ym = f"{y}-{m:02d}"
    py, pm = prev_month(y, m)
    upto = mtd["upto"]
    days_in_month = last.day
    is_current = (y, m) == (today.year, today.month)

    # 비교 3종
    prev_mtd = month_to_date(daily, py, pm, upto) if upto else None
    ly_rows, _ = _safe(
        lambda: mkt_store.sales_between(*month_range(y - 1, m)), [])
    ly_daily = mkt_store.totals_by_date(ly_rows)
    ly_mtd = month_to_date(ly_daily, y - 1, m, upto) if upto else None
    if ly_mtd and ly_mtd["days"] == 0 and ly_mtd["partial_days"] == 0:
        ly_mtd = None
    pace = weekday_pace(daily, y, m, upto) if upto else None

    goals, _ = _safe(mkt_store.sales_goals, {})
    goal = goal_view(goals, ym, mtd, days_in_month)
    if not is_current:                     # 지난 달에 '이 속도면 월말'은 무의미
        for g in goal.values():
            g["pace"] = g["pace_pct"] = None

    # 채널·상품
    channels = channel_mix(daily, y, m, upto, prev=(py, pm)) if upto else []
    prows, _ = _safe(lambda: mkt_store.product_sales_between(first, last), [])
    prev_first, prev_last = month_range(py, pm)
    if upto:
        prev_last = min(prev_last, date(py, pm, min(upto, prev_last.day)))
    prev_prows, _ = _safe(
        lambda: mkt_store.product_sales_between(prev_first, prev_last), [])
    products = product_rank(prows, prev_prows)

    # 요일×시간대 — 장부가 있는 마지막 날부터 거꾸로 8주(보는 달 기준)
    heat, heat_range = {"hours": [], "modes": {}, "max": {}, "days": 0}, None
    if last_pos:
        h_end = min(last_pos, last)
        h_start = h_end - timedelta(days=HEATMAP_DAYS - 1)
        hrows, _ = _safe(lambda: mkt_store.hourly_between(h_start, h_end), [])
        heat = heatmap(hrows)
        heat_range = (str(h_start), str(h_end))

    days = month_days(daily, y, m, today, last_pos)
    prev_days = month_days(daily, py, pm, today, last_pos)
    pending_days = sum(1 for d in days if d["state"] == "pending")

    def _cmp(cur_v, base):
        return {"base": base, "pct": pct(cur_v, base)} if base else None

    summary = {}
    for key in ("store", "delivery", "total"):
        summary[key] = {
            "amount": mtd[key], "short": won_short(mtd[key]),
            "prev": _cmp(mtd[key], (prev_mtd or {}).get(key)),
            "last_year": _cmp(mtd[key], (ly_mtd or {}).get(key)),
            "weekday": (pace or {}).get(key),
        }

    return {
        "y": y, "m": m, "ym": ym, "label": f"{y}년 {m}월",
        "today": str(today), "is_current": is_current,
        "db_ready": db_ready,
        "last_pos": str(last_pos) if last_pos else None,
        "last_pos_label": (f"{last_pos.month}/{last_pos.day}" if last_pos else None),
        "fallback": fallback,
        "upto": upto, "days_in_month": days_in_month,
        "data_days": mtd["days"], "partial_days": mtd["partial_days"],
        "pending_days": pending_days,
        "summary": summary,
        "goal": goal,
        "goal_raw": (goals or {}).get(ym) or {},
        "channels": channels,
        "products": products,
        "heat": heat, "heat_range": heat_range,
        "days": days, "prev_days": prev_days,
        "prev": {"y": py, "m": pm, "label": f"{pm}월"},
        "next": dict(zip(("y", "m"), next_month(y, m))),
        "has_next": (y, m) < (today.year, today.month),
        "last_year_label": f"{y - 1}년 {m}월",
    }
