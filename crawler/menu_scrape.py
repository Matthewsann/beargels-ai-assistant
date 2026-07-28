"""채널별 '노출 중인 메뉴' 수집 — 배민·쿠팡(사장님 포털) + 네이버플레이스(공개).

리뷰 크롤러와 같은 세션(BrowserSession)을 쓴다. 메뉴 관리 화면의 API 응답
스키마는 플랫폼이 수시로 바꾸므로, 특정 스키마에 기대지 않고 **메뉴 화면에서
오간 JSON 응답을 전부 모아 이름+가격 꼴의 객체를 재귀적으로 찾아내는** 방식으로
버틴다. 실패하면 디버그 덤프를 남겨 새벽 자동 점검이 고칠 수 있게 한다.

네이버는 로그인 없이 공개 플레이스 페이지를 읽는다. .env 에
NAVER_PLACE_ID=<플레이스 숫자 ID> 가 있어야 한다(모바일 플레이스 주소
m.place.naver.com/restaurant/<ID>/... 의 숫자 부분).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

from crawler.browser import BrowserSession

logger = logging.getLogger(__name__)

DEBUG_DIR = Path(__file__).resolve().parent.parent / "debug"

# 배민 메뉴 화면은 /shops/<가게ID>/menupan — 가게ID는 홈 화면 링크에서 자동 감지.
BAEMIN_HOME_URL = "https://self.baemin.com/"
BAEMIN_MENU_URL_TPL = "https://self.baemin.com/shops/{shop}/menupan"
# 쿠팡 메뉴 화면 경로는 menu(단수). menus 로 가면 랜딩 페이지로 떨어진다(정찰 2026-07).
COUPANG_MENU_URL = "https://store.coupangeats.com/merchant/management/menu"

_NAME_KEYS = ("menuName", "menu_name", "name", "dishName", "itemName")
_PRICE_KEYS = ("price", "menuPrice", "salePrice", "sellingPrice", "amount")
# 배민처럼 가격이 하위 옵션 목록에 들어있는 경우 — 메뉴명은 바깥, 가격은 안쪽.
# 이 키 안의 객체("기본" 등 옵션명)는 메뉴로 세지 않는다.
_PRICE_LIST_KEYS = ("menuPriceResponses", "menuPrices", "prices")


def _dump(tag: str, text: str) -> None:
    DEBUG_DIR.mkdir(exist_ok=True)
    p = DEBUG_DIR / f"menu_{tag}_{int(time.time())}.txt"
    p.write_text(text[:500_000], encoding="utf-8")
    logger.info("디버그 덤프 저장: %s", p)


def _walk(node, found: list) -> None:
    """JSON 트리에서 이름+가격을 함께 가진 객체를 전부 수집."""
    if isinstance(node, dict):
        name = next((node[k] for k in _NAME_KEYS
                     if isinstance(node.get(k), str) and node[k].strip()), None)
        price = None
        for k in _PRICE_KEYS:
            v = node.get(k)
            if isinstance(v, (int, float)):
                price = v
                break
            # 네이버는 "3,500" 같은 문자열로 내려주기도 한다.
            if isinstance(v, str) and re.fullmatch(r"[\d,]{3,9}", v.strip()):
                price = int(v.replace(",", ""))
                break
        if name and price is None:
            # 가격이 하위 옵션 목록(menuPriceResponses 등)에 있는 구조.
            for lk in _PRICE_LIST_KEYS:
                for opt in (node.get(lk) or []):
                    if isinstance(opt, dict):
                        v = opt.get("price")
                        if isinstance(v, (int, float)) and v > 0:
                            price = v
                            break
                if price is not None:
                    break
        if name and price is not None and 100 <= price <= 200_000:
            found.append({"menu_name": name.strip(), "price": int(price),
                          "raw": {k: node.get(k) for k in
                                  (*_NAME_KEYS, *_PRICE_KEYS, "description",
                                   "status", "soldOut", "categoryName")
                                  if k in node}})
        for k, v in node.items():
            if k in _PRICE_LIST_KEYS:
                continue  # 옵션 목록 안으로는 안 들어간다("기본" 등 오검출 방지)
            _walk(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk(v, found)


def _dedupe(rows: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in rows:
        key = r["menu_name"]
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _capture_json(page, url: str, settle_seconds: float = 12.0) -> list[dict]:
    """메뉴 화면을 띄우고 오간 JSON 응답(메뉴 관련 URL)을 전부 모은다."""
    captured: list[dict] = []

    def on_response(resp):
        try:
            if "application/json" not in (resp.headers.get("content-type") or ""):
                return
            if not re.search(r"menu|dish|item", resp.url, re.I):
                return
            captured.append(resp.json())
        except Exception:  # noqa: BLE001 — 본문 없는 응답 등은 무시
            pass

    page.on("response", on_response)
    try:
        page.goto(url, wait_until="domcontentloaded")
        # SPA 가 메뉴 목록 API 를 다 부를 때까지 잠시 둔다.
        page.wait_for_timeout(int(settle_seconds * 1000))
    finally:
        page.remove_listener("response", on_response)
    return captured


def _capture_menus(page, url: str, tag: str,
                   settle_seconds: float = 12.0) -> list[dict]:
    """메뉴 화면에서 오간 JSON 응답을 재귀 탐색해 메뉴(이름+가격)를 긁는다."""
    captured = _capture_json(page, url, settle_seconds)
    found: list[dict] = []
    for payload in captured:
        _walk(payload, found)
    if not found:
        _dump(tag, page.content())
    return _dedupe(found)


def _parse_baemin_menupan(payloads: list[dict]) -> list[dict]:
    """배민 menupan API(data.menuGroups[].menus[]) 응답을 파싱한다.

    숨김(HIDE)·미노출(displayYn=False) 메뉴는 '노출 중'이 아니므로 제외.
    품절(SOLDOUT)은 노출은 되는 상태라 남기되 raw.status 로 표시한다.
    """
    rows: list[dict] = []
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        data = payload.get("data")
        groups = data.get("menuGroups") if isinstance(data, dict) else None
        if not isinstance(groups, list):
            continue
        for g in groups:
            for m in (g.get("menus") or []):
                if m.get("itemStatus") == "HIDE" or not m.get("displayYn", True):
                    continue
                name = (m.get("menuName") or "").strip()
                price = None
                for opt in (m.get("menuPriceResponses") or []):
                    v = opt.get("price")
                    if isinstance(v, (int, float)) and v > 0:
                        price = int(v)
                        break
                if not name or price is None:
                    continue
                rows.append({"menu_name": name, "price": price,
                             "raw": {"name": name, "price": price,
                                     "status": m.get("itemStatus"),
                                     "categoryName": g.get("menuGroupName"),
                                     "description": m.get("menuDesc") or None}})
        if rows:
            break  # menupan 응답 하나면 충분
    return _dedupe(rows)


def _scrape_portal(url: str, tag: str, settle_seconds: float = 12.0) -> list[dict]:
    with BrowserSession() as sess:
        return _capture_menus(sess.page, url, tag, settle_seconds)


def fetch_baemin_menus() -> list[dict]:
    with BrowserSession() as sess:
        page = sess.page
        # 홈 화면 LNB 링크에서 가게 ID를 찾는다 (/shops/<숫자>/...).
        page.goto(BAEMIN_HOME_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        m = re.search(r"/shops/(\d+)/", page.content())
        if not m:
            _dump("baemin_home", page.content())
            logger.warning("배민 가게 ID를 찾지 못함 — 로그인 상태 확인 필요")
            return []
        url = BAEMIN_MENU_URL_TPL.format(shop=m.group(1))
        captured = _capture_json(page, url)
        rows = _parse_baemin_menupan(captured)
        if not rows:
            # menupan 스키마가 바뀌었으면 예전 방식(재귀 탐색)으로 버틴다.
            found: list[dict] = []
            for payload in captured:
                _walk(payload, found)
            rows = _dedupe(found)
        if not rows:
            _dump("baemin", page.content())
    logger.info("배민 노출 메뉴 %d건", len(rows))
    return rows


def fetch_coupang_menus() -> list[dict]:
    rows = _scrape_portal(COUPANG_MENU_URL, "coupang")
    logger.info("쿠팡 노출 메뉴 %d건", len(rows))
    return rows


def fetch_naver_menus() -> list[dict]:
    """네이버플레이스 공개 메뉴 페이지(로그인 불필요)."""
    place_id = os.getenv("NAVER_PLACE_ID", "").strip()
    if not place_id:
        logger.warning("NAVER_PLACE_ID 미설정 — 네이버 메뉴 수집 건너뜀")
        return []
    url = f"https://m.place.naver.com/restaurant/{place_id}/menu/list"
    with BrowserSession() as sess:
        page = sess.page
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(6000)
        html = page.content()
        # 네이버 플레이스는 초기 상태를 window.__APOLLO_STATE__ 로 내려준다.
        m = re.search(r"__APOLLO_STATE__\s*=\s*({.*?});", html, re.S)
        found: list[dict] = []
        if m:
            try:
                _walk(json.loads(m.group(1)), found)
            except Exception:  # noqa: BLE001
                pass
        if not found:
            _dump("naver", html)
    rows = _dedupe(found)
    logger.info("네이버 노출 메뉴 %d건", len(rows))
    return rows
