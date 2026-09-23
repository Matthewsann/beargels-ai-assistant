"""네이버 스마트플레이스 실태 진단 — **손님이 보는 화면** 기준으로 세팅 상태를 뽑는다.

`/place` 페이지(스마트플레이스 목표 1단계 '최적화')가 쓰는 진단 엔진이다.
사장님이 무엇이 비었는지 **눈으로 찾지 않고 화면에서 바로** 알게 하는 게 목적.

원칙(사장님 2026-09-23): 도구 값과 손님 화면이 다르면 손님 화면이 진실이다.
그래서 판정 근거는 전부 손님이 여는 공개 페이지 `m.place.naver.com/restaurant/<id>/…`
가 화면에 그리는 데이터(`window.__APOLLO_STATE__`)다. **로그인 불필요.**

⚠️ 2026-09-23 오진 이력: 옛 버전은 `PlaceDetailBase.openingHours`(항상 null)·`phone`(비공개면
null)·`Menu:` 키(지금 없음)·`missingInfo.isBizHourMissing`(영업시간이 보여도 true 로 남음)을
읽어 "영업시간 미등록·대표번호 없음·메뉴 0건"이라 했다. 손님 화면엔 셋 다 있었다.
지금 화면이 실제로 쓰는 자리:
  - 영업시간  `ROOT_QUERY.placeDetail(...).newBusinessHours[].businessHours[]` (요일·시작·끝·라스트오더)
  - 전화      `placeDetail.phoneInfo.hasMobilePhoneNumber` / `PlaceDetailBase.phone|virtualPhone`
              (번호가 비공개(isPrivatePhone)면 null 이지만 홈에 '전화' 버튼은 있다)
  - 정보 탭 메뉴 `PlaceMenuItem:*` + `placeDetail.placeMenus`(menuCount·updatedAt·categories.itemIds)
              같은 항목 ID 가 여러 카테고리에 들어 있으면 화면에 **두 번** 그려진다
  - 네이버주문 `placeDetail.naverOrder`(포장·예약주문 서비스 — 메뉴 개수가 아니다)
  - 소식      `Feed` 객체의 날짜

세 탭 모두 서버 HTML 에 이 데이터가 실려 오므로 기본은 **브라우저 없이**(httpx, 모바일 UA) 받는다.
일꾼이 이미 붙어 있는 `BrowserSession` 을 넘기면 그 페이지로 연다(결과는 같다).
읽기 전용이다 — 플레이스에 아무것도 쓰지 않는다.

CLI 는 `python -m scripts.place_audit` (이 모듈의 얇은 껍데기).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

TABS = ["home", "menu/list", "feed"]

# 메뉴는 몇 개부터 '채워졌다'고 볼 것인가. 실판매가 50종+ 이므로 15를
# 최소선으로 둔다(전 품목 등록이 목표지만, 경고를 켜는 문턱).
MENU_MIN = 15

MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
DAYS = "월화수목금토일"
_DATE_RE = re.compile(r"20\d\d[.\-]\d\d[.\-]\d\d")


# ── 원자료 파싱(순수 함수) ─────────────────────────────────────

def _apollo(html: str) -> dict:
    m = (re.search(r"__APOLLO_STATE__\s*=\s*({.*?});\s*(?:window\.|</script>)", html, re.S)
         or re.search(r"__APOLLO_STATE__\s*=\s*({.*?});", html, re.S))
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


def _detail(state: dict) -> dict:
    """화면 데이터의 본체 `ROOT_QUERY.placeDetail(...)`."""
    rq = state.get("ROOT_QUERY") or {}
    for k, v in rq.items():
        if k.startswith("placeDetail(") and isinstance(v, dict):
            return v
    return {}


def _hours(detail: dict) -> list[dict]:
    """손님 화면의 영업시간 → [{day, start, end, last_order, off}] (요일 순)."""
    out = []
    for block in detail.get("newBusinessHours") or []:
        for d in block.get("businessHours") or []:
            bh = d.get("businessHours") or {}
            lo = [x.get("time") for x in (d.get("lastOrderTimes") or []) if x.get("time")]
            out.append({"day": d.get("day"), "start": bh.get("start"), "end": bh.get("end"),
                        "last_order": lo[0] if lo else None,
                        "off": bool(d.get("isDayOff")) or not (bh.get("start") and bh.get("end"))})
        if out:
            break                       # 첫 블록이 기본 영업시간
    out.sort(key=lambda h: DAYS.find(h["day"]) if h["day"] in DAYS else 99)
    return out


def hours_summary(hours: list[dict]) -> str:
    """[{day,start,end,...}] → '월~토 07:20–23:00 · 일 07:20–22:00 · 라스트오더 22:50'."""
    if not hours:
        return ""
    groups: list[list] = []          # [[days], key]
    for h in hours:
        key = ("휴무",) if h["off"] else (h["start"], h["end"])
        if groups and groups[-1][1] == key:
            groups[-1][0].append(h["day"])
        else:
            groups.append([[h["day"]], key])
    parts = []
    for days, key in groups:
        label = days[0] if len(days) == 1 else f"{days[0]}~{days[-1]}"
        parts.append(f"{label} 휴무" if key == ("휴무",) else f"{label} {key[0]}–{key[1]}")
    lo = {h["last_order"] for h in hours if h.get("last_order")}
    if lo:
        parts.append("라스트오더 " + "/".join(sorted(lo)))
    return " · ".join(parts)


def _menu_items(state: dict) -> list[dict]:
    out = []
    for v in state.values():
        if isinstance(v, dict) and v.get("__typename") == "PlaceMenuItem":
            price = v.get("price")
            out.append({"id": v.get("id"), "name": v.get("name", "?"),
                        "price": price.get("displayText") if isinstance(price, dict) else price,
                        "badges": v.get("badges") or []})
    return out


def _menu_dups(state: dict) -> list[str]:
    """여러 카테고리에 같은 항목 ID 가 들어가 화면에 두 번 그려지는 메뉴 이름."""
    seen: dict[str, int] = {}
    for v in state.values():
        if isinstance(v, dict) and v.get("__typename") == "PlaceMenuCategory":
            for i in v.get("itemIds") or []:
                seen[i] = seen.get(i, 0) + 1
    names = {m["id"]: m["name"] for m in _menu_items(state)}
    return [names.get(i, i) for i, n in seen.items() if n > 1]


def _feed_dates(state: dict) -> list[str]:
    """소식 발행일 최근 5개('2026.09.23'). `createdString`(YYYYMMDD)이 기준이다 —
    `relativeCreated` 는 오늘 글이 '오늘'로 와서 날짜 정규식에 안 걸린다(2026-09-23 실측)."""
    dates = []
    for v in state.values():
        if not (isinstance(v, dict) and v.get("__typename") == "Feed") or v.get("isDeleted"):
            continue
        cs = str(v.get("createdString") or "")
        if re.fullmatch(r"20\d{6}", cs):
            dates.append(f"{cs[:4]}.{cs[4:6]}.{cs[6:]}")
            continue
        for val in v.values():                       # 옛 형식 대비
            if isinstance(val, str) and _DATE_RE.match(val):
                dates.append(val[:10].replace("-", ".").rstrip("."))
                break
    return sorted(set(dates), reverse=True)[:5]


# ── 수집 ─────────────────────────────────────────────────────

def _fetch_html(place_id: str, tab: str, session=None) -> str:
    url = f"https://m.place.naver.com/restaurant/{place_id}/{tab}"
    if session is not None:
        page = session.page
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        return page.content()
    import httpx
    r = httpx.get(url, headers={"User-Agent": MOBILE_UA, "Accept-Language": "ko"},
                  timeout=30, follow_redirects=True)
    r.raise_for_status()
    return r.text


def collect(place_id: str, session=None) -> dict:
    """손님 화면 세 탭(홈·메뉴·소식)의 화면 데이터를 모은다.

    session: 일꾼이 이미 붙어 있는 BrowserSession(선택). 없으면 httpx 로 받는다 —
    세 탭 모두 서버 HTML 에 `__APOLLO_STATE__` 가 실려 오므로 결과는 같다.
    """
    out: dict = {"placeId": place_id, "source": "browser" if session is not None else "http"}
    for tab in TABS:
        state = _apollo(_fetch_html(place_id, tab, session))
        if tab == "home":
            d = _detail(state)
            out["base"] = _base(state)
            out["hours"] = _hours(d)
            blocks = d.get("newBusinessHours") or [{}]
            out["status"] = blocks[0].get("businessStatusDescription") or {}
            out["phone_info"] = d.get("phoneInfo") or {}
            out["naver_order"] = d.get("naverOrder") or {}
            buttons = next((v for k, v in d.items() if k.startswith("buttons(")), None) or []
            out["buttons"] = [b.get("key") for b in buttons if isinstance(b, dict)]
        elif tab == "menu/list":
            d = _detail(state)
            items = _menu_items(state)
            out["menu_items"] = items
            out["menus"] = [m["name"] for m in items]          # 옛 소비처 호환(이름 목록)
            out["menu_dups"] = _menu_dups(state)
            pm = d.get("placeMenus") or {}
            out["menu_meta"] = {"count": pm.get("menuCount"), "updatedAt": pm.get("updatedAt"),
                                "images": len(d.get("menuImages") or [])}
        elif tab == "feed":
            out["feed_recent"] = _feed_dates(state)
    return out


# ── 판정(순수 로직, 브라우저 없이 테스트 가능) ────────────────────

def diagnose(raw: dict) -> dict:
    """원자료 → 화면·기록용 진단 결과.

    반환: {checkedAt, name, category, score, checks:[{key,label,value,ok,why}], todo:[...]}
    판정 문구는 전부 '손님 화면에 무엇이 보이나' 기준이다.
    """
    b = raw.get("base") or {}
    miss = b.get("missingInfo") or {}
    hours = raw.get("hours") or []
    menus = raw.get("menu_items") or [{"name": n} for n in (raw.get("menus") or [])]
    dups = raw.get("menu_dups") or []
    meta = raw.get("menu_meta") or {}
    pinfo = raw.get("phone_info") or {}
    order = raw.get("naver_order") or {}
    feed = raw.get("feed_recent") or []

    # 크롤이 통째로 실패하면 base 가 비어 온다. 이때 '빠진 게 없다'로 읽혀
    # **전부 통과처럼** 보이는 게 가장 위험한 실패 방향이라, 자료가 없으면
    # 통과로 치지 않고 '확인 실패'로 둔다.
    got = bool(b)

    def _flag(field, ok_text, bad_text):
        if not got:
            return "확인 실패", False
        return (bad_text, False) if miss.get(field) else (ok_text, True)

    # 영업시간: 손님 화면에 요일별 시간이 그려지면 등록된 것이다.
    # missingInfo.isBizHourMissing 은 시간이 보여도 true 로 남아 있어(2026-09-23) 믿지 않는다.
    if not got:
        hours_v, hours_ok = "확인 실패", False
    elif hours and any(not h["off"] for h in hours):
        hours_v, hours_ok = hours_summary(hours), True
    else:
        hours_v, hours_ok = "손님 화면에 없음", False

    # 전화: 번호가 비공개라도 홈에 '전화' 버튼이 뜨면 손님은 걸 수 있다.
    number = b.get("phone") or b.get("virtualPhone")
    has_phone = bool(number or pinfo.get("hasMobilePhoneNumber") or b.get("hasMobilePhoneNumber")
                     or "phone" in (raw.get("buttons") or []))
    if not got:
        phone_v, phone_ok = "확인 실패", False
    elif number:
        phone_v, phone_ok = number, True
    elif has_phone:
        phone_v, phone_ok = "전화 버튼 있음(번호 비공개)", True
    else:
        phone_v, phone_ok = "없음", False
    smartcall = bool(b.get("virtualPhone") or pinfo.get("isVirtualPhone"))

    n_menu = len(menus)
    menu_v = f"{n_menu}건"
    if dups:
        menu_v += " · 같은 메뉴가 2번 보임"
    if meta.get("updatedAt"):
        menu_v += f" · {meta['updatedAt']} 갱신"
    menu_ok = n_menu >= MENU_MIN and not dups

    order_kinds = [k for k, f in (("포장", "isPickup"), ("예약주문", "isPreOrder"),
                                  ("배달", "isDelivery"), ("테이블주문", "isTableOrder")) if order.get(f)]
    order_ok = bool(order.get("items"))

    desc_v, desc_ok = _flag("isDescriptionMissing", "등록됨", "없음")
    mimg_v, mimg_ok = _flag("isMenuImageMissing", "있음", "없음")
    road_v, road_ok = _flag("isAccessorMissing", "등록됨", "없음")

    checks = [
        # '정보 탭' 메뉴다. 네이버주문 메뉴는 따로 등록돼 있어 그냥 '메뉴'라고 쓰면
        # 그걸 또 넣어야 하는 줄 안다. 손님이 '메뉴' 탭에서 보는 목록이 이것.
        {"key": "menu", "label": "메뉴 탭", "value": menu_v, "ok": menu_ok,
         "why": ("손님이 가장 많이 여는 탭. 지금은 1건이 두 번 보여 메뉴가 하나뿐인 가게로 보인다"
                 if dups else "손님이 가장 많이 여는 탭. 메뉴명·설명이 검색 매칭의 주재료")},
        {"key": "hours", "label": "영업시간", "value": hours_v, "ok": hours_ok,
         "why": "손님 화면의 '영업 중/종료·라스트오더' 표시. 틀리면 헛걸음 → 나쁜 리뷰"},
        {"key": "phone", "label": "전화" + ("" if smartcall or not phone_ok else "(스마트콜 아님)"),
         "value": phone_v, "ok": phone_ok,
         "why": "손님이 홈에서 바로 걸 수 있나. 스마트콜이면 전화 유입이 통계에 잡힌다"},
        {"key": "order", "label": "네이버주문",
         "value": ("·".join(order_kinds) + " 연결") if order_ok else "없음",
         "ok": order_ok, "why": "홈의 [주문] 버튼 — 전환 버튼이자 행동 데이터"},
        {"key": "talk", "label": "톡톡",
         "value": "연결됨" if b.get("talktalkUrl") else "미연결",
         "ok": bool(b.get("talktalkUrl")), "why": "문의 전환 창구"},
        {"key": "blog", "label": "블로그 연동",
         "value": "연동됨" if b.get("naverBlog") else "미연동",
         "ok": bool(b.get("naverBlog")), "why": "블로그와 플레이스가 서로 밀어줌"},
        {"key": "desc", "label": "소개글", "value": desc_v, "ok": desc_ok,
         "why": "지역·상황 키워드가 들어가는 자리"},
        {"key": "menuimg", "label": "메뉴판 사진",
         "value": (f"{meta['images']}장" if meta.get("images") else mimg_v), "ok": mimg_ok,
         "why": "메뉴 탭 아래 '메뉴판 이미지로 보기'"},
        {"key": "amenity", "label": "편의시설",
         "value": f"{len(b.get('conveniences') or [])}종", "ok": bool(b.get("conveniences")),
         "why": "정보 완성도 기본 점수"},
        {"key": "road", "label": "찾아오는 길", "value": road_v, "ok": road_ok, "why": "방문 전환"},
        {"key": "feed", "label": "소식 최근 발행",
         "value": ", ".join(feed) if feed else "확인 실패", "ok": bool(feed),
         "why": "자주 관리되는 업체에 가점(업계 통설)"},
    ]
    done = sum(1 for c in checks if c["ok"])
    return {
        "checkedAt": datetime.now().isoformat(timespec="seconds"),
        "placeId": raw.get("placeId"),
        "source": raw.get("source"),
        "name": b.get("name") or "",
        "category": b.get("category") or "",
        "score": {"done": done, "total": len(checks)},
        "stats": {
            "rating": b.get("visitorReviewsScore"),
            "visitorReviews": b.get("visitorReviewsTotal"),
            "blogReviews": b.get("cafeBlogReviewsTotal"),
        },
        "status": (raw.get("status") or {}).get("description") or "",
        "hours": hours,
        "menus": [m.get("name") for m in menus],
        "checks": checks,
        "todo": [c["label"] for c in checks if not c["ok"]],
    }


def audit(place_id: str | None = None, session=None) -> dict:
    """수집 + 진단을 한 번에. place_id 를 안 주면 .env 의 NAVER_PLACE_ID."""
    pid = (place_id or os.getenv("NAVER_PLACE_ID", "")).strip()
    if not pid:
        raise ValueError("NAVER_PLACE_ID 가 없습니다(.env 확인)")
    return diagnose(collect(pid, session=session))
