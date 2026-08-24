"""'케이크 · 산도' 한 분류를 '케이크' 와 '산도' 둘로 나눈다 (1회용).

사장님 지시 2026-08-24. 나누는 기준은 이미 데이터에 있다 — group_name 이
'생과일 산도' 인 것이 산도, 나머지(테디케이크·케이크)가 케이크다.

같이 옮기는 것:
  목표 원가율   '케이크 · 산도' 키 하나뿐이라, 그냥 나누면 두 분류 다
                '기타 35%' 로 떨어진다. 값을 양쪽에 복사하고 옛 키는 지운다.

건드리지 않는 것:
  SKU          전부 DESRT-xxx 그대로. 새 메뉴도 DESRT 를 이어 붙이도록
               _CATEGORY_PREFIX 에 두 분류를 넣어 뒀다.
  sort_order   이미 케이크 520~650 · 산도 660~700 으로 갈려 있어 손댈 게 없다.
  채널          배민·쿠팡·네이버 메뉴판의 노출 분류는 이것과 별개다(사장님 확인).

쓰는 법:
    python scripts/split_cake_sando.py            # 미리보기
    python scripts/split_cake_sando.py --apply    # 실제 적용
"""
from __future__ import annotations

import argparse
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

OLD = "케이크 · 산도"
SANDO_GROUP = "생과일 산도"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    items = [m for m in db.menu_all() if m.get("category") == OLD]
    if not items:
        print(f"'{OLD}' 분류가 없습니다 — 이미 나뉘었거나 이름이 다릅니다.")
        return 0

    plan = {m["sku"]: ("산도" if m.get("group_name") == SANDO_GROUP else "케이크")
            for m in items}
    for want in ("케이크", "산도"):
        rows = [m for m in items if plan[m["sku"]] == want]
        print(f"\n[{want}] {len(rows)}개")
        for m in sorted(rows, key=lambda x: x.get("sort_order") or 0):
            print(f"   {m.get('sort_order'):>5}  {m['sku']:10s} {m['name']}")

    rates = db.menu_settings_all().get("target_cost_rates") or {}
    cur = rates.get(OLD)
    print(f"\n목표 원가율: '{OLD}' = {cur}% → '케이크'·'산도' 양쪽에 복사"
          if cur is not None else
          f"\n목표 원가율: '{OLD}' 키가 없어 옮길 값이 없습니다(기본 35% 적용)")

    if not args.apply:
        print("\n실제 적용: python scripts/split_cake_sando.py --apply")
        return 0

    sb = db.get_client()
    for sku, cat in plan.items():
        sb.table("menu_items").update({"category": cat}).eq("sku", sku).execute()
    print(f"\n메뉴 {len(plan)}개 분류 변경 완료")

    if cur is not None:
        rates["케이크"] = cur
        rates["산도"] = cur
        rates.pop(OLD, None)
        db.menu_set_setting("target_cost_rates", rates)
        print(f"목표 원가율 옮김: 케이크 {cur}% · 산도 {cur}% (옛 키 삭제)")

    left = [m for m in db.menu_all() if m.get("category") == OLD]
    print(f"확인: '{OLD}' 에 남은 메뉴 {len(left)}개 (0이어야 정상)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
