"""메뉴별 추천 판매가 — 매장가는 원가율 × 시장가, 배달가는 매장가 × 1.13.

추천가는 두 기준을 함께 본다:

1) 원가 기준   재료원가 ÷ 카테고리 목표원가율(/menu ⚙ 설정값)
2) 시장 기준   베이커리·디저트 카페 시장조사 밴드(2026-08, 아래 MARKET)
              — 원가가 허락해도 시장 밴드 위로는 올리지 않는다(손님이 비교하는
              건 옆집 가격이지 우리 원가가 아니다)

정리하면: 추천가 = clamp(원가기준가, 현재가, 시장 상단), 100원 단위 반올림.
현재가가 이미 원가·시장 기준을 다 만족하면 '유지'다. 인하는 권하지 않는다
(가격을 내려서 얻는 것보다 잃는 것이 큰 업태라, 내릴 이유는 따로 판단할 일).

시장 밴드 출처(2026-08 조사):
- 아메리카노: 스타벅스/투썸 4,700 · 송도 개인카페 ~4,200 · 저가 프랜차이즈 1,000~2,000
- 플레인 베이글: 런던베이글뮤지엄 ~3,800 · 코끼리베이글 2,800~2,900
- 베이글 샌드위치: 코끼리 8,600~9,200 · 성수권 8,000~11,300 · 런던 최고 14,800
- 탄단지 샐러드: 프랜차이즈 8,600~11,400
## 추천 배달가 (2026-08-24, 사장님 확정)

배달가는 원가에서 따로 뽑지 않는다. **매장가 × 1.13**, 100원 반올림이다.
근거는 수수료 차이뿐이다 — 중개+결제(부가세 포함)가 매장 1.43% · 배민 10.78% ·
쿠팡 14.08% 라, 매장과 같은 마진을 남기려면 배민 +10.5% / 쿠팡 +14.7% 가 필요하다.
배달가는 정본에 하나뿐이므로 그 사이인 13% 로 잡고 전 채널에 같은 값을 쓴다.

배달비(주문당 정액 3,500원)는 여기 얹지 않는다. 메뉴마다 나누면 저가 음료가 전부
적자로 보이는데, 실측 객단가(배민 17,519 · 쿠팡 17,110)가 손익분기 객단가
6,500원의 두 배가 넘어 주문 단위에서 이미 회수된다. 메뉴-원가-관리.md 참고.

인상률 상한은 15% 로 본다 — 손님은 매장 가격을 알고 오므로 그 위는 리뷰에서
지적거리가 된다. 이미 그보다 높게 받고 있는 메뉴(세트·샐러드)는 '유지'로 둔다.

쓰는 법: python scripts/price_recommend.py [--csv]
"""
from __future__ import annotations

import argparse
import csv
import io
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
from database import supabase_client as db  # noqa: E402

OUT = ROOT / "data" / "price_recommendations.csv"
DOUT = ROOT / "data" / "delivery_price_recommendations.csv"

# 배달가 = 매장가 × (1 + 이 값). 근거는 위 설명 참고.
DELIVERY_MARKUP = 0.13
# 이미 이 배율 위로 받고 있으면 더 올리라고 하지 않는다(손님 체감 상한).
DELIVERY_MAX = 1.15

# 카테고리별 시장 밴드 (매장가 기준, 원) — (하단, 중심, 상단)
# 하단: 저가 경쟁권 / 중심: 동급 베이커리·디저트 카페 평균 / 상단: 프리미엄권
MARKET = {
    "커피":        (3800, 4300, 4700),    # 스벅·투썸 4,700 / 송도 개인카페 4,200
    "티":          (4000, 4500, 5500),
    "논커피":      (4300, 5000, 6100),
    "에이드&스무디": (4500, 5500, 6500),
    "시그니처&스페셜": (4800, 5800, 6800),
    "베이커리":    (2900, 3800, 4700),    # 코끼리 2.9천 ~ 런던 3.8천+
    "크림치즈":    (2000, 2800, 3500),
    "샌드위치":    (8000, 9200, 11300),   # 코끼리 8.6~9.2천, 성수권 ~11.3천
    "샐러드":      (8600, 9900, 11400),   # 탄단지 샐러드 프랜차이즈 밴드
    "디저트":      (3000, 4500, 6500),
    "세트":        (8500, 10500, 15000),
    "보틀":        (7800, 9900, 11000),
}


def r100(x):
    return int(round(x / 100.0)) * 100


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", action="store_true")
    args = ap.parse_args()

    menus = db.menu_all()
    targets = db.menu_settings_all().get("target_cost_rates") or {}

    # 배달가는 원가가 없어도 매장가만 있으면 뽑힌다 — 원가 미입력 메뉴까지 다 본다.
    drows = []
    for m in menus:
        price = m.get("store_price")
        if not price or not m.get("delivery_active"):
            continue
        cur = m.get("delivery_price")
        rec = r100(price * (1 + DELIVERY_MARKUP))
        if cur and cur >= price * DELIVERY_MAX:
            rec = cur                    # 이미 상한 위 — 인하는 권하지 않는다
        drows.append({
            "sku": m["sku"], "cat": m.get("category") or "기타", "name": m["name"],
            "store": int(price), "cur": int(cur) if cur else None, "rec": rec,
            "delta": (rec - int(cur)) if cur else None,
        })

    rows = []
    for m in menus:
        cost, price = m.get("ingredient_cost"), m.get("store_price")
        if not cost or not price:
            continue
        cat = m.get("category") or "기타"
        rate = (targets.get(cat) or targets.get("기타") or 35) / 100.0
        by_cost = cost / rate
        lo, mid, hi = MARKET.get(cat, (None, None, None))
        # 원가가 요구하는 가격과 현재가 중 큰 쪽에서 출발해 시장 상단으로 자른다
        rec = max(by_cost, price)
        if hi:
            rec = min(rec, hi)
        rec = r100(max(rec, price))          # 인하는 권하지 않는다
        cur_rate = cost / price * 100
        new_rate = cost / rec * 100
        rows.append({
            "sku": m["sku"], "cat": cat, "name": m["name"],
            "cost": round(cost), "price": int(price), "rec": rec,
            "cur_rate": cur_rate, "new_rate": new_rate,
            "target": rate * 100, "mid": mid, "hi": hi,
            "delta": rec - int(price),
        })

    # 원가율이 판매가를 넘으면 레시피가 1인분이 아니라 배치로 들어간 것이다.
    # 그런 줄에 가격을 제안하면 엉뚱한 숫자가 나오므로 따로 뺀다.
    bad = [r for r in rows if r["cur_rate"] > 100]
    rows = [r for r in rows if r["cur_rate"] <= 100]
    ups = [r for r in rows if r["delta"] > 0]
    keeps = [r for r in rows if r["delta"] == 0]

    print(f"원가 계산된 메뉴 {len(rows) + len(bad)}개 — 인상 제안 {len(ups)} · "
          f"유지 {len(keeps)} · 원가 데이터 의심 {len(bad)}\n")
    if bad:
        print("⚠ 원가율이 100%를 넘습니다 — 레시피가 1인분이 아니라 한 배치로 들어간 것 같습니다.")
        for r in bad:
            print(f"   {r['name'][:28]:<30} 원가 {r['cost']:,} / 판매 {r['price']:,} "
                  f"= {r['cur_rate']:.0f}%  → 1인분 환산 필요(가격 제안에서 제외)")
        print()
    print("=" * 100)
    print("인상 제안 (원가율이 목표를 넘는데 시장 여유가 있는 것)")
    print("=" * 100)
    print(f"{'메뉴':<30} {'현재':>7} {'추천':>7} {'인상':>6}  "
          f"{'원가율':>6}→{'추천후':>5}  {'목표':>4}  시장중심")
    for r in sorted(ups, key=lambda x: -x["delta"]):
        print(f"{r['name'][:28]:<30} {r['price']:>7,} {r['rec']:>7,} "
              f"{r['delta']:>+6,}  {r['cur_rate']:>5.0f}%→{r['new_rate']:>4.0f}%  "
              f"{r['target']:>3.0f}%  {r['mid'] or '-'}")

    over = [r for r in keeps if r["cur_rate"] > r["target"] + 1]
    if over:
        print(f"\n{'=' * 100}")
        print("시장 상단이라 더 못 올리는 것 — 가격 대신 레시피·매입가를 손봐야 함")
        print("=" * 100)
        for r in sorted(over, key=lambda x: -(x["cur_rate"] - x["target"])):
            print(f"{r['name'][:28]:<30} {r['price']:>7,} (상단 {r['hi']:,})  "
                  f"원가율 {r['cur_rate']:.0f}% (목표 {r['target']:.0f}%)")

    # ── 추천 배달가 ────────────────────────────────────────────────
    d_up = [d for d in drows if d["cur"] is not None and d["rec"] > d["cur"]]
    d_new = [d for d in drows if d["cur"] is None]
    d_ok = [d for d in drows if d["cur"] is not None and d["rec"] <= d["cur"]]
    print(f"\n{'=' * 100}")
    print(f"추천 배달가 (매장가 × {1 + DELIVERY_MARKUP:.2f}, 100원 반올림) — "
          f"올릴 것 {len(d_up)} · 배달가 미입력 {len(d_new)} · 현행 유지 {len(d_ok)}")
    print("=" * 100)
    print(f"{'메뉴':<30} {'매장가':>7} {'현재 배달':>9} {'추천':>7} {'차이':>7}")
    for d in sorted(d_up + d_new, key=lambda x: (x["cat"], -x["store"])):
        cur = f"{d['cur']:,}" if d["cur"] is not None else "미입력"
        delta = f"{d['delta']:+,}" if d["delta"] else ""
        print(f"{d['name'][:28]:<30} {d['store']:>7,} {cur:>9} {d['rec']:>7,} {delta:>7}")

    if args.csv:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        with OUT.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["SKU", "카테고리", "메뉴", "재료원가", "현재 매장가",
                        "추천 매장가", "인상폭", "현재 원가율%", "추천후 원가율%",
                        "목표 원가율%", "시장 중심가", "시장 상단가", "판단"])
            for r in sorted(rows + bad, key=lambda x: (-x["delta"], x["cat"])):
                verdict = ("원가 데이터 의심(1인분 환산 필요)" if r["cur_rate"] > 100 else
                           "인상 제안" if r["delta"] > 0 else
                           "유지(원가율 초과·시장 상단)" if r["cur_rate"] > r["target"] + 1
                           else "유지")
                w.writerow([r["sku"], r["cat"], r["name"], r["cost"], r["price"],
                            r["rec"], r["delta"], f"{r['cur_rate']:.1f}",
                            f"{r['new_rate']:.1f}", f"{r['target']:.0f}",
                            r["mid"] or "", r["hi"] or "", verdict])
        print(f"\n→ {OUT}")

        with DOUT.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["SKU", "카테고리", "메뉴", "매장가", "현재 배달가",
                        "추천 배달가", "차이", "현재 인상률%", "판단"])
            for d in sorted(drows, key=lambda x: (x["cat"], -x["store"])):
                if d["cur"] is None:
                    verdict, rate = "배달가 미입력", ""
                else:
                    rate = f"{(d['cur'] / d['store'] - 1) * 100:.1f}"
                    verdict = "인상 제안" if d["rec"] > d["cur"] else "유지"
                w.writerow([d["sku"], d["cat"], d["name"], d["store"],
                            d["cur"] if d["cur"] is not None else "",
                            d["rec"], d["delta"] if d["delta"] else "",
                            rate, verdict])
        print(f"→ {DOUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
