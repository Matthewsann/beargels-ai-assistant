"""네이버 스마트플레이스 실태 진단 — 공개 페이지에서 현재 세팅 상태를 뽑는다.

`/place` 페이지(스마트플레이스 목표 1단계 '최적화')가 쓰는 진단 엔진이다.
사장님이 무엇이 비었는지 **눈으로 찾지 않고 화면에서 바로** 알게 하는 게 목적.

브라우저 도구·WebFetch 는 네이버 도메인이 정책 차단이라, 이미 쓰고 있는
`crawler.browser.BrowserSession`(Playwright)으로 공개 플레이스 페이지를 열고
`window.__APOLLO_STATE__` 의 `PlaceDetailBase` 를 읽는다. **로그인 불필요.**

읽기 전용이다 — 플레이스에 아무것도 쓰지 않는다.

CLI 는 `python -m scripts.place_audit` (이 모듈의 얇은 껍데기).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

# 진단 항목 정의 — (열쇠, 화면에 보일 이름, 통과 기준 설명)
# 순서가 곧 화면 표시 순서다. 목표에 미치는 영향이 큰 것부터.
TABS = ["home", "menu/list", "feed"]

# 메뉴는 몇 개부터 '채워졌다'고 볼 것인가. 실판매가 50종+ 이므로 15를
# 최소선으로 둔다(전 품목 등록이 목표지만, 경고를 켜는 문턱).
MENU_MIN = 15


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


def collect(place_id: str, session=None) -> dict:
    """플레이스 공개 페이지를 열어 원자료를 모은다(브라우저 필요).

    session: 재사용할 BrowserSession. 없으면 새로 연다(일꾼이 이미 붙어 있는
    세션을 넘기면 attach 비용을 아낀다).
    """
    from crawler.browser import BrowserSession

    out: dict = {"placeId": place_id}
    own = session is None
    sess = session or BrowserSession()
    try:
        page = sess.__enter__().page if own else sess.page
        for tab in TABS:
            page.goto(f"https://m.place.naver.com/restaurant/{place_id}/{tab}",
                      wait_until="domcontentloaded")
            page.wait_for_timeout(7000)
            state = _apollo(page.content())
            if tab == "home":
                out["base"] = _base(state)
            elif tab == "menu/list":
                out["menus"] = _menus(state)
            elif tab == "feed":
                # 소식이 언제 마지막으로 올라갔는지만 본다(상대시간 표기 포함).
                txt = page.inner_text("body")
                out["feed_recent"] = re.findall(
                    r"(\d+일 전|\d+시간 전|어제|오늘|20\d\d\.\d\d\.\d\d\.)", txt)[:5]
    finally:
        if own:
            sess.__exit__(None, None, None)
    return out


def diagnose(raw: dict) -> dict:
    """원자료 → 화면·기록용 진단 결과(순수 로직, 브라우저 없이 테스트 가능).

    반환: {checkedAt, name, category, score, checks:[{key,label,value,ok}], todo:[...]}
    """
    b = raw.get("base") or {}
    miss = b.get("missingInfo") or {}
    menus = raw.get("menus") or []
    feed = raw.get("feed_recent") or []

    # 크롤이 통째로 실패하면 base 가 비어 온다. 이때 missingInfo 기반 항목은
    # "빠진 게 없다"로 읽혀 **전부 통과처럼** 보인다 — 이 화면에서 가장 위험한
    # 실패 방향이라, 자료가 없으면 통과로 치지 않고 '확인 실패'로 둔다.
    got = bool(b)

    def _flag(field, ok_text, bad_text):
        """missingInfo 의 '빠짐' 플래그 → (표시값, 통과여부)."""
        if not got:
            return "확인 실패", False
        return (bad_text, False) if miss.get(field) else (ok_text, True)

    hours_v, hours_ok = _flag("isBizHourMissing", "등록됨", "미등록")
    desc_v, desc_ok = _flag("isDescriptionMissing", "등록됨", "없음")
    mimg_v, mimg_ok = _flag("isMenuImageMissing", "있음", "없음")
    road_v, road_ok = _flag("isAccessorMissing", "등록됨", "없음")

    checks = [
        {"key": "menu", "label": "메뉴 등록", "value": f"{len(menus)}건",
         "ok": len(menus) >= MENU_MIN,
         "why": "손님이 가장 많이 여는 탭. 메뉴명·설명이 검색 매칭의 주재료"},
        {"key": "hours", "label": "영업시간",
         "value": hours_v, "ok": hours_ok,
         "why": "틀리면 헛걸음 → 나쁜 리뷰로 되돌아옴"},
        {"key": "phone", "label": "대표번호(스마트콜)",
         "value": b.get("phone") or b.get("virtualPhone") or "없음",
         "ok": bool(b.get("phone") or b.get("virtualPhone")),
         "why": "전화 유입이 통계에 잡히고, 전화 연결도 순위 신호"},
        {"key": "talk", "label": "톡톡",
         "value": "연결됨" if b.get("talktalkUrl") else "미연결",
         "ok": bool(b.get("talktalkUrl")),
         "why": "문의 전환 창구"},
        {"key": "blog", "label": "블로그 연동",
         "value": "연동됨" if b.get("naverBlog") else "미연동",
         "ok": bool(b.get("naverBlog")),
         "why": "블로그와 플레이스가 서로 밀어줌"},
        {"key": "desc", "label": "소개글",
         "value": desc_v, "ok": desc_ok,
         "why": "지역·상황 키워드가 들어가는 자리"},
        {"key": "menuimg", "label": "메뉴 사진",
         "value": mimg_v, "ok": mimg_ok,
         "why": "클릭률"},
        {"key": "amenity", "label": "편의시설",
         "value": f"{len(b.get('conveniences') or [])}종",
         "ok": bool(b.get("conveniences")),
         "why": "정보 완성도 기본 점수"},
        {"key": "road", "label": "찾아오는 길",
         "value": road_v, "ok": road_ok,
         "why": "방문 전환"},
        {"key": "feed", "label": "소식 최근 발행",
         "value": ", ".join(feed) if feed else "확인 실패",
         "ok": bool(feed),
         "why": "자주 관리되는 업체에 가점(업계 통설)"},
    ]
    done = sum(1 for c in checks if c["ok"])
    return {
        "checkedAt": datetime.now().isoformat(timespec="seconds"),
        "placeId": raw.get("placeId"),
        "name": b.get("name") or "",
        "category": b.get("category") or "",
        "score": {"done": done, "total": len(checks)},
        "stats": {
            "rating": b.get("visitorReviewsScore"),
            "visitorReviews": b.get("visitorReviewsTotal"),
            "blogReviews": b.get("cafeBlogReviewsTotal"),
        },
        "menus": menus,
        "checks": checks,
        "todo": [c["label"] for c in checks if not c["ok"]],
    }


def audit(place_id: str | None = None, session=None) -> dict:
    """수집 + 진단을 한 번에. place_id 를 안 주면 .env 의 NAVER_PLACE_ID."""
    pid = (place_id or os.getenv("NAVER_PLACE_ID", "")).strip()
    if not pid:
        raise ValueError("NAVER_PLACE_ID 가 없습니다(.env 확인)")
    return diagnose(collect(pid, session=session))
