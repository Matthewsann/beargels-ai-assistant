"""재료원가 일괄 갱신 — data/menu_cost_update_v2.json → menu_items.ingredient_cost.

2026-08 자재리스트(사장님 제공) 단가로 재계산한 메뉴별 재료원가를 정본에 넣는다.
집 PC에서:

    python scripts/apply_cost_update.py           # 미리보기(변경 없음)
    python scripts/apply_cost_update.py --apply   # 실제 적용

안전장치:
- 웹에서 직접 입력한 원가(cost_source가 '웹에서 직접 입력')는 덮어쓰지 않는다
  (사장님 수기 값 우선). 강제로 덮으려면 --force.
- 여러 번 실행해도 같은 결과(idempotent).
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.supabase_client import get_client  # noqa: E402

SPEC = ROOT / "data" / "menu_cost_update_v2.json"


def main() -> int:
    apply = "--apply" in sys.argv
    force = "--force" in sys.argv
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    src = spec["cost_source"]
    sb = get_client()
    rows = sb.table("menu_items").select(
        "sku,name,ingredient_cost,cost_source").execute().data
    by_sku = {r["sku"]: r for r in rows}
    print(f"=== 재료원가 갱신 v2 — {'적용' if apply else '미리보기(--apply 로 실행)'} ===\n")

    n = skip = same = 0
    for sku, cost in spec["costs"].items():
        cur = by_sku.get(sku)
        if not cur:
            print(f"  [없음] {sku}")
            continue
        if (cur.get("cost_source") or "").startswith("웹에서 직접 입력") and not force:
            print(f"  [보존] {sku} {cur['name']} — 수기 입력값 유지({cur['ingredient_cost']})")
            skip += 1
            continue
        old = cur.get("ingredient_cost")
        if old is not None and abs(float(old) - cost) < 0.5:
            same += 1
            continue
        diff = "" if old is None else f" ({'+' if cost >= float(old) else ''}{cost - float(old):,.0f})"
        print(f"  {sku} {cur['name'][:24]:26} {old or '없음':>9} → {cost:>9,.1f}{diff}")
        if apply:
            sb.table("menu_items").update(
                {"ingredient_cost": cost, "cost_source": src}
            ).eq("sku", sku).execute()
        n += 1
    print(f"\n갱신 {n}건 · 수기값 보존 {skip}건 · 변동없음 {same}건")
    if not apply:
        print("실제 반영: python scripts/apply_cost_update.py --apply")
    else:
        print("완료. 웹 /menu 에서 원가율·기여마진이 새로 계산됩니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
