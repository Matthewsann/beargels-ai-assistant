"""정본 갱신안 v1 적용 — data/menu_master_update_v1.json → Supabase menu_items.

2026-08 사장님 인터뷰 결정(이름 통일·신메뉴 추가·판매중지·삭제)을 정본에 반영한다.
집 PC에서 1회 실행:

    python scripts/apply_menu_update.py           # 미리보기(변경 없음)
    python scripts/apply_menu_update.py --apply   # 실제 적용

여러 번 실행해도 안전하다(idempotent):
- rename 은 SKU 기준 update — 이미 바뀐 이름이면 그대로.
- 신메뉴는 이름이 이미 존재하면 건너뛴다.
- 삭제/판매중지는 없으면 건너뛴다.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.supabase_client import get_client, normalize_menu_name  # noqa: E402

SPEC = ROOT / "data" / "menu_master_update_v1.json"


def main() -> int:
    apply = "--apply" in sys.argv
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    sb = get_client()
    items = sb.table("menu_items").select("sku,name,sort_order").execute().data
    by_sku = {i["sku"]: i for i in items}
    existing_norm = {normalize_menu_name(i["name"]) for i in items}
    mode = "적용" if apply else "미리보기(--apply 로 실행)"
    print(f"=== 정본 갱신안 v1 — {mode} ===\n")

    # 1) 이름 변경(+가격/노출 동반 변경)
    n = 0
    for r in spec["renames"]:
        sku = r["sku"]
        cur = by_sku.get(sku)
        if not cur:
            print(f"  [건너뜀] {sku} 없음")
            continue
        fields = {k: v for k, v in r.items() if k not in ("sku",) and not k.startswith("_")}
        if cur["name"] == r["name"] and len(fields) == 1:
            continue  # 이미 반영됨
        print(f"  [이름] {sku}: {cur['name']} → {r['name']}"
              + (f"  (+{ {k:v for k,v in fields.items() if k!='name'} })" if len(fields) > 1 else ""))
        if apply:
            sb.table("menu_items").update(fields).eq("sku", sku).execute()
        n += 1
    print(f"이름/속성 변경: {n}건\n")

    # 2) 삭제
    n = 0
    for d in spec["deletes"]:
        if d["sku"] not in by_sku:
            continue
        print(f"  [삭제] {d['sku']} {by_sku[d['sku']]['name']} — {d.get('_이유','')}")
        if apply:
            sb.table("menu_items").delete().eq("sku", d["sku"]).execute()
        n += 1
    print(f"삭제: {n}건\n")

    # 3) 판매중지(노출 끔, 정본에는 보관)
    n = 0
    for d in spec["deactivate"]:
        if d["sku"] not in by_sku:
            continue
        print(f"  [중지] {d['sku']} {by_sku[d['sku']]['name']} — {d.get('_이유','')}")
        if apply:
            sb.table("menu_items").update(
                {"store_active": False, "delivery_active": False}
            ).eq("sku", d["sku"]).execute()
        n += 1
    print(f"판매중지: {n}건\n")

    # 4) 신메뉴 — SKU 는 접두어별 최대번호 다음부터
    mx: dict[str, int] = {}
    for sku in by_sku:
        m = re.match(r"([A-Z]+)-(\d+)", sku)
        if m:
            mx[m.group(1)] = max(mx.get(m.group(1), 0), int(m.group(2)))
    max_sort = max((i.get("sort_order") or 0) for i in items) if items else 0
    n = 0
    for it in spec["new_items"]:
        if normalize_menu_name(it["name"]) in existing_norm:
            continue  # 이미 있음(재실행 등)
        pfx = it["prefix"]
        mx[pfx] = mx.get(pfx, 0) + 1
        max_sort += 1
        sku = f"{pfx}-{mx[pfx]:03d}"
        row = {
            "sku": sku, "menu_type": "세트" if pfx == "SET" else "단일",
            "category": it["category"], "group_name": it.get("group_name"),
            "name": it["name"],
            "store_price": it.get("store_price"),
            "delivery_price": it.get("delivery_price"),
            "cost_source": it.get("_비고"),
            "store_active": it.get("store_price") is not None,
            "delivery_active": it.get("delivery_price") is not None,
            "sort_order": max_sort,
        }
        print(f"  [신규] {sku} {it['name']} "
              f"(매장 {it.get('store_price') or '–'} / 배달 {it.get('delivery_price') or '–'})")
        if apply:
            sb.table("menu_items").insert(row).execute()
        n += 1
    print(f"신메뉴 추가: {n}건\n")

    if not apply:
        print("실제 반영하려면:  python scripts/apply_menu_update.py --apply")
    else:
        print("완료. 웹 /menu 에서 확인 후, worker\\menu_diff.bat 로 지시서를 다시 만드세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
