"""메뉴 소개글(한/영)을 AI 로 다시 쓴다 — 규칙 초안을 걷어내기 위함.

규칙 생성기로 만든 문장은 "~살아 있는 ~입니다"만 반복돼 카페 문구가 아니다
(사장님 2026-08-17). 무료 제미나이로 다시 쓴다.

무료 티어라 분당 할당량(429)이 걸린다. 한 건씩 쉬어 가며 돌리고, 실패한
메뉴는 건너뛰고 끝까지 간다 — 나중에 --only-empty 로 다시 채우면 된다.

    python scripts/rewrite_intros.py                # 판매중 전부 다시 쓰기
    python scripts/rewrite_intros.py --only-empty   # 비어 있는 것만
    python scripts/rewrite_intros.py --limit 20     # 20개만 (맛보기)
    python scripts/rewrite_intros.py --gap 5        # 호출 간격(초)
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
for pth in (ROOT, ROOT / "service"):
    if str(pth) not in sys.path:
        sys.path.insert(0, str(pth))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from database import supabase_client as db  # noqa: E402
from intro_ai import draft as ai_draft  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="메뉴 소개글 AI 재작성")
    ap.add_argument("--only-empty", action="store_true", help="비어 있는 것만")
    ap.add_argument("--limit", type=int, default=0, help="개수 제한(0=전부)")
    ap.add_argument("--gap", type=float, default=4.0, help="호출 간격(초)")
    ap.add_argument("--only-name-en", action="store_true",
                    help="영문 메뉴명이 없는 것만 (소개글은 그대로 둠)")
    args = ap.parse_args()

    menus = [m for m in db.menu_all()
             if m.get("store_active") or m.get("delivery_active")]
    if args.only_empty:
        menus = [m for m in menus if not (m.get("intro_ko") or "").strip()]
    if args.only_name_en:
        menus = [m for m in menus if not (m.get("name_en") or "").strip()]
    menus.sort(key=lambda m: (m.get("category") or "", m["sku"]))
    if args.limit:
        menus = menus[:args.limit]

    print(f"대상 {len(menus)}개 · 간격 {args.gap}초 "
          f"(예상 {len(menus) * args.gap / 60:.0f}분)\n", flush=True)

    ok = fail = 0
    for i, m in enumerate(menus, 1):
        try:
            ko, en, name_en = ai_draft(m["name"], m.get("category"),
                                       m.get("composition"), m.get("description"))
            fields = {}
            # --only-name-en 일 때는 이미 손본 소개글을 덮지 않는다.
            if not args.only_name_en:
                fields.update({"intro_ko": ko, "intro_en": en})
            if name_en and not (m.get("name_en") or "").strip():
                fields["name_en"] = name_en
            if fields:
                db.menu_update_item(m["sku"], fields)
            ok += 1
            print(f"[{i}/{len(menus)}] {m['sku']} {m['name'][:24]} → {name_en}",
                  flush=True)
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"[{i}/{len(menus)}] {m['sku']} {m['name'][:24]} → 실패 "
                  f"({str(e)[:80]})", flush=True)
        if i < len(menus):
            time.sleep(args.gap)

    print(f"\n완료 — 성공 {ok} · 실패 {fail}")
    if fail:
        print("실패분은 --only-empty 로 다시 돌리거나 화면의 ✨ 초안 만들기로 채우세요.")


if __name__ == "__main__":
    raise SystemExit(main())
