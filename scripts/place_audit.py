"""네이버 스마트플레이스 진단 CLI — 엔진은 `crawler/place_audit.py`.

실행:
    python -m scripts.place_audit           # 요약 출력
    python -m scripts.place_audit --save    # 결과를 DB 에 저장(/place 화면이 읽음)
    python -m scripts.place_audit --json    # 원본 JSON 을 reports/ 에 덤프

.env 의 NAVER_PLACE_ID 가 필요하다(베어글스 송도 = 2023997350).
읽기 전용이다 — 플레이스에 아무것도 쓰지 않는다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from crawler.place_audit import audit  # noqa: E402


def report(a: dict) -> None:
    st = a.get("stats") or {}
    sc = a.get("score") or {}
    print(f"== {a.get('name','?')} (id {a.get('placeId')}) ==")
    print(f"카테고리   : {a.get('category')}")
    print(f"평점/리뷰  : {st.get('rating')} / 방문자 {st.get('visitorReviews')} "
          f"· 블로그 {st.get('blogReviews')}")
    print(f"점수       : {sc.get('done')}/{sc.get('total')} 통과")
    print()
    for c in a.get("checks", []):
        print(f"  {'OK ' if c['ok'] else '!! '} {c['label']:<18} {c['value']}")
    print()
    todo = a.get("todo") or []
    print("고칠 것:", ", ".join(todo) if todo else "없음 — 전부 통과")


if __name__ == "__main__":
    result = audit()
    report(result)

    if "--save" in sys.argv:
        from database import supabase_client as db
        db.menu_set_setting("place_audit", result)
        print("\n저장 완료 — 웹 /place 화면에서 보입니다.")

    if "--json" in sys.argv:
        dest = ROOT / "reports" / "place-audit.json"
        dest.parent.mkdir(exist_ok=True)
        dest.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                        encoding="utf-8")
        print(f"\n원본 → {dest}")
