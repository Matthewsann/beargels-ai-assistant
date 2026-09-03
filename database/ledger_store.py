"""월별 장부 요약 데이터 계층 (schema_v12.sql · ledger_monthly).

사장님이 매달 정리하는 구글 시트 '베어글스_장부'의 **요약** 시트가 원천이다.
집 PC 일꾼(worker/ledger_sheet.py)이 그 시트를 CSV 로 내려받아 여기의
parse_summary_csv 로 달마다 한 행을 만들고 upsert 한다. 웹(service/sales_page.py)
은 ledger_months 로 읽어 진단·비용·시뮬 탭을 그린다.

⚠️ 영업이익 = 정산총액 − 매입원가 − 고정비 (사양서 §2).
   정산총액은 카드·배달 수수료가 **이미 빠진** 실입금액이다. 매출총액에서
   수수료를 또 빼면 이중 차감이다. 검산(2026-07, 시트 값):
   25,659,250 − 14,174,530 − 11,903,489 = −418,769 ✅

파싱·계산은 순수 함수(테스트 대상), DB 는 upsert_/ledger_ 함수만 만진다.
"""

from __future__ import annotations

import calendar
import csv
import io
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone

from .supabase_client import get_client, get_setting, menu_set_setting

logger = logging.getLogger(__name__)

TABLE = "ledger_monthly"
TARGETS_KEY = "ledger_targets"      # menu_settings — 시트 '목표' 열

# 시트의 월 열은 '10월, 11월, 12월, 1월, …' 처럼 연도 없이 이어진다.
# 첫 열이 어느 해인지는 시트가 말해주지 않으므로 여기서 고정한다.
START_YM = os.getenv("LEDGER_START_YM", "2025-10")

# 요약시트 행 이름 → 컬럼. (앞부분 일치 — 시트 라벨이 조금 바뀌어도 잡히게)
_ROW_MAP = (
    ("매출총액", "sales_total"),
    ("총주문건수", "orders_total"),
    ("매장매출", "store_sales"),
    ("매장건수", "store_orders"),
    ("배달매출", "delivery_sales"),
    ("배달건수", "delivery_orders"),
    ("정산총액", "settlement"),
    ("매입원가", "cogs"),
    ("배달 수수료(+광고)", "delivery_fees"),     # '_%' 행은 뒤에서 걸러진다
    ("고정비_총액", "fixed_cost"),
    ("임대료율", "rent_rate"),
    ("인건비율", "labor_rate"),
    ("인건비", "labor_cost"),                    # 금액/비율이 섞여 있는 행
    ("영업이익률", "_skip"),                     # 계산으로 대체
    ("영업이익", "op_profit"),
    ("영업외비용", "non_op_cost"),
    ("순이익률", "_skip"),
    ("순이익", "net_profit"),
)
_RATE_COLS = {"rent_rate", "labor_rate"}
_INT_COLS = {"sales_total", "orders_total", "store_sales", "store_orders",
             "delivery_sales", "delivery_orders", "settlement", "cogs",
             "delivery_fees", "fixed_cost", "labor_cost", "op_profit",
             "non_op_cost", "net_profit"}
COLUMNS = sorted(_INT_COLS | _RATE_COLS)

KST = timezone(timedelta(hours=9))

# 매장 카드수수료율 — 매출총액−정산총액 중 배달 몫과 카드 몫을 가르는 데 쓴다
# (사양서 §2 검증: 7월 매장매출 13,895,026 × 2.1% = 291,796). 시트에 없어 고정.
CARD_FEE_RATE = float(os.getenv("LEDGER_CARD_FEE_RATE", "0.021"))


# ---------------------------------------------------------------------------
# 셀 파싱
# ---------------------------------------------------------------------------

def _cell(v):
    """'40,000,000' → 40000000 / '35%' → 0.35 / '#DIV/0!' · '' → None.

    반환 (값, 비율여부). 비율은 0~1 로 통일한다.
    """
    s = str(v if v is not None else "").strip()
    if not s or s.startswith("#"):
        return None, False
    pct = s.endswith("%")
    s = s.rstrip("%").replace(",", "").replace("+", "")
    try:
        x = float(s)
    except ValueError:
        return None, False
    return (x / 100.0, True) if pct else (x, False)


def _month_columns(header):
    """헤더 행에서 'N월' 이 연속으로 이어지는 구간 → [(col_index, ym)].

    START_YM 부터 달이 하나씩 늘어난다고 본다(월이 작아지면 해가 바뀐 것).
    """
    y, m = (int(x) for x in START_YM.split("-"))
    out, started, prev_m = [], False, None
    for j, h in enumerate(header):
        mm = re.fullmatch(r"\s*(\d{1,2})월\s*", str(h or ""))
        if not mm:
            if started:
                break                      # 월 열 구간이 끝났다
            continue
        mon = int(mm.group(1))
        if not started:
            if mon != m:
                # 시트 첫 달과 START_YM 이 다르면 START_YM 을 믿는다
                m = mon
            started = True
        elif prev_m is not None:
            if mon != (prev_m % 12) + 1:
                break                      # 연속이 깨지면 그 뒤는 다른 표
            if mon < prev_m:
                y += 1
        out.append((j, f"{y}-{mon:02d}"))
        prev_m = mon
    return out


def _month_end(ym):
    y, m = (int(x) for x in ym.split("-"))
    return date(y, m, calendar.monthrange(y, m)[1])


def parse_summary_csv(text, modified_at=None, today=None):
    """요약시트 CSV → (rows, targets).

    rows: [{ym, sales_total, …, status}] — 매출총액이 있는 달만.
    targets: 시트 '목표' 열 {sales_total, store_sales, …, cogs_rate, labor_rate}
    status: 월이 끝난 뒤에 시트가 수정됐으면 confirmed, 아니면 estimate
            (사장님이 달 중에 적어 둔 예상치).
    """
    today = today or datetime.now(KST).date()
    reader = list(csv.reader(io.StringIO(text)))
    header_i, months = None, []
    for i, row in enumerate(reader[:10]):
        cols = _month_columns(row)
        if len(cols) >= 3:
            header_i, months = i, cols
            break
    if header_i is None:
        raise ValueError("요약시트에서 'N월' 헤더 행을 찾지 못했어요")
    header = reader[header_i]
    target_col = next((j for j, h in enumerate(header) if str(h).strip() == "목표"), 1)

    data = {ym: {"ym": ym} for _, ym in months}
    targets = {}
    for row in reader[header_i + 1:]:
        if not row:
            continue
        label = str(row[0] or "").strip()
        if not label:
            continue
        col = next((c for k, c in _ROW_MAP if label.startswith(k)), None)
        if col is None or col == "_skip":
            continue
        if col == "delivery_fees" and label.endswith("%"):
            continue
        # 목표 열
        tv, tpct = _cell(row[target_col]) if target_col < len(row) else (None, False)
        if tv is not None:
            if col == "labor_cost" and tpct:
                targets["labor_rate"] = tv
            elif col in _RATE_COLS or tpct:
                targets[col] = tv
            else:
                targets[col] = int(round(tv))
        for j, ym in months:
            v, pct = _cell(row[j]) if j < len(row) else (None, False)
            if v is None:
                continue
            if col == "labor_cost":
                # 이 행은 달마다 %와 금액이 섞여 있다 — 비율이면 인건비율로
                if pct or v < 1000:
                    data[ym].setdefault("labor_rate", v if pct else v / 100)
                else:
                    data[ym]["labor_cost"] = int(round(v))
                continue
            if col in _RATE_COLS:
                data[ym][col] = v if pct else (v / 100 if v > 1 else v)
            else:
                data[ym][col] = int(round(v))
    # 원가율 목표 행(‘원가율 35%’)은 _ROW_MAP 에 없으니 따로
    for row in reader[header_i + 1:]:
        if row and str(row[0] or "").strip() == "원가율" and target_col < len(row):
            tv, _ = _cell(row[target_col])
            if tv is not None:
                targets["cogs_rate"] = tv
    rows = []
    for _, ym in months:
        r = data[ym]
        if not r.get("sales_total"):
            continue
        end = _month_end(ym)
        if modified_at is not None:
            mod = modified_at.astimezone(KST).date() if modified_at.tzinfo else modified_at.date()
            r["status"] = "confirmed" if mod > end else "estimate"
        else:
            r["status"] = "confirmed" if today > end else "estimate"
        r["source_modified_at"] = (modified_at.isoformat() if modified_at else None)
        rows.append(r)
    return rows, targets


# ---------------------------------------------------------------------------
# 파생 지표 (순수)
# ---------------------------------------------------------------------------

def _rate(a, b):
    return (a / b) if (a is not None and b) else None


def derive(row: dict) -> dict:
    """한 달 행 → 화면이 쓰는 파생 지표를 덧붙인 dict (원본은 안 건드린다).

    · fees_total   = 매출총액 − 정산총액  (배달수수료 + 카드수수료) — 이것이 진실
    · card_fees    = 매장매출 × 2.1% (fees_total 한도)
    · delivery_fees(실질) = fees_total − card_fees
      ⚠️ 시트의 '배달 수수료(+광고)' 행은 정산총액과 안 맞는다(2026-07:
         12,056,020 vs 역산 7,448,667). 정산총액이 실입금이므로 역산을 쓰고
         시트 값은 delivery_fees_sheet 로만 남긴다.
    · variable     = 원가 + fees_total          → variable_rate
    · contribution = 1 − variable_rate          → breakeven = 고정비 ÷ contribution
    · op_profit    = 시트 값, 없으면 정산총액 − 원가 − 고정비
    · rent = 임대료율 × 매출, other_fixed = 고정비 − 인건비 − 임대료
    """
    r = dict(row)
    sales = r.get("sales_total") or 0
    settlement = r.get("settlement")
    cogs = r.get("cogs")
    fixed = r.get("fixed_cost")
    fees_total = (sales - settlement) if (settlement is not None and sales) else None
    r["delivery_fees_sheet"] = r.get("delivery_fees")
    card = dfees = None
    if fees_total is not None:
        card = min(int(round((r.get("store_sales") or 0) * CARD_FEE_RATE)), max(fees_total, 0))
        dfees = max(fees_total - card, 0)
        r["delivery_fees"] = dfees
    variable = (cogs + fees_total) if (cogs is not None and fees_total is not None) else None
    var_rate = _rate(variable, sales)
    contrib = (1 - var_rate) if var_rate is not None else None
    if r.get("op_profit") is None and None not in (settlement, cogs, fixed):
        r["op_profit"] = settlement - cogs - fixed
    labor = r.get("labor_cost")
    if labor is None and r.get("labor_rate") is not None and sales:
        labor = int(round(r["labor_rate"] * sales))
    rent = int(round(r["rent_rate"] * sales)) if (r.get("rent_rate") is not None and sales) else None
    other = (fixed - (labor or 0) - (rent or 0)) if fixed is not None else None
    r.update({
        "fees_total": fees_total, "card_fees": card,
        "variable": variable, "variable_rate": var_rate,
        "contribution_rate": contrib,
        "breakeven": (int(round(fixed / contrib)) if (fixed is not None and contrib) else None),
        "fixed_rate": _rate(fixed, sales),
        "cogs_rate": _rate(cogs, sales),
        "delivery_fee_rate": _rate(dfees, r.get("delivery_sales")),
        "card_fee_rate": _rate(card, r.get("store_sales")),
        "op_rate": _rate(r.get("op_profit"), sales),
        "labor": labor, "labor_rate": r.get("labor_rate") if r.get("labor_rate") is not None else _rate(labor, sales),
        "rent": rent, "other_fixed": other,
        "prime_cost": ((_rate(cogs, sales) or 0) + (r.get("labor_rate") or _rate(labor, sales) or 0))
                      if cogs is not None else None,
        "store_ticket": _rate(r.get("store_sales"), r.get("store_orders")),
        "delivery_ticket": _rate(r.get("delivery_sales"), r.get("delivery_orders")),
        "delivery_share": _rate(r.get("delivery_sales"), sales),
    })
    return r


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def upsert_ledger(rows):
    if not rows:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    payload = []
    for r in rows:
        row = {k: r.get(k) for k in COLUMNS}
        row.update({"ym": r["ym"], "status": r.get("status") or "confirmed",
                    "source_modified_at": r.get("source_modified_at"),
                    "imported_at": now})
        payload.append(row)
    get_client().table(TABLE).upsert(payload, on_conflict="ym").execute()
    return len(payload)


def ledger_months(limit=14):
    """최근 N개월 (오래된 달부터)."""
    rows = (get_client().table(TABLE).select("*")
            .order("ym", desc=True).limit(limit).execute().data) or []
    return sorted(rows, key=lambda r: r["ym"])


def ledger_targets() -> dict:
    return get_setting(TARGETS_KEY, {}) or {}


def set_ledger_targets(t: dict):
    menu_set_setting(TARGETS_KEY, t or {})
