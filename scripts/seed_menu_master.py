"""메뉴 정본 초기 데이터 넣기 — data/menu_master_seed.json → Supabase.

집 PC(또는 .env 에 SUPABASE_URL/KEY 가 있는 곳)에서 1회 실행:

    python scripts/seed_menu_master.py             # 없는 것만 추가(안전)
    python scripts/seed_menu_master.py --settings  # 설정(수수료·목표원가율·주문모델)만 다시 적용
    python scripts/seed_menu_master.py --force     # 시드값으로 전부 덮어쓰기

먼저 supabase/migrations/005_menu_master.sql 을 SQL Editor 에서 실행해
테이블을 만들어야 한다.

기본 모드는 '없는 것만 추가'라서, 웹 화면에서 고친 값은 건드리지 않는다.
--force 는 시드 재생성 후 처음부터 다시 맞출 때만 쓴다.
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.supabase_client import get_client  # noqa: E402

SEED = ROOT / "data" / "menu_master_seed.json"


def main() -> None:
    force = "--force" in sys.argv
    data = json.loads(SEED.read_text(encoding="utf-8"))
    sb = get_client()

    # 설정만 갱신 — 메뉴·가격은 건드리지 않는다(웹에서 고친 값 보존).
    if "--settings" in sys.argv:
        for key, value in data["settings"].items():
            sb.table("menu_settings").upsert(
                {"key": key, "value": value}, on_conflict="key").execute()
            print(f"menu_settings: {key} 갱신")
        print("설정만 반영했습니다. 메뉴 데이터는 그대로입니다.")
        return

    existing = {r["sku"] for r in sb.table("menu_items").select("sku").execute().data}
    items = data["items"] if force else [i for i in data["items"] if i["sku"] not in existing]
    if items:
        sb.table("menu_items").upsert(items, on_conflict="sku").execute()
    print(f"menu_items: {len(items)}건 반영 (기존 {len(existing)}건, force={force})")

    ch_existing = {(r["sku"], r["channel"])
                   for r in sb.table("menu_channels").select("sku,channel").execute().data}
    chans = data["channels"] if force else [
        c for c in data["channels"] if (c["sku"], c["channel"]) not in ch_existing]
    if chans:
        sb.table("menu_channels").upsert(chans, on_conflict="sku,channel").execute()
    print(f"menu_channels: {len(chans)}건 반영")

    st_existing = {r["key"] for r in sb.table("menu_settings").select("key").execute().data}
    for key, value in data["settings"].items():
        if force or key not in st_existing:
            sb.table("menu_settings").upsert(
                {"key": key, "value": value}, on_conflict="key").execute()
            print(f"menu_settings: {key} 반영")
    print("완료. 직원용 웹의 /menu 화면에서 확인하세요.")


if __name__ == "__main__":
    main()
