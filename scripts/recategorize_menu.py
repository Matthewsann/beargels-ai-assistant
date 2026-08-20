"""메뉴 정본 카테고리 재편 — 고객이 보는 메뉴판 기준 (2026-08-21).

왜 바꾸나(사장님과 정리):
 · 첫 칸이 '베이커리'였다. 베이글 전문점인데 일반 빵집 이름이라 간판이 안 보인다.
 · '시그니처&스페셜' 20개가 잡탕이었다 — 커피 8 + 프로틴 3 + 하이볼 4 + 비타 3
   + 라떼 1 + 빙수 1. 특히 소금 슈페너 6종이 여기 묶여 있어서 '커피'를 누른
   고객이 이 집 대표 메뉴를 못 봤다. 시그니처는 카테고리가 아니라 배지여야 한다.
 · 디저트 43개가 한 덩어리라 배달앱에서 스크롤이 끝나지 않았다.

    python scripts/recategorize_menu.py            # 미리보기(안 씀)
    python scripts/recategorize_menu.py --apply    # 실제 반영
"""
from __future__ import annotations

import argparse
import collections
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

# 카테고리 순서 = 메뉴판 순서. 매장 기준(빵 사러 온 손님)으로 잡는다.
ORDER = ["베이글", "크림치즈", "샌드위치", "샐러드",
         "케이크 · 산도", "구움과자 · 간식", "빙수",
         "커피", "논커피", "티", "에이드 · 스무디",
         "보틀 1L", "세트", "반제품"]

# 통째로 옮기는 것(이름만 바뀜)
RENAME = {"베이커리": "베이글", "보틀": "보틀 1L", "에이드&스무디": "에이드 · 스무디"}

# SKU 를 콕 집어 옮기는 것 — 흩어져 있던 것들을 제자리로.
MOVE = {}

def _put(cat, *skus):
    for s in skus:
        MOVE[s] = cat

# 디저트 43 → 셋으로. 케이크와 산도는 '생크림+과일'이라 한 칸에 둔다.
_put("케이크 · 산도",
     *[f"DESRT-{n:03d}" for n in range(6, 20)],      # 테디4 · 말렌카2 · 케이크8
     *[f"DESRT-{n:03d}" for n in range(39, 44)])     # 생과일 산도 5
_put("구움과자 · 간식",
     "DESRT-004", "DESRT-030",                        # 베이글 러스크칩 2
     *[f"DESRT-{n:03d}" for n in range(20, 30)],      # 버터바3 · 붕어3 · 한입간식4
     *[f"DESRT-{n:03d}" for n in range(31, 39)])      # 버터떡2 · 스콘 · 쫀득쿠키3 · 선물세트2
_put("빙수", "DESRT-001", "DESRT-002", "DESRT-003",   # 얼먹 코르네 3
     "DESRT-005",                                     # 인절미 팥컵빙
     "SIG-020")                                       # 애플망고 컵빙수 — 음료가 아니었다

# '시그니처&스페셜' 해체 → 고객이 찾는 칸으로
_put("커피", "SIG-001", "SIG-002", "SIG-003", "SIG-004", "SIG-005",  # 소금 슈페너 5
     "SIG-019",                                                      # 소금 슈페너 라떼
     "SIG-006", "SIG-008")                                           # 샤케라또 · 벤치프레소
_put("논커피", "SIG-007",                                            # 밤 티라미슈 라떼
     "SIG-009", "SIG-010", "SIG-011")                                # 프로틴 3
_put("에이드 · 스무디",
     "SIG-012", "SIG-013", "SIG-014", "SIG-015",                     # 논알콜 하이볼 4
     "SIG-016", "SIG-017", "SIG-018")                                # 비타 3

# 소분류(메뉴판 안에서 묶이는 단위) 손보기.
# 소금 슈페너는 이 집 대표 메뉴 — 커피 칸 맨 위에 '시그니처'로 서게 한다.
GROUP = {}
for s in ("SIG-001", "SIG-002", "SIG-003", "SIG-004", "SIG-005", "SIG-019"):
    GROUP[s] = "시그니처 소금커피"
for s in ("DESRT-039", "DESRT-040", "DESRT-041", "DESRT-042", "DESRT-043"):
    GROUP[s] = "생과일 산도"          # 지금은 소분류가 비어 있다
GROUP["SIG-020"] = "빙수"
GROUP["DESRT-005"] = "빙수"

# 카테고리 안에서의 줄 순서 — 대표 메뉴를 위로.
GROUP_FIRST = {
    "커피": ["시그니처 소금커피"],
    "빙수": ["코르네"],
}


def target_category(it):
    if it["sku"] in MOVE:
        return MOVE[it["sku"]]
    return RENAME.get(it["category"], it["category"])


def main():
    ap = argparse.ArgumentParser(description="메뉴 카테고리 재편")
    ap.add_argument("--apply", action="store_true", help="실제로 반영")
    args = ap.parse_args()

    items = db.menu_all()
    plan = []
    for it in items:
        cat = target_category(it)
        grp = GROUP.get(it["sku"], it.get("group_name"))
        if cat != it["category"] or grp != it.get("group_name"):
            plan.append((it, cat, grp))

    # 정렬값 — 카테고리 순서 × 소분류 우선 × 기존 순서. 화면·채널 모두 이 순서를 쓴다.
    after = [(it, target_category(it)) for it in items]
    by_cat = collections.defaultdict(list)
    for it, cat in after:
        by_cat[cat].append(it)
    order_plan = {}
    n = 0
    for ci, cat in enumerate(ORDER):
        rows = by_cat.get(cat, [])
        first = GROUP_FIRST.get(cat, [])

        def key(it):
            g = GROUP.get(it["sku"], it.get("group_name")) or ""
            return (first.index(g) if g in first else len(first),
                    it.get("sort_order") or 0)

        for it in sorted(rows, key=key):
            n += 1
            order_plan[it["sku"]] = (ci + 1) * 1000 + n

    unknown = [c for c in by_cat if c not in ORDER]
    if unknown:
        print("⚠ ORDER 에 없는 분류:", unknown)
        return 1

    print("── 분류가 바뀌는 메뉴", len(plan), "개")
    for it, cat, grp in plan:
        g = "" if grp == it.get("group_name") else f"  [소분류 {it.get('group_name') or '없음'} → {grp}]"
        print(f"  {it['sku']:<11} {it['name'][:26]:<28} {it['category']} → {cat}{g}")

    print("\n── 개편 후 메뉴판")
    for cat in ORDER:
        rows = by_cat.get(cat, [])
        sell = [r for r in rows if r.get("store_active") or r.get("delivery_active")]
        note = "  (내부용 · 메뉴판 제외)" if cat == "반제품" else ""
        print(f"  {cat:<16} {len(rows):>3}개  판매중 {len(sell):>3}{note}")

    if not args.apply:
        print("\n미리보기입니다. 반영하려면 --apply")
        return 0

    print("\n반영 중…")
    ok = fail = 0
    for it in items:
        cat = target_category(it)
        grp = GROUP.get(it["sku"], it.get("group_name"))
        so = order_plan[it["sku"]]
        if (cat == it["category"] and grp == it.get("group_name")
                and so == it.get("sort_order")):
            continue
        try:
            db.menu_update_item(it["sku"], {"category": cat, "group_name": grp,
                                            "sort_order": so})
            ok += 1
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"  실패 {it['sku']}: {str(e)[:70]}")
    print(f"완료 — 수정 {ok}건 · 실패 {fail}건")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
