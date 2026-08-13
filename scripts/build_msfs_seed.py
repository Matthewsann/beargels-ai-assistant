"""엠즈푸드 발주품목 CSV → 자재 등록용 JSON.

발주 사이트가 주는 '규격'은 사람이 읽는 문자열이라(1.2kg*8ea/box) 그대로는
원가 계산에 못 쓴다. 여기서 **한 번 주문하면 실제로 손에 들어오는 양**과
그 값으로 바꾼다.

핵심 규칙 — 규격의 마지막 곱셈수가 '입수량'과 같으면 그건 박스 포장을 설명한
것이라 빼고, 다르면 그게 파는 단위 안에 든 개수다.
    1.2kg*8ea/box, 입수량 8  → 8은 박스당 개수 → 1개 = 1.2kg
    190ml*30ea/BOX, 입수량 1 → 파는 단위가 박스 → 1박스 = 5,700ml
값은 **공급가(부가세 별도)** 를 쓴다. 매입세액공제를 받으므로 실제 원가는
부가세를 뺀 금액이다.
"""
from __future__ import annotations

import csv
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "orderlink_items.csv"
OUT = ROOT / "data" / "msfs_ingredients.json"

SUPPLIER = "엠즈푸드"

# 발주 사이트 분류 → 우리 자재 분류
CAT_MAP = {
    "유제품": "유제품",
    "제과 냉동": "냉동·냉장 완제품",
    "제빵 냉동": "냉동·냉장 완제품",
    "농산물": "채소·과일",
    "가루류": "가루·당류·조미",
    "초콜릿": "가루·당류·조미",
    "비식품": "포장·소모품",
}
# '가공식품' 은 범위가 너무 넓어 이름으로 한 번 더 가른다.
NAME_CAT = [
    (("시럽", "소스", "잼", "페스토", "머스타드", "마요", "드레싱", "베이스", "청",
      "퓨레", "앙금", "단팥", "올리브", "피클"), "소스·시럽·잼"),
    (("티백", " 티", "티(", "홍차", "녹차", "커피", "원두", "에스프레소", "말차"), "커피·차"),
    (("우유", "두유", "연유", "생크림", "크림치즈", "치즈", "버터", "요거트"), "유제품"),
    (("사이다", "콜라", "탄산", "주스", "음료", "워터", "스파클링"), "음료"),
    (("설탕", "소금", "파우더", "가루", "분말", "시즈닝", "후추"), "가루·당류·조미"),
    (("베이글", "빵", "도우", "크루아상", "번", "브레드"), "베이커리"),
    (("베이컨", "햄", "닭", "치킨", "연어", "계란", "브레스트", "포크", "비프"), "육류·계란"),
]

UNIT_G = {"kg": 1000.0, "g": 1.0}
UNIT_ML = {"l": 1000.0, "ml": 1.0}


def _num(s):
    try:
        return float(str(s).replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


TOKEN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(kg|g|l|ml|티백|개입|ea|개|매|장|입|pk|봉|팩|box)", re.I)

# 포장 단계 — 낱개(1) < 팩(2) < 박스(3).
LEVEL = {"ea": 1, "개": 1, "개입": 1, "매": 1, "장": 1, "입": 1, "봉": 1,
         "pk": 2, "팩": 2, "box": 3}


def parse_spec(spec, sale_unit):
    """규격 문자열 → (한 번 주문하면 손에 들어오는 양, 단위).

    파는 단위(주문 화면의 '단위' 칸: EA/PK/BOX)보다 **위 단계**의 곱셈수는
    '박스에 몇 개 드는지'라 실제로 받는 양에 안 든다.
        110g*6ea*6pk  · 파는 단위 PK  → 6pk 는 박스 포장 → 1PK = 660g
        1.2kg*8ea     · 파는 단위 EA  → 8ea 는 박스 포장 → 1EA = 1.2kg
        90g*12ea      · 파는 단위 BOX → 뺄 것 없음      → 1BOX = 1,080g
    못 읽으면 (None, None).
    """
    spec = (spec or "").replace(",", "").strip()
    if not spec:
        return None, None
    sale = LEVEL.get((sale_unit or "EA").strip().lower(), 1)
    body = spec.rsplit("/", 1)[0]          # 뒤의 '/EA' '/box' 는 파는 단위 이름
    toks = TOKEN.findall(body)
    if not toks:
        return None, None

    # 티백은 개수로 세는 게 편하다(레시피에 '티백 1개'로 쓴다).
    for n, u in toks:
        if u == "티백":
            return float(n), "ea"

    size = None
    unit = None
    mult = 1.0
    for n, u in toks:
        u = u.lower()
        if u in UNIT_G and size is None:
            size, unit = float(n) * UNIT_G[u], "g"
        elif u in UNIT_ML and size is None:
            size, unit = float(n) * UNIT_ML[u], "ml"
        elif u in LEVEL:
            if LEVEL[u] < sale:            # 파는 단위 안에 드는 개수만 센다
                mult *= float(n)
        elif size is None:                 # 단위 없는 첫 숫자는 개수로 본다
            size, unit = float(n), "ea"

    if size is None:
        return (mult, "ea") if mult > 1 else (1.0, "ea")
    return size * mult, unit


# 이름 꼬리에 붙은 포장 설명. 두 모양이 있다 —
#   괄호형: '(1L*6ea/BOX)' '(92파이)' '(110g*6ea)*6pk/box'
#   구분자형: '_1,000ea/box' '_20ea*10pk/box'
# 앞쪽을 훑고 들어가면 이름 전체가 지워지므로(페스츄리호두과자_20ea*… → 빈 문자열)
# 꼬리가 구분자·괄호에서 바로 시작하는 것만 잡는다.
PACK_PAREN = re.compile(
    r"[\s_]*[(\[][^()\[\]]*\d[^()\[\]]*[)\]][\s*xX]*[\w\d.,*/]*$")
PACK_SEP = re.compile(
    r"[\s_]+\d[\d,\.]*\s*(?:kg|g|l|ml|ea|pk|장|매|티백)\S*\s*$", re.I)

NAME_COUNT = re.compile(r"[(/]\s*(\d[\d,]*)\s*(장|매|개|ea)\s*[)/]", re.I)


def count_from_name(name):
    """규격이 개수를 안 알려줄 때 이름에서 줍는다 — '(쉐프/200장)' → 200."""
    m = NAME_COUNT.search(name or "")
    return float(m.group(1).replace(",", "")) if m else None


def clean_name(raw, nonfood=False):
    """'1883_바닐라 시럽(1L*6ea/BOX)' → ('바닐라 시럽', '1883').

    비식품은 이름 자체가 '투명컵(무지)_16oz(92파이)_1,000ea/box' 처럼 규격
    설명으로 이어져 있어 브랜드 자르기를 하면 안 된다 — 꼬리만 떼어낸다.
    """
    s = (raw or "").strip()
    for _ in range(4):                         # 꼬리가 겹쳐 붙은 경우가 있다
        cut = s
        for rx in (PACK_SEP, PACK_PAREN):
            cut = rx.sub("", cut).strip(" _-·")
        if cut and cut != s and len(cut) >= 2:
            s = cut
        else:
            break
    brand = ""
    m = re.match(r"^\[([^\]]+)\]\s*(.+)$", s) or re.match(r"^\(([^)]+)\)\s*(.+)$", s)
    if m:
        brand, s = m.group(1), m.group(2)
    # 브랜드 자르기는 '짧은 브랜드_제품명' 형태일 때만. 비식품은 건드리지 않는다.
    if not nonfood and "_" in s:
        b, rest = s.split("_", 1)
        rest = rest.strip()
        # 뒤쪽이 숫자로 시작하면 그건 제품명이 아니라 규격이다('커피스틱_18cm_…')
        if (rest and not rest[0].isdigit() and len(b) <= 8
                and " " not in b and not re.search(r"\d", b)):
            brand, s = (brand or b), rest
    # '…_1000ea/box' 처럼 마지막까지 남은 포장 꼬리를 한 번 더 떼어낸다
    s = re.sub(r"[\s_]*\d[\d,]*\s*(?:ea|pk|매|장)\s*/\s*(?:box|pk|ea)\s*$", "",
               s, flags=re.I)
    # 꼬리를 떼다 보면 여는 괄호만 남는 수가 있다('… 12*250mm(개별/투명')
    if s.count("(") > s.count(")"):
        s = s[:s.rindex("(")]
    s = re.sub(r"[\s_]+", " ", s).strip(" _-·")
    return s, brand.strip()


def guess_cat(cat, name):
    if cat in CAT_MAP:
        return CAT_MAP[cat]
    for words, out in NAME_CAT:
        if any(w in name for w in words):
            return out
    return "가공식품"


def main():
    rows = list(csv.DictReader(SRC.open(encoding="utf-8-sig")))
    out, skipped = [], []
    for r in rows:
        qty, unit = parse_spec(r.get("규격"), r.get("단위"))
        cost = _num(r.get("공급가"))
        name, brand = clean_name(r.get("품목명"),
                                  nonfood=(r.get("카테고리") == "비식품"))
        if unit == "ea" and qty == 1:
            qty = count_from_name(r.get("품목명")) or 1.0
        if not name or not qty or not cost:
            skipped.append({"name": r.get("품목명"), "규격": r.get("규격"),
                            "사유": "규격 또는 가격을 읽지 못함"})
            continue
        note_bits = [b for b in (brand, (r.get("규격") or "").strip()) if b]
        out.append({
            "name": name,
            "brand": brand,
            "unit": unit,
            "pack_qty": round(qty, 3),
            "pack_cost": cost,
            "category": guess_cat(r.get("카테고리"), name),
            "supplier": SUPPLIER,
            "note": " · ".join(note_bits + [f"엠즈푸드 {r.get('품목코드')}"]),
            "code": r.get("품목코드"),
        })

    # 이름이 겹치면 브랜드를 붙여 구분한다(같은 이름은 하나만 등록되므로)
    seen = {}
    for it in out:
        seen.setdefault(it["name"], []).append(it)
    for name, group in seen.items():
        if len(group) > 1:
            for it in group:
                if it["brand"]:
                    it["name"] = f"{it['brand']} {name}"

    OUT.write_text(json.dumps({"ingredients": out, "skipped": skipped},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"변환 {len(out)}개 · 건너뜀 {len(skipped)}개 → {OUT}")
    return out, skipped


if __name__ == "__main__":
    items, skipped = main()
    for it in items[:30]:
        print(f"  {it['name'][:32]:<34} {it['pack_qty']:>8g}{it['unit']:<3} "
              f"{it['pack_cost']:>8,.0f}원  = {it['pack_cost']/it['pack_qty']:>8.2f}/{it['unit']}"
              f"  [{it['category']}]")
    if skipped:
        print("\n건너뛴 것:")
        for s in skipped:
            print("  ·", s["name"], "|", s["규격"])
