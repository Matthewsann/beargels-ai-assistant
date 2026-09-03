"""경영 대시보드 6탭 조립 — 진단·매출·비용·상품·운영·시뮬 (service/app.py /sales).

참고 사양(경영대시보드-사양서, 2026-08-22)을 실데이터로 옮긴 것이다. 원칙:
  · **결론 → 근거 → 상세** 순서. 첫 화면(진단)의 숫자는 12개 이하.
  · 영업이익 = 정산총액 − 원가 − 고정비 (ledger_store 참고). 시트 값 우선.
  · 판정 문장·'다음 달 할 일 3개'는 **규칙**으로 만든다(AI 비용 0).
  · 장부(구글 시트)는 월 단위·월말 이후 확정이라, 진단은 **마지막 확정 달**을
    기준으로 하고 예상치 달은 '예상'으로만 곁들인다. 포스 기준 '이번 달
    진행'은 sales_page(일별 장부)에서 온다.
계산은 순수 함수(테스트 대상). DB 는 build_dashboard 에서만.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from database import ledger_store, mkt_store
from service import sales_page as sp

BASE_FEE_RATE = 0.1188          # 중개 7.8% + 결제 3.0%, 부가세 포함 (2026 상생요금제 상한)
INDUSTRY_FEE = (0.25, 0.30)     # 실질 배달 수수료 업계 평균 구간
TARGET_OP_RATE = 0.10           # 영업이익률 목표
DEFAULT_WAGE = 10_320           # 시뮬 기본 시급 (화면에서 바꿀 수 있음)
MOVER_MONTHS = 4                # 상품 추이 개월수
_MAN = 10_000


def man(n):
    """원 → 만원(정수). None 은 None."""
    return None if n is None else int(round(n / _MAN))


def _pct(p, digits=1):
    return None if p is None else round(p * 100, digits)


def _chg(cur, prev):
    if cur is None or prev in (None, 0):
        return None
    return cur / prev - 1


def _label(ym):
    return f"{int(ym[5:7])}월"


def _full(ym):
    return f"{ym[:4]}년 {int(ym[5:7])}월"


# ---------------------------------------------------------------------------
# 진단 — 규칙
# ---------------------------------------------------------------------------

def verdict(L: dict, targets: dict) -> dict:
    """마지막 확정 달 한 줄 판정 (사양 §3-1 ①)."""
    sales, be = L.get("sales_total") or 0, L.get("breakeven")
    op = L.get("op_profit")
    lab = _label(L["ym"])
    if be is None or not sales:
        return {"tone": "warn", "title": f"{lab} 장부가 아직 덜 채워졌습니다.",
                "body": "정산총액·원가·고정비가 있어야 손익분기를 계산할 수 있어요."}
    var_p, con_p = _pct(L.get("variable_rate"), 1), _pct(L.get("contribution_rate"), 1)
    fixed = man(L.get("fixed_cost"))
    if op is not None and op >= 0:
        rate_ok = (L.get("op_rate") or 0) >= (targets.get("op_rate") or TARGET_OP_RATE)
        return {
            "tone": "ok" if rate_ok else "warn",
            "title": (f"{lab}은 <em>{man(op):,}만원</em> 남았습니다.<br>"
                      f"손익분기 {man(be):,}만원을 {man(sales - be):,}만원 넘겼습니다."),
            "body": (f"변동비 {var_p}%를 빼고 남는 공헌이익 {con_p}%로 고정비 "
                     f"{fixed:,}만원을 채우고도 남았습니다. 영업이익률은 "
                     f"<b>{_pct(L.get('op_rate'))}%</b>"
                     + ("" if rate_ok else f" — 목표 {_pct(targets.get('op_rate') or TARGET_OP_RATE, 0)}%에는 못 미칩니다.")),
        }
    short = be - sales
    return {
        "tone": "bad",
        "title": (f"손익분기는 <em>{man(be):,}만원</em>.<br>"
                  f"{lab} 매출은 {man(sales):,}만원이었습니다."),
        "body": (f"변동비 {var_p}%를 빼고 남는 공헌이익 {con_p}%로 고정비 {fixed:,}만원을 "
                 f"채워야 합니다. 그러려면 매출이 <b>{man(be):,}만원</b>은 나와야 하는데 "
                 f"<b>{man(short):,}만원이 모자랐습니다.</b>"),
    }


def waterfall(L: dict) -> dict:
    """매출 1만원이 어디로 갔나 (사양 §3-1 ②). 시트 값으로 비율을 만든다."""
    sales = L.get("sales_total") or 0
    if not sales:
        return {"rows": [], "groups": {}, "remain": None}

    def r(v):
        return (v or 0) / sales

    rows = [
        ("변동비", "식자재", r(L.get("cogs")), "c-cogs"),
        ("변동비", "배달 수수료", r(L.get("delivery_fees")), "c-dfee"),
        ("변동비", "카드 수수료", r(L.get("card_fees")), "c-card"),
        ("고정비", "인건비", r(L.get("labor")), "c-labor"),
        ("고정비", "임대료", r(L.get("rent")), "c-rent"),
        ("고정비", "기타 고정비", r(L.get("other_fixed")), "c-other"),
    ]
    out = [{"group": g, "name": n, "pct": round(p * 100, 1),
            "won": int(round(p * _MAN)), "cls": c} for g, n, p, c in rows]
    var = sum(x["pct"] for x in out if x["group"] == "변동비")
    fix = sum(x["pct"] for x in out if x["group"] == "고정비")
    remain = _MAN - sum(x["won"] for x in out)
    contrib = L.get("contribution_rate") or 0
    note = (f"<b>공헌이익률 {_pct(contrib)}%</b> — 1만원 팔면 {int(round(contrib * _MAN)):,}원이 "
            f"남아 고정비를 갚습니다. 고정비 {man(L.get('fixed_cost')):,}만원을 채우려면 월 매출 "
            f"<b>{man(L.get('breakeven')):,}만원</b>이 필요합니다."
            + (f" 변동비를 1%p 줄이면 손익분기가 <b>약 {man((L.get('fixed_cost') or 0) / max(contrib, 0.01) - (L.get('fixed_cost') or 0) / max(contrib + 0.01, 0.02)):,}만원</b> 낮아집니다."
               if contrib else ""))
    return {"rows": out, "groups": {"변동비": round(var, 1), "고정비": round(fix, 1)},
            "remain": remain, "note": note}


def gauges(L: dict, P: dict | None, targets: dict) -> list:
    """게이지 3개: 매출 목표 / 손익분기 / 영업이익."""
    sales = L.get("sales_total") or 0
    goal = targets.get("sales_total") or 0
    be = L.get("breakeven")
    op = L.get("op_profit")
    op_goal = targets.get("op_profit") or int(round(sales * TARGET_OP_RATE))
    out = []
    pct = (sales / goal * 100) if goal else 0
    out.append({"label": "매출 목표", "num": f"{man(sales):,}", "unit": "만원",
                "target": f"목표 {man(goal):,}만원" if goal else "목표 없음",
                "pct": pct, "cls": "up" if pct >= 100 else ("warn" if pct >= 85 else "dn"),
                "status": f"{pct:.0f}% 달성" if goal else "-",
                "prev": f"전월 {man(P.get('sales_total')):,}만" if P and P.get("sales_total") else ""})
    if be:
        pct = sales / be * 100
        gap = sales - be
        out.append({"label": "손익분기 매출", "num": f"{man(be):,}", "unit": "만원",
                    "target": f"고정비 ÷ 공헌이익률 {_pct(L.get('contribution_rate'))}%",
                    "pct": pct, "mark": 100,
                    "cls": "up" if gap >= 0 else "dn",
                    "status": (f"{man(gap):,}만원 초과" if gap >= 0 else f"{man(-gap):,}만원 부족"),
                    "prev": f"{_label(L['ym'])} 매출 {man(sales):,}만"})
    if op is not None:
        pct = (op / op_goal * 100) if op_goal > 0 else 0
        prev_op = P.get("op_profit") if P else None
        out.append({"label": "영업이익", "num": f"{'−' if op < 0 else ''}{abs(man(op)):,}", "unit": "만원",
                    "target": f"목표 {man(op_goal):,}만원 (이익률 {_pct(targets.get('op_rate') or TARGET_OP_RATE, 0)}%)",
                    "pct": max(pct, 0), "cls": "up" if pct >= 100 else ("warn" if op >= 0 else "dn"),
                    "status": ("목표 달성" if pct >= 100 else ("흑자" if op >= 0 else "적자")),
                    "prev": (f"전월 {'+' if prev_op >= 0 else '−'}{abs(man(prev_op)):,}만"
                             if prev_op is not None else "")})
    return out


def actions(L: dict, P: dict | None, targets: dict, limit=3) -> list:
    """다음 달 할 일 — 규칙으로 후보를 만들고 기대효과(월 원) 큰 순 3개."""
    P = P or {}
    sales = L.get("sales_total") or 0
    cands = []
    # 1) 원가율
    cr, ct = L.get("cogs_rate"), targets.get("cogs_rate") or 0.35
    if cr and cr > ct + 0.005 and sales:
        gain = (cr - ct) * sales
        why = (f"전월 {_pct(P.get('cogs_rate'))}%에서 {_pct(cr - (P.get('cogs_rate') or cr))}%p 올랐습니다. "
               if P.get("cogs_rate") else "") + "폐기가 늘었는지, 단가가 올랐는지 원인부터 확인해야 합니다."
        cands.append({"title": f"원가율 {_pct(cr)}% → {_pct(ct, 0)}%", "why": why,
                      "do": ["폐기 품목·수량 집계", "발주 주기를 짧게(소량 자주)", "세트 메뉴 원가 재계산 (/menu)"],
                      "gain": gain, "gain_text": f"{_pct(cr - ct)}%p 낮추면 월 +{man(gain):,}만원"})
    # 2) 매장 객단가
    st, pt = L.get("store_ticket"), P.get("store_ticket")
    if st and pt and st < pt * 0.97 and L.get("store_orders"):
        target = pt
        gain = (target - st) * L["store_orders"]
        cands.append({"title": f"매장 객단가 {int(st):,}원 → {int(target):,}원",
                      "why": (f"전월 {int(pt):,}원에서 {_pct(1 - st / pt)}% 떨어졌습니다. "
                              f"배달 객단가({int(L.get('delivery_ticket') or 0):,}원)와 격차가 큽니다."),
                      "do": ["키오스크 첫 화면을 세트로", "단품 선택 시 세트 전환 안내", "모닝세트·사이드 추가 권유"],
                      "gain": gain, "gain_text": f"{int(target - st):,}원 올리면 월 +{man(gain):,}만원"})
    # 3) 배달 수수료율
    dr = L.get("delivery_fee_rate")
    if dr and dr > INDUSTRY_FEE[1] and L.get("delivery_sales"):
        gain = (dr - INDUSTRY_FEE[1]) * L["delivery_sales"]
        cands.append({"title": f"배달 실질 수수료 {_pct(dr)}% → {_pct(INDUSTRY_FEE[1], 0)}%",
                      "why": (f"기본 수수료 {_pct(BASE_FEE_RATE)}%를 뺀 나머지 "
                              f"{_pct(dr - BASE_FEE_RATE)}%가 광고비·배달비 부담입니다. "
                              f"업계 실질 부담은 {_pct(INDUSTRY_FEE[0], 0)}~{_pct(INDUSTRY_FEE[1], 0)}%입니다."),
                      "do": ["배민·쿠팡 광고 예산 2~3%p씩 단계적 축소", "쿠폰·무료배달 조건 재검토", "시뮬 탭 계산기로 확인"],
                      "gain": gain, "gain_text": f"업계 수준까지 낮추면 월 +{man(gain):,}만원"})
    # 4) 인건비율
    lr, lt = L.get("labor_rate"), targets.get("labor_rate") or 0.20
    if lr and lr > lt + 0.005 and sales:
        gain = (lr - lt) * sales
        cands.append({"title": f"인건비율 {_pct(lr)}% → {_pct(lt, 0)}%",
                      "why": "매출 대비 인건비가 목표를 넘었습니다. 한산한 시간대(운영 탭 히트맵)의 근무를 줄일 여지를 봅니다.",
                      "do": ["근무표에서 한산 시간대 인원 조정", "마감 시간 시뮬레이션(시뮬 탭)", "피크 시간대에 집중 배치"],
                      "gain": gain, "gain_text": f"{_pct(lr - lt)}%p 낮추면 월 +{man(gain):,}만원"})
    # 5) 매장 매출 급감
    ss, ps = L.get("store_sales"), P.get("store_sales")
    if ss and ps and ss < ps * 0.9:
        gain = (ps - ss) * (L.get("contribution_rate") or 0.35)
        cands.append({"title": f"매장 매출 {man(ss):,}만 → {man(ps):,}만원 회복",
                      "why": f"전월보다 {_pct(1 - ss / ps)}% 줄었습니다. 매장은 수수료가 없어 같은 매출이라도 남는 돈이 큽니다.",
                      "do": ["스마트플레이스 소식·리뷰 답글 챙기기", "인스타 릴스로 신메뉴 알리기", "단골 대상 이벤트"],
                      "gain": gain, "gain_text": f"전월 수준 회복 시 공헌이익 월 +{man(gain):,}만원"})
    cands.sort(key=lambda c: -c["gain"])
    out = []
    for i, c in enumerate(cands[:limit]):
        c["rank"] = i + 1
        c["cls"] = ("p1", "p2", "p3")[i]
        c["gain"] = int(round(c["gain"]))
        out.append(c)
    return out


def details(L: dict, P: dict | None) -> list:
    """세부 지표 12개 — 전월 대비. inverse=True 는 낮을수록 좋은 지표."""
    P = P or {}

    def money(v):
        return f"{man(v):,}만" if v is not None else "-"

    def won(v):
        return f"{int(v):,}원" if v else "-"

    def pc(v):
        return f"{_pct(v)}%" if v is not None else "-"

    items = [
        ("매출 총액", money, "sales_total", False, "pct"),
        ("매장 매출", money, "store_sales", False, "pct"),
        ("배달 매출", money, "delivery_sales", False, "pct"),
        ("정산총액 (실입금)", money, "settlement", False, "pct"),
        ("원가율", pc, "cogs_rate", True, "pp"),
        ("인건비율", pc, "labor_rate", True, "pp"),
        ("Prime Cost", pc, "prime_cost", True, "pp"),
        ("배달 수수료율", pc, "delivery_fee_rate", True, "pp"),
        ("영업이익", money, "op_profit", False, "pct"),
        ("매장 객단가", won, "store_ticket", False, "pct"),
        ("배달 객단가", won, "delivery_ticket", False, "pct"),
        ("거래 건수", lambda v: f"{int(v):,}건" if v else "-", "orders_total", False, "pct"),
    ]
    out = []
    for name, fmt, key, inverse, kind in items:
        cur, prev = L.get(key), P.get(key)
        if kind == "pp":
            chg = ((cur - prev) * 100) if (cur is not None and prev is not None) else None
            unit = "%p"
        else:
            chg = (_chg(cur, prev) * 100) if _chg(cur, prev) is not None else None
            unit = "%"
        out.append({"name": name, "prev": fmt(prev) if prev is not None else "-",
                    "now": fmt(cur) if cur is not None else "-",
                    "chg": round(chg, 1) if chg is not None else None,
                    "unit": unit, "inverse": inverse})
    return out


def cost_verdict(L: dict) -> dict:
    var_p, fix_p = _pct(L.get("variable_rate"), 1), _pct(L.get("fixed_rate"), 1)
    if var_p is None or fix_p is None:
        return {"tone": "warn", "title": "비용 구조를 계산할 자료가 부족합니다.", "body": ""}
    total = var_p + fix_p
    dr = L.get("delivery_fee_rate")
    fee_txt = ""
    if dr is not None:
        hi = dr > INDUSTRY_FEE[1]
        fee_txt = (f" 변동비 중 <b>배달 수수료 {_pct((L.get('delivery_fees') or 0) / (L.get('sales_total') or 1))}%</b>가 "
                   f"실질 수수료율 <b>{_pct(dr)}%</b>로 " + ("업계 평균(25~30%)보다 높은 게 핵심 원인입니다." if hi
                                                          else "업계 평균(25~30%) 안에 있습니다."))
    if total > 100:
        body = f"둘을 합치면 {total:.1f}%로 매출을 넘어섭니다.{fee_txt}"
        tone = "bad"
    else:
        body = f"둘을 합치면 {total:.1f}%, 매출의 {100 - total:.1f}%가 남습니다.{fee_txt}"
        tone = "ok" if total <= 90 else "warn"
    return {"tone": tone, "title": f"변동비 {var_p}%,<br>고정비 {fix_p}%.", "body": body}


def fee_split(L: dict, platform_sales: dict, platform_orders: dict) -> list:
    """플랫폼별 수수료 분해 — 기본(11.88%)은 매출 비례, 광고·배달비는 나머지를 매출 비중으로."""
    dfees, dsales = L.get("delivery_fees"), L.get("delivery_sales") or 0
    if not dfees or not dsales:
        return []
    total_base = dsales * BASE_FEE_RATE
    ad_total = max(dfees - total_base, 0)
    out = []
    for key, name in (("baemin", "배달의민족"), ("coupang", "쿠팡이츠")):
        s = platform_sales.get(key) or 0
        if not s:
            continue
        share = s / dsales
        base = s * BASE_FEE_RATE
        ad = ad_total * share
        out.append({"key": key, "name": name, "sales": man(s), "cnt": platform_orders.get(key) or 0,
                    "base": man(base), "ad": man(ad), "total": man(base + ad),
                    "base_pct": round(BASE_FEE_RATE * 100, 1), "ad_pct": round(ad / s * 100, 1),
                    "total_pct": round((base + ad) / s * 100, 1),
                    "keep": man(s - base - ad), "keep_pct": round((1 - (base + ad) / s) * 100, 1)})
    return out


# ---------------------------------------------------------------------------
# 상품 — 카테고리·추이·분류
# ---------------------------------------------------------------------------

_CAT_RULES = (
    ("세트", ("[set]", "세트")),
    ("샌드위치&샐러드", ("샌드위치", "샐러드")),
    ("베이글&크림치즈", ("베이글", "크림치즈")),
    ("커피", ("아메리카노", "라떼", "커피", "에스프레소", "콜드브루")),
    ("케이크", ("케이크",)),
    ("스무디/에이드", ("스무디", "에이드")),
    ("티", ("티", "차")),
    ("베이커리/디저트", ("쿠키", "스콘", "떡", "러스크", "디저트", "크루아상", "빵")),
)


def category_of(name: str, known: dict | None = None) -> str:
    if known and name in known:
        return known[name]
    low = (name or "").lower()
    for cat, keys in _CAT_RULES:
        if any(k in low for k in keys):
            return cat
    return "기타"


def category_share(prows) -> list:
    """이달 상품 행 → 카테고리 비중 (시트 카테고리가 있으면 그것, 없으면 이름 규칙)."""
    known = {}
    for r in prows or []:
        if r.get("category"):
            known[r["product"]] = r["category"]
    agg = defaultdict(int)
    for r in prows or []:
        name = r.get("product") or ""
        if any(w in name for w in mkt_store._NON_MENU):
            continue
        agg[category_of(name, known)] += r.get("amount") or 0
    total = sum(agg.values()) or 1
    out = sorted(({"name": k, "amount": v, "share": round(v / total * 100, 1)}
                  for k, v in agg.items() if v > 0), key=lambda x: -x["amount"])
    if len(out) > 6:
        keep, rest_rows = out[:5], out[5:]
        rest = sum(x["amount"] for x in rest_rows)
        etc = next((x for x in keep if x["name"] == "기타"), None)
        if etc:                       # 시트 카테고리 '기타'와 합친다 — '기타'가 둘 뜨면 안 됨
            etc["amount"] += rest
            etc["share"] = round(etc["amount"] / total * 100, 1)
            out = keep
        else:
            out = keep + [{"name": "기타", "amount": rest, "share": round(rest / total * 100, 1)}]
    return out


def product_buckets(monthly: dict, months: list, limit=3) -> dict:
    """상품별 월 매출 {product: {ym: amount}} → 밀 것/지켜볼 것/정리할 것 + 추이 후보.

    · 밀 것: 이달 상위 10 중 지난달 대비 가장 많이 오른 것
    · 지켜볼 것: 이달 새로 상위권에 든 것(지난달 없음) 또는 수량 상위
    · 정리할 것: 최근 N개월 최고점 대비 60% 이상 빠진 것(최고점 50만 이상)
    """
    if not months:
        return {"push": [], "watch": [], "cut": [], "movers": []}
    cur, prev = months[-1], (months[-2] if len(months) > 1 else None)
    rows = []
    for p, by in monthly.items():
        c = by.get(cur, 0)
        pv = by.get(prev, 0) if prev else 0
        peak = max(by.values()) if by else 0
        rows.append({"product": p, "cur": c, "prev": pv, "peak": peak,
                     "chg": _chg(c, pv), "series": [by.get(m, 0) for m in months]})
    top = sorted((r for r in rows if r["cur"] > 0), key=lambda r: -r["cur"])[:12]
    push = sorted((r for r in top if r["chg"] is not None and r["chg"] > 0.15),
                  key=lambda r: -(r["cur"] - r["prev"]))[:limit]
    watch = [r for r in top if r["prev"] == 0][:limit]
    if len(watch) < limit:
        watch += [r for r in sorted(rows, key=lambda r: -r["cur"])
                  if r not in watch and r not in push][:limit - len(watch)]
    # 최근에도 팔렸던 것만(cur 또는 prev > 0) — 이름이 바뀌어 0이 된 옛 메뉴는 제외
    cut = sorted((r for r in rows if r["peak"] >= 500_000 and r["cur"] < r["peak"] * 0.4
                  and r["peak"] != r["cur"] and (r["cur"] > 0 or r["prev"] > 0)),
                 key=lambda r: -(r["peak"] - r["cur"]))[:limit]
    movers = (push[:2] + cut[:2] + watch[:1])[:5]
    return {
        "push": [{"product": r["product"], "cur": man(r["cur"]), "prev": man(r["prev"]),
                  "chg": _pct(r["chg"], 0)} for r in push],
        "watch": [{"product": r["product"], "cur": man(r["cur"]), "new": r["prev"] == 0} for r in watch],
        "cut": [{"product": r["product"], "cur": man(r["cur"]), "peak": man(r["peak"])} for r in cut],
        "movers": [{"product": r["product"], "data": [man(v) for v in r["series"]]} for r in movers],
        "labels": [_label(m) for m in months],
    }


# ---------------------------------------------------------------------------
# 운영·시뮬 — 시간대·요일
# ---------------------------------------------------------------------------

def hour_profile(hourly_rows) -> dict:
    """시간대별 하루 평균 (매장/배달/전체) — 장부 있는 날수로 나눔."""
    rank = mkt_store._SOURCE_RANK
    per = defaultdict(dict)
    for r in hourly_rows or []:
        key = (str(r["sale_date"])[:10], int(r.get("hour") or 0), r.get("channel") or "etc")
        src = r.get("source") or "?"
        per[key][src] = per[key].get(src, 0) + (r.get("amount") or 0)
    dates, store, deliv = set(), defaultdict(int), defaultdict(int)
    for (d, h, ch), by in per.items():
        if ch == "store":
            amt = sum(by.values())
        else:
            best = max(rank.get(s, 0) for s in by)
            amt = sum(v for s, v in by.items() if rank.get(s, 0) == best)
        if amt <= 0:
            continue
        dates.add(d)
        (store if ch == "store" else deliv)[h] += amt
    n = len(dates) or 1
    hours = sorted(set(store) | set(deliv))
    if not hours:
        return {"hours": [], "store": [], "delivery": [], "days": 0}
    hours = list(range(min(hours), max(hours) + 1))
    return {"hours": hours, "days": len(dates),
            "store": [round(store[h] / n) for h in hours],
            "delivery": [round(deliv[h] / n) for h in hours]}


def dow_profile(daily: dict, d1: date, d2: date) -> list:
    """요일별 하루 평균 총매출 (휴무·partial 제외)."""
    sums, cnts = [0] * 7, [0] * 7
    d = d1
    while d <= d2:
        row = daily.get(str(d))
        if row and row.get("total", 0) > 0 and not row.get("partial"):
            sums[d.weekday()] += row["total"]
            cnts[d.weekday()] += 1
        d += timedelta(days=1)
    return [round(sums[i] / cnts[i]) if cnts[i] else 0 for i in range(7)]


# ---------------------------------------------------------------------------
# 화면 한 장
# ---------------------------------------------------------------------------

def _platform_monthly(sales_rows):
    """sales_daily 행 → {ym: {channel: amount}} + {ym: {channel: orders}} (배달 출처 우선순위 적용)."""
    daily = mkt_store.totals_by_date(sales_rows)
    amt, cnt = defaultdict(lambda: defaultdict(int)), defaultdict(lambda: defaultdict(int))
    for d, row in daily.items():
        ym = d[:7]
        for ch, v in row.items():
            if ch in ("total", "delivery", "partial") or not isinstance(v, int):
                continue
            amt[ym][ch] += v
    # 건수는 원본 행에서 (출처 우선순위: tos > xls > crawler)
    best = {}
    for r in sales_rows:
        ym, ch = str(r["sale_date"])[:7], r.get("channel")
        rk = mkt_store._SOURCE_RANK.get(r.get("source"), 0)
        if rk >= best.get((ym, ch), 0):
            best[(ym, ch)] = rk
    for r in sales_rows:
        ym, ch = str(r["sale_date"])[:7], r.get("channel")
        if mkt_store._SOURCE_RANK.get(r.get("source"), 0) == best.get((ym, ch), 0):
            cnt[ym][ch] += r.get("orders_count") or 0
    return amt, cnt


def build_dashboard(y: int, m: int, today: date | None = None, explicit: bool = False) -> dict:
    today = today or mkt_store._today_kst()
    v = sp.build_view(y, m, today, explicit=explicit)      # 포스 기준(매출·상품·운영 탭)
    y, m = v["y"], v["m"]

    # ── 장부(구글 시트) 월별 ──────────────────────────────────────────
    ledger_rows, ledger_ok = sp._safe(lambda: ledger_store.ledger_months(limit=14), [])
    targets, _ = sp._safe(ledger_store.ledger_targets, {})
    targets = dict(targets or {})
    if targets.get("op_profit") and targets.get("sales_total"):
        targets["op_rate"] = targets["op_profit"] / targets["sales_total"]   # 시트 목표 기준 이익률
    months = [ledger_store.derive(r) for r in ledger_rows]
    for r in months:
        r["label"], r["full"] = _label(r["ym"]), _full(r["ym"])
    confirmed = [r for r in months if r.get("status") == "confirmed"]
    estimate = [r for r in months if r.get("status") != "confirmed"]
    L = confirmed[-1] if confirmed else (months[-1] if months else None)
    P = None
    if L:
        idx = months.index(L)
        P = months[idx - 1] if idx > 0 else None
    est = estimate[-1] if estimate and L and estimate[-1]["ym"] > L["ym"] else None

    # ── 포스 월별 채널 (10개월 추이·플랫폼 분해용) ──────────────────────
    first_ym = months[0]["ym"] if months else f"{y}-{m:02d}"
    fy, fm = (int(x) for x in first_ym.split("-"))
    span_rows, _ = sp._safe(lambda: mkt_store.sales_between(date(fy, fm, 1), sp.month_range(y, m)[1]), [])
    plat_amt, plat_cnt = _platform_monthly(span_rows)

    labels = [r["label"] for r in months]
    def series(key, scale=True):
        return [(man(r.get(key)) if scale else r.get(key)) if r.get(key) is not None else None for r in months]

    sales_series = {
        "labels": labels, "status": [r.get("status") for r in months],
        "total": series("sales_total"), "store": series("store_sales"), "delivery": series("delivery_sales"),
        "baemin": [man(plat_amt.get(r["ym"], {}).get("baemin", 0)) for r in months],
        "coupang": [man(plat_amt.get(r["ym"], {}).get("coupang", 0)) for r in months],
        "yogiyo": [man(plat_amt.get(r["ym"], {}).get("yogiyo", 0) + plat_amt.get(r["ym"], {}).get("ddangyo", 0)) for r in months],
        "growth": [None] + [(_pct(_chg(months[i]["sales_total"], months[i - 1]["sales_total"]))
                            if i else None) for i in range(1, len(months))],
        "share_store": [_pct(1 - (r.get("delivery_share") or 0), 0) if r.get("sales_total") else None for r in months],
        "share_delivery": [_pct(r.get("delivery_share"), 0) if r.get("sales_total") else None for r in months],
        "orders": [r.get("orders_total") for r in months],
    }
    cost_series = {
        "labels": labels,
        "variable_rate": [_pct(r.get("variable_rate")) for r in months],
        "fixed_rate": [_pct(r.get("fixed_rate")) for r in months],
        "contribution_rate": [_pct(r.get("contribution_rate")) for r in months],
        "op_rate": [_pct(r.get("op_rate")) for r in months],
        "op_profit": series("op_profit"),
        "targets": {"variable": _pct(1 - (targets.get("contribution_rate") or 0.35), 0) if targets.get("contribution_rate") else 65,
                    "fixed": _pct(targets.get("fixed_rate"), 0) if targets.get("fixed_rate") else 35,
                    "op": _pct(targets.get("op_rate") or TARGET_OP_RATE, 0)},
    }
    # 플랫폼별 매출·실수익 시계열 (수수료는 매출 비중으로 배분)
    plat_series = {"labels": labels, "baemin_sales": [], "baemin_net": [], "coupang_sales": [], "coupang_net": []}
    for r in months:
        ds, df = r.get("delivery_sales") or 0, r.get("delivery_fees")
        for key in ("baemin", "coupang"):
            s = plat_amt.get(r["ym"], {}).get(key, 0)
            plat_series[f"{key}_sales"].append(man(s))
            plat_series[f"{key}_net"].append(man(s - df * (s / ds)) if (df is not None and ds and s) else None)

    # ── 진단·비용 ────────────────────────────────────────────────────
    diag = cost = None
    if L:
        diag = {"month": L["label"], "full": L["full"], "status": L.get("status"),
                "verdict": verdict(L, targets), "flow": waterfall(L),
                "gauges": gauges(L, P, targets), "actions": actions(L, P, targets),
                "details": details(L, P),
                "sum_gain": sum(a["gain"] for a in actions(L, P, targets)),
                "short": (L["breakeven"] - (L.get("sales_total") or 0)) if L.get("breakeven") else None,
                "estimate": ({"month": est["label"], "sales": man(est.get("sales_total")),
                              "op": man(est.get("op_profit")) if est.get("op_profit") is not None else None}
                             if est else None)}
        cost = {"verdict": cost_verdict(L),
                "fee_split": fee_split(L, plat_amt.get(L["ym"], {}), plat_cnt.get(L["ym"], {})),
                "fee_rate": _pct(L.get("delivery_fee_rate")),
                "base_rate": round(BASE_FEE_RATE * 100, 1),
                "ad_rate": _pct(max((L.get("delivery_fee_rate") or 0) - BASE_FEE_RATE, 0)),
                "month": L["label"]}
    sales_gauges = []
    if L:
        chg = _chg(L.get("sales_total"), P.get("sales_total") if P else None)
        avg = sum(r["sales_total"] or 0 for r in confirmed) / max(len(confirmed), 1)
        sales_gauges = [
            {"label": f"{L['label']} 매출", "num": f"{man(L['sales_total']):,}", "unit": "만원",
             "target": f"전월 {man(P['sales_total']):,}만원" if P and P.get("sales_total") else "",
             "pct": (L["sales_total"] / P["sales_total"] * 100) if P and P.get("sales_total") else 0,
             "cls": "up" if (chg or 0) >= 0 else "dn",
             "status": (f"{'▲' if chg >= 0 else '▼'}{abs(_pct(chg))}%" if chg is not None else "-"),
             "prev": f"{len(confirmed)}개월 평균 {man(avg):,}만"},
            {"label": "배달 비중", "num": f"{_pct(L.get('delivery_share'))}", "unit": "%",
             "target": f"매장 {_pct(1 - (L.get('delivery_share') or 0))}%",
             "pct": (L.get("delivery_share") or 0) * 100, "cls": "warn",
             "status": "배달 우위" if (L.get("delivery_share") or 0) > 0.5 else "매장 우위",
             "prev": f"전월 {_pct(P.get('delivery_share'))}%" if P and P.get("delivery_share") else ""},
            {"label": "거래 건수", "num": f"{L.get('orders_total') or 0:,}", "unit": "건",
             "target": f"전월 {P.get('orders_total'):,}건" if P and P.get("orders_total") else "",
             "pct": (L["orders_total"] / P["orders_total"] * 100) if (L.get("orders_total") and P and P.get("orders_total")) else 0,
             "cls": "up" if (L.get("orders_total") or 0) >= ((P or {}).get("orders_total") or 0) else "dn",
             "status": (f"{'▲' if _chg(L.get('orders_total'), (P or {}).get('orders_total')) >= 0 else '▼'}"
                        f"{abs(_pct(_chg(L.get('orders_total'), (P or {}).get('orders_total'))))}%"
                        if _chg(L.get("orders_total"), (P or {}).get("orders_total")) is not None else "-"),
             "prev": f"객단가 {int((L.get('sales_total') or 0) / (L.get('orders_total') or 1)):,}원"},
        ]

    # ── 상품: 카테고리·추이·분류 (최근 N개월 상품 행) ──────────────────
    first, last = sp.month_range(y, m)
    py, pm = y, m
    for _ in range(MOVER_MONTHS - 1):
        py, pm = sp.prev_month(py, pm)
    prows_all, _ = sp._safe(lambda: mkt_store.product_sales_between(date(py, pm, 1), last), [])
    monthly = defaultdict(lambda: defaultdict(int))
    cur_rows = []
    for r in prows_all:
        ym = str(r["sale_date"])[:7]
        name = r.get("product") or ""
        if not name or any(w in name for w in mkt_store._NON_MENU):
            continue
        monthly[name][ym] += r.get("amount") or 0
        if ym == f"{y}-{m:02d}":
            cur_rows.append(r)
    mlist = []
    yy, mm = py, pm
    for _ in range(MOVER_MONTHS):
        mlist.append(f"{yy}-{mm:02d}")
        yy, mm = sp.next_month(yy, mm)
    products_extra = {"categories": category_share(cur_rows),
                      **product_buckets(monthly, mlist)}

    # ── 운영·시뮬 ────────────────────────────────────────────────────
    last_pos = date.fromisoformat(v["last_pos"]) if v.get("last_pos") else None
    hp = {"hours": [], "store": [], "delivery": [], "days": 0}
    dow = [0] * 7
    if last_pos:
        h_end = min(last_pos, last)
        h_start = h_end - timedelta(days=sp.HEATMAP_DAYS - 1)
        hrows, _ = sp._safe(lambda: mkt_store.hourly_between(h_start, h_end), [])
        hp = hour_profile(hrows)
        drows, _ = sp._safe(lambda: mkt_store.sales_between(h_start, h_end), [])
        dow = dow_profile(mkt_store.totals_by_date(drows), h_start, h_end)
    contrib = (L.get("contribution_rate") if L else None) or 0.35
    sim = {
        "contribution_rate": round(contrib, 4),
        "hours": hp["hours"], "hour_total": [s + d for s, d in zip(hp["store"], hp["delivery"])],
        "dow": dow, "days_in_month": 30, "wage": DEFAULT_WAGE, "staff": 2,
        "delivery_sales": man((L or {}).get("delivery_sales")) if L else None,
        "ad_rate": _pct(max(((L or {}).get("delivery_fee_rate") or 0) - BASE_FEE_RATE, 0)) if L else 20.0,
        "base_fee": round(BASE_FEE_RATE * 100, 2), "mid_rate": 7.8,
        "month": L["label"] if L else None,
    }

    # 목표 기본값: 시트 '목표' 열 (화면 입력이 없을 때)
    if not v.get("goal_raw") and targets:
        gr = {}
        if targets.get("store_sales"):
            gr["store"] = targets["store_sales"]
        if targets.get("delivery_sales"):
            gr["delivery"] = targets["delivery_sales"]
        if gr:
            mtd = {"store": v["summary"]["store"]["amount"], "delivery": v["summary"]["delivery"]["amount"],
                   "days": v["data_days"], "upto": v["upto"]}
            v["goal"] = sp.goal_view({v["ym"]: gr}, v["ym"], mtd, v["days_in_month"])
            if not v["is_current"]:
                for g in v["goal"].values():
                    g["pace"] = g["pace_pct"] = None
            v["goal_from_sheet"] = True

    v.update({
        "ledger_ok": ledger_ok, "has_ledger": bool(months),
        "ledger_latest": L["full"] if L else None, "ledger_status": L.get("status") if L else None,
        "targets": {k: (man(val) if isinstance(val, int) and val > 1000 else val) for k, val in (targets or {}).items()},
        "diag": diag, "cost": cost,
        "sales_series": sales_series, "cost_series": cost_series, "plat_series": plat_series,
        "sales_gauges": sales_gauges,
        "products_extra": products_extra,
        "ops": {"hour": hp, "dow": dow},
        "sim": sim,
    })
    return v
