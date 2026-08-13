"""네이버 등록 메뉴 ↔ 메뉴 정본 불일치 비교.

`/menu` 화면의 📡 채널 대조가 보여주는 것과 같은 비교를, 네이버만 놓고
터미널에서 자세히 본다. 무엇을 고쳐야 하는지 목록으로 뽑는 게 목적이다.

네이버 가격은 **매장가(store_price)** 와 비교한다. 배달앱과 달리 네이버는
매장에서 파는 값을 그대로 올리기 때문이다.

비교는 네 단계로 나눈다. 그냥 '없음'으로 뭉뚱그리면 실제로는 이름만 다른
같은 메뉴가 잔뜩 섞여 들어와 고칠 것을 못 고른다.

    ⓪ 이름만 다름   이름이 닮았고 값도 같다 → 이름 통일
    ① 가격 불일치   같은 메뉴인데 값이 다르다 → 한쪽을 고친다
    ② 이름 많이 다름 값은 같은데 이름이 안 닮았다 → 눈으로 확인
    ③ 진짜 한쪽에만  대응이 없다 → 등록하거나 내린다

쓰는 법:
    python scripts/naver_menu_diff.py
    python scripts/naver_menu_diff.py --csv    # 결과를 CSV 로도 저장

수집 자체는 이 스크립트가 하지 않는다. `/menu` 화면의 📡 채널 대조에서
"채널수집 요청"을 눌러 집 PC 일꾼이 받아온 스냅샷을 읽는다.
"""
from __future__ import annotations

import argparse
import csv
import difflib
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

CHANNEL = "naver"
OUT_CSV = ROOT / "data" / "naver_menu_diff.csv"


def looks_same(a: str, b: str) -> bool:
    """이름이 같은 메뉴를 가리키는가. 수식어가 앞뒤로 붙는 경우가 많다."""
    if a in b or b in a:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.75


def compare():
    norm = db.normalize_menu_name
    snaps = [s for s in db.menu_snapshots_all() if s.get("channel") == CHANNEL]
    menus = db.menu_all()
    if not snaps:
        print("네이버 스냅샷이 없습니다 — /menu 화면에서 채널수집을 먼저 돌려주세요.")
        return None

    by_norm: dict[str, list] = {}
    for m in menus:
        by_norm.setdefault(norm(m["name"]), []).append(m)

    price_diff, only_naver, matched = [], [], []
    for s in snaps:
        cand = by_norm.get(norm(s["menu_name"]))
        if not cand:
            only_naver.append(s)
            continue
        m = cand[0]
        matched.append((s, m))
        mp, np_ = m.get("store_price"), s.get("price")
        if mp and np_ and int(mp) != int(np_):
            price_diff.append((s, m, int(np_) - int(mp)))

    seen = {norm(m["name"]) for _, m in matched}
    only_master = [m for m in menus
                   if norm(m["name"]) not in seen and m.get("store_active")]

    # ⓪ 이름만 다름 — 이름이 닮은 것끼리. 값이 같으면 확신을 더한다.
    renamed, rest_naver, taken = [], [], set()
    for s in only_naver:
        a = norm(s["menu_name"])
        best, best_score = None, 0.0
        for m in only_master:
            if m["sku"] in taken:
                continue
            b = norm(m["name"])
            if not looks_same(a, b):
                continue
            r = difflib.SequenceMatcher(None, a, b).ratio()
            same_price = bool(s.get("price") and m.get("store_price")
                              and int(s["price"]) == int(m["store_price"]))
            score = r + (1.0 if same_price else 0.0)
            if score > best_score:
                best, best_score = m, score
        if best is not None and best_score >= 0.75:
            taken.add(best["sku"])
            renamed.append((s, best))
        else:
            rest_naver.append(s)
    only_master = [m for m in only_master if m["sku"] not in taken]

    # ② 이름은 안 닮았는데 값이 똑같은 쌍 — 이름을 크게 바꾼 경우다.
    #    ('리얼수박주스' ↔ '생과일 수박주스', '코코아라떼' ↔ '초코라떼')
    #    값이 같은 후보가 여럿일 때 먼저 나온 것을 집으면 짝이 엇갈린다
    #    (수박주스 ↔ 코코아라떼처럼). 그중 이름이 가장 닮은 것을 고른다.
    price_pair, rest2, taken2 = [], [], set()
    for s in rest_naver:
        if not s.get("price"):
            rest2.append(s)
            continue
        cands = [m for m in only_master
                 if m["sku"] not in taken2 and m.get("store_price")
                 and int(m["store_price"]) == int(s["price"])]
        if not cands:
            rest2.append(s)
            continue
        a = norm(s["menu_name"])
        hit = max(cands, key=lambda m: difflib.SequenceMatcher(
            None, a, norm(m["name"])).ratio())
        taken2.add(hit["sku"])
        price_pair.append((s, hit))
    only_master = [m for m in only_master if m["sku"] not in taken2]

    return {
        "snaps": snaps, "menus": menus, "matched": matched,
        "renamed": renamed, "price_diff": price_diff,
        "price_pair": price_pair, "only_naver": rest2, "only_master": only_master,
    }


def report(r):
    bar = "=" * 92
    when = str(r["snaps"][0].get("collected_at"))[:10]
    print(f"네이버 등록 {len(r['snaps'])}건 · 정본 {len(r['menus'])}건 (수집 {when})")

    print(f"\n{bar}\n⓪ 이름만 다름 — {len(r['renamed'])}건  (같은 메뉴 · 이름 통일)\n{bar}")
    for s, m in sorted(r["renamed"], key=lambda x: x[1]["name"]):
        mp, np_ = m.get("store_price"), s.get("price")
        tail = (f"  ※ 값도 다름 {int(mp):,}→{int(np_):,}"
                if mp and np_ and int(mp) != int(np_) else "")
        print(f"  정본   {m['name']}\n  네이버 {s['menu_name']}{tail}\n")

    print(f"{bar}\n① 가격 불일치 — {len(r['price_diff'])}건  (정본 매장가 ↔ 네이버)\n{bar}")
    for s, m, d in sorted(r["price_diff"], key=lambda x: -abs(x[2])):
        who = "네이버가 비쌈" if d > 0 else "네이버가 쌈"
        print(f"  {m['name'][:34]:<36} {int(m['store_price']):>7,} → "
              f"{int(s['price']):>7,}  ({d:+,}원 · {who})")

    print(f"\n{bar}\n② 이름 많이 다름 — {len(r['price_pair'])}건  (값이 같아 짝으로 보임 · 확인)\n{bar}")
    for s, m in sorted(r["price_pair"], key=lambda x: -(x[0].get("price") or 0)):
        print(f"  {int(s.get('price') or 0):>7,}원  정본 {m['name']}\n"
              f"           네이버 {s['menu_name']}")

    print(f"\n{bar}\n③ 네이버에만 있음 — {len(r['only_naver'])}건\n{bar}")
    for s in sorted(r["only_naver"], key=lambda x: -(x.get("price") or 0)):
        print(f"  {int(s.get('price') or 0):>7,}원  {s['menu_name']}")

    print(f"\n{bar}\n④ 정본에만 있음 — {len(r['only_master'])}건  (매장 판매중인데 네이버 미등록)\n{bar}")
    for m in sorted(r["only_master"], key=lambda x: (x.get("category") or "", x["name"])):
        print(f"  {int(m.get('store_price') or 0):>7,}원  [{(m.get('category') or '')[:8]:<8}] {m['name']}")

    ok = len(r["matched"]) - len(r["price_diff"])
    print(f"\n{bar}\n요약: 그대로 맞는 것 {ok}건 · 고칠 것 "
          f"{len(r['renamed']) + len(r['price_diff']) + len(r['price_pair'])}건 "
          f"(이름 {len(r['renamed'])} · 가격 {len(r['price_diff'])} · 확인 {len(r['price_pair'])}) "
          f"· 한쪽에만 {len(r['only_naver']) + len(r['only_master'])}건")


def write_csv(r):
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["구분", "정본 메뉴명", "정본 매장가", "네이버 메뉴명", "네이버 가격", "메모"])
        for s, m in r["renamed"]:
            w.writerow(["이름만 다름", m["name"], m.get("store_price"),
                        s["menu_name"], s.get("price"), "이름 통일"])
        for s, m, d in r["price_diff"]:
            w.writerow(["가격 불일치", m["name"], m.get("store_price"),
                        s["menu_name"], s.get("price"), f"{d:+,}원"])
        for s, m in r["price_pair"]:
            w.writerow(["이름 많이 다름", m["name"], m.get("store_price"),
                        s["menu_name"], s.get("price"), "같은 메뉴인지 확인"])
        for s in r["only_naver"]:
            w.writerow(["네이버에만", "", "", s["menu_name"], s.get("price"), "정본에 없음"])
        for m in r["only_master"]:
            w.writerow(["정본에만", m["name"], m.get("store_price"), "", "", "네이버 미등록"])
    print(f"\n→ {OUT_CSV}")


def main():
    ap = argparse.ArgumentParser(description="네이버 메뉴 ↔ 정본 비교")
    ap.add_argument("--csv", action="store_true", help="결과를 CSV 로도 저장")
    args = ap.parse_args()
    r = compare()
    if not r:
        return 1
    report(r)
    if args.csv:
        write_csv(r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
