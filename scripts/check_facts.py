# -*- coding: utf-8 -*-
"""답글이 참고하는 '사실'이 맞는지 점검한다 — 틀린 사실 하나가 손님에게 나간다.

왜 필요한가:
    답글 프롬프트에는 reference/reply_context.md(사실 카드)가 통째로 들어간다.
    여기에 없는 메뉴나 틀린 제조 사실이 적혀 있으면 AI 가 그대로 손님에게
    말한다. 실제로 2026-08-23 점검에서 세 가지가 나왔다.
      ① 관리용 태그·키워드칩이 붙은 메뉴명([SET]…, '플레인 베이글 대박맛집입니다')
      ② 손으로 적어 둔 제조 사실 블록이 파일 재생성 때 통째로 사라짐
      ③ 손님이 주문할 수 없는 반제품(플레인크림치즈(반제품))이 메뉴로 올라감

실행: python scripts/check_facts.py     (문제가 있으면 종료코드 1)
새벽 자동 점검이 매일 돌린다.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CARD = ROOT / "reference" / "reply_context.md"

# 사실이 아닌 표현 — 베이글은 본사 냉동 납품이고 매장은 토스팅만 한다.
FALSE_CLAIMS = ("직접 반죽", "수제 베이글", "매장에서 구운", "매장에서 굽",
                "갓 구운", "새벽부터 구", "매일 아침 굽", "손반죽")
# 이 문구가 사라지면 제조 사실 블록이 통째로 날아간 것이다.
REQUIRED = ("본사 새벽 냉동", "그릴 토스팅")


def main() -> int:
    problems = []
    if not CARD.exists():
        print(f"[오류] 사실 카드가 없습니다: {CARD}")
        return 1
    text = CARD.read_text(encoding="utf-8")

    # 1) 제조 사실이 살아 있나
    for must in REQUIRED:
        if must not in text:
            problems.append(f"제조 사실이 빠졌습니다 — '{must}' 문구 없음")

    # 2) 사실이 아닌 표현이 섞였나 ('금지:' 목록에 예시로 적힌 건 빼고 본다)
    body = "\n".join(l for l in text.split("\n")
                     if not l.strip().startswith(("- 금지:", '"', "  ")))
    for bad in FALSE_CLAIMS:
        if bad in body:
            problems.append(f"사실과 다른 표현이 사실 카드에 있음: '{bad}'")

    # 3) 메뉴명이 정본과 맞나
    names = [l[2:].strip() for l in text.split("\n")
             if l.startswith("- ") and not l.startswith("- **")]
    section = text.split("## 판매 메뉴")
    menu_names = []
    if len(section) > 1:
        for line in section[1].split("##")[0].split("\n"):
            if line.startswith("- "):
                menu_names.append(line[2:].strip())
    dirty = [m for m in menu_names
             if re.search(r"\[[^\]]{1,12}\]", m) or "대박맛집" in m
             or "반제품" in m or m != m.strip()]
    for m in dirty[:10]:
        problems.append(f"메뉴명이 오염됨(태그·키워드칩·반제품): {m!r}")

    try:
        from database import supabase_client as db
        from assistant.beargels import _clean_menu
        canon = {_clean_menu(r.get("name")) for r in db.menu_all()}
        unknown = [m for m in menu_names if m not in canon]
        for m in unknown[:10]:
            problems.append(f"정본에 없는 메뉴명: {m!r}")
    except Exception as e:  # noqa: BLE001 — DB 를 못 봐도 나머지 점검은 한다
        print(f"(정본 대조는 건너뜀: {str(e)[:80]})")

    if not problems:
        print(f"사실 카드 이상 없음 — 메뉴 {len(menu_names)}개, 제조 사실 유지됨")
        return 0
    print(f"[문제 {len(problems)}건]")
    for p in problems:
        print(" ·", p)
    print("\n고치는 법: python -c \"import crawler.reply_history as r; "
          "r._write_context([])\" 로 정본에서 다시 만든다.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
