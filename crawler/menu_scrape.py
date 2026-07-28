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

BAEMIN_MENU_URL = "https://self.baemin.com/menu"
COUPANG_MENU_URL = "https://store.coupangeats.com/merchant/management/menus"

_NAME_KEYS = ("menuName", "menu_name", "name", "dishName", "itemName")
_PRICE_KEYS = ("price", "menuPrice", "salePrice", "sellingPrice", "amount")


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
        if name and price is not None and 100 <= price <= 200_000:
            found.append({"menu_name": name.strip(), "price": int(price),
                          "raw": {k: node.get(k) for k in
                                  (*_NAME_KEYS, *_PRICE_KEYS, "description",
                                   "status", "soldOut", "categoryName")
                                  if k in node}})
        for v in node.values():
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


def _scrape_portal(url: str, tag: str, settle_seconds: float = 12.0) -> list[dict]:
    """메뉴 관리 화면을 열고, 오간 JSON 응답에서 메뉴(이름+가격)를 긁는다."""
    captured: list[dict] = []

    with BrowserSession() as sess:
        page = sess.page

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
        page.goto(url, wait_until="domcontentloaded")
        # SPA 가 메뉴 목록 API 를 다 부를 때까지 잠시 둔다.
        page.wait_for_timeout(int(settle_seconds * 1000))

        found: list[dict] = []
        for payload in captured:
            _walk(payload, found)
        if not found:
            _dump(tag, page.content())
    return _dedupe(found)


def fetch_baemin_menus() -> list[dict]:
    rows = _scrape_portal(BAEMIN_MENU_URL, "baemin")
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
