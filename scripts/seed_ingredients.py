"""자재·레시피 초기 데이터 주입 — data/ingredients_seed.json → Supabase.

2026-08 자재리스트(사장님 제공) 188개 자재 + 기존 원가계산기 레시피 549줄.
집 PC에서:

    python scripts/seed_ingredients.py            # 미리보기
    python scripts/seed_ingredients.py --apply    # 주입 + 원가 재계산

여러 번 실행해도 안전(idempotent): 자재는 (이름,단위) upsert, 레시피는
(SKU,자재) upsert. 웹에서 수정한 값은 시드가 덮지 않도록 이미 있는 자재의
가격은 건드리지 않는다(--force-prices 로 강제 갱신 가능).
주입 후 레시피가 있는 메뉴의 재료원가를 재계산한다('웹에서 직접 입력' 보존).
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database import supabase_client as db  # noqa: E402

SPEC = ROOT / "data" / "ingredients_seed.json"


def main() -> int:
    apply = "--apply" in sys.argv
    force_prices = "--force-prices" in sys.argv
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    sb = db.get_client()
    print(f"=== 자재·레시피 시드 — {'적용' if apply else '미리보기(--apply)'} ===\n")

    existing = {(i["name"], i["unit"]): i for i in db.ingredients_all()}
    n_new = n_upd = 0
    for ing in spec["ingredients"]:
        key = (ing["name"], ing["unit"])
        cur = existing.get(key)
        if cur and not force_prices:
            continue
        if apply:
            row = db.ingredient_upsert(ing)
            existing[key] = row
        n_new += 0 if cur else 1
        n_upd += 1 if cur else 0
    print(f"자재: 신규 {n_new} · 갱신 {n_upd} · 유지 {len(spec['ingredients']) - n_new - n_upd}")

    if apply:
        existing = {(i["name"], i["unit"]): i for i in db.ingredients_all()}
    have = {(r["sku"], r["ingredient_id"])
            for r in (db.recipes_all() if apply else [])}
    valid_skus = {i["sku"] for i in sb.table("menu_items").select("sku").execute().data}
    n_line = n_skip = 0
    touched = set()
    for ln in spec["recipes"]:
        if ln["sku"] not in valid_skus:
            n_skip += 1
            continue
        ing = existing.get((ln["ingredient"], ln["unit"]))
        if not ing:
            n_skip += 1
            continue
        if (ln["sku"], ing["id"]) in have:
            continue
        if apply:
            db.recipe_upsert(ln["sku"], ing["id"], ln["qty"])
        touched.add(ln["sku"])
        n_line += 1
    print(f"레시피 라인: 추가 {n_line} · 건너뜀 {n_skip}")

    if apply and touched:
        updated = db.recompute_costs(sorted(touched))
        print(f"원가 재계산: {len(updated)}개 메뉴 (수기 입력 원가는 보존)")
        # 반제품 원가 → 짝인 자재 pack_cost → 그 자재를 쓰는 메뉴까지.
        # 이걸 빠뜨리면 반제품을 쓰는 메뉴가 원가 0원으로 남는다.
        synced, more = db.prep_sync()
        if synced:
            print(f"반제품 자재값 동기화: {synced}개 · 뒤이어 재계산 {len(more)}개 메뉴")
    if not apply:
        print("\n실제 주입: python scripts/seed_ingredients.py --apply")
    else:
        print("\n완료. 웹 /menu/ingredients 에서 확인하세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
