"""네이버 스마트플레이스 실태 진단 — 공개 페이지에서 현재 세팅 상태를 뽑는다.

브라우저 도구·WebFetch 는 네이버 도메인이 막혀 있어서, 이미 쓰고 있는
`crawler.browser.BrowserSession`(Playwright)으로 공개 플레이스 페이지를 열고
`window.__APOLLO_STATE__` 의 `PlaceDetailBase` 를 읽는다. 로그인 불필요.

실행:
    python -m scripts.place_audit          # 요약만
    python -m scripts.place_audit --json   # 원본 JSON 도 scratch 로 덤프

.env 의 NAVER_PLACE_ID 가 필요하다(베어글스 송도 = 2023997350).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from crawler.browser import BrowserSession  # noqa: E402

TABS = ["home", "menu/list", "feed"]


def _apollo(html: str) -> dict:
    m = re.search(r"__APOLLO_STATE__\s*=\s*({.*?});", html, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}


def _base(state: dict) -> dict:
    for v in state.values():
        if isinstance(v, dict) and v.get("__typename") == "PlaceDetailBase":
            return v
    return {}


def _menus(state: dict) -> list[str]:
    return [v.get("name", "?") for v in state.values()
            if isinstance(v, dict) and v.get("__typename") == "Menu"]


def audit(place_id: str) -> dict:
    out: dict = {"placeId": place_id}
    with BrowserSession() as sess:
        page = sess.page
        for tab in TABS:
            page.goto(f"https://m.place.naver.com/restaurant/{place_id}/{tab}",
                      wait_until="domcontentloaded")
            page.wait_for_timeout(7000)
            html = page.content()
            state = _apollo(html)
            if tab == "home":
                out["base"] = _base(state)
            elif tab == "menu/list":
                out["menus"] = _menus(state)
            elif tab == "feed":
                # 소식 최신 발행이 언제인지만 본다(상대시간 표기 포함).
                txt = page.inner_text("body")
                out["feed_recent"] = re.findall(
                    r"(\d+일 전|\d+시간 전|어제|오늘|20\d\d\.\d\d\.\d\d\.)", txt)[:5]
    return out


def report(a: dict) -> None:
    b = a.get("base", {})
    miss = b.get("missingInfo") or {}
    print(f"== {b.get('name','?')} (id {a['placeId']}) ==")
    print(f"카테고리   : {b.get('category')}")
    print(f"평점/리뷰  : {b.get('visitorReviewsScore')} / 방문자 "
          f"{b.get('visitorReviewsTotal')} · 블로그 {b.get('cafeBlogReviewsTotal')}")
    print()
    checks = [
        ("메뉴 등록", f"{len(a.get('menus', []))}건", len(a.get("menus", [])) >= 15),
        ("영업시간", "미등록" if miss.get("isBizHourMissing") else "등록됨",
         not miss.get("isBizHourMissing")),
        ("대표번호(스마트콜)", "없음" if not b.get("phone") else b["phone"],
         bool(b.get("phone") or b.get("virtualPhone"))),
        ("톡톡", "미연결" if not b.get("talktalkUrl") else "연결됨",
         bool(b.get("talktalkUrl"))),
        ("블로그 연동", "미연동" if not b.get("naverBlog") else "연동됨",
         bool(b.get("naverBlog"))),
        ("소개글", "없음" if miss.get("isDescriptionMissing") else "등록됨",
         not miss.get("isDescriptionMissing")),
        ("메뉴 사진", "없음" if miss.get("isMenuImageMissing") else "있음",
         not miss.get("isMenuImageMissing")),
        ("편의시설", f"{len(b.get('conveniences') or [])}종",
         bool(b.get("conveniences"))),
        ("찾아오는 길", "없음" if miss.get("isAccessorMissing") else "등록됨",
         not miss.get("isAccessorMissing")),
        ("소식 최근 발행", ", ".join(a.get("feed_recent") or []) or "확인 실패",
         bool(a.get("feed_recent"))),
    ]
    for label, value, ok in checks:
        print(f"  {'OK ' if ok else '!! '} {label:<18} {value}")
    bad = [c[0] for c in checks if not c[2]]
    print()
    print("고칠 것:", ", ".join(bad) if bad else "없음 — 전부 통과")


if __name__ == "__main__":
    pid = os.getenv("NAVER_PLACE_ID", "").strip()
    if not pid:
        raise SystemExit(".env 에 NAVER_PLACE_ID 가 없습니다.")
    result = audit(pid)
    report(result)
    if "--json" in sys.argv:
        dest = ROOT / "reports" / "place-audit.json"
        dest.parent.mkdir(exist_ok=True)
        dest.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                        encoding="utf-8")
        print(f"\n원본 → {dest}")
