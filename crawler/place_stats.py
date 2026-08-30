"""스마트플레이스 통계 — 유입 키워드 수집 (목표 2단계 '노출 상승').

1단계('최적화', crawler/place_audit.py)는 세팅이 채워졌는지만 본다. 그건
공개 페이지로 되지만, **최적화가 실제로 노출을 움직였는지**는 스마트플레이스
센터의 통계 화면에서만 보이고 그 화면은 **네이버 로그인이 필요하다.**

그래서 이 모듈은 로그인된 크롬(BROWSER_MODE=attach, scripts/launch_chrome.bat)
세션을 쓴다. 로그인이 없으면 **조용히 빈 값을 돌려주지 않고 예외를 던진다** —
"수집됐는데 키워드가 0개"와 "로그인이 풀렸다"가 화면에서 같아 보이면 안 된다.

읽기 전용이다 — 스마트플레이스에 아무것도 쓰지 않는다.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

BIZES_URL = "https://new.smartplace.naver.com/bizes"

# 로그인이 안 됐을 때 화면에 뜨는 문구들(둘 다 관측됨, 2026-08-30).
_LOGIN_MARKERS = ("네이버 로그인이 필요한 기능입니다",
                  "권한을 보유한 업체가 없습니다")


class NaverLoginRequired(RuntimeError):
    """크롤러 크롬에 네이버 로그인이 안 돼 있음 — 사람이 한 번 로그인해야 한다."""


def is_logged_in(page) -> bool:
    """지금 이 페이지가 '로그인된 사장님 화면'인지."""
    try:
        txt = page.inner_text("body")
    except Exception:  # noqa: BLE001
        return False
    if any(m in txt for m in _LOGIN_MARKERS):
        return False
    return "nid.naver.com" not in (page.url or "")


def find_biz_id(page) -> str | None:
    """내 업체 목록에서 스마트플레이스 업체 ID 를 찾는다."""
    html = page.content()
    ids = re.findall(r"/bizes/place/(\d+)", html) or re.findall(r"/bizes/(\d+)", html)
    return ids[0] if ids else None


def parse_keywords(payload) -> list[dict]:
    """통계 응답(JSON) → [{keyword, count}] 정규화. 순수 로직.

    네이버가 키 이름을 바꿔도 견디도록, 정확한 경로 대신 **모양**으로 찾는다:
    "문자열 키워드 + 숫자 카운트"를 가진 dict 들의 목록이면 채택한다.
    (통계 API 응답 스키마가 공개돼 있지 않고 개편이 잦아, 경로 고정은 잘 깨진다.)
    """
    found: list[dict] = []

    kw_keys = ("keyword", "key", "query", "searchKeyword", "name", "label")
    cnt_keys = ("count", "cnt", "value", "inflow", "pv", "total", "hits")

    def walk(node):
        if isinstance(node, list):
            for it in node:
                walk(it)
            return
        if not isinstance(node, dict):
            return
        kw = next((node[k] for k in kw_keys
                   if isinstance(node.get(k), str) and node[k].strip()), None)
        cnt = next((node[k] for k in cnt_keys
                    if isinstance(node.get(k), (int, float))), None)
        if kw and cnt is not None:
            found.append({"keyword": kw.strip(), "count": int(cnt)})
        for v in node.values():
            walk(v)

    walk(payload)

    # 같은 키워드가 여러 번 잡히면 가장 큰 값 하나로.
    best: dict[str, int] = {}
    for row in found:
        k = row["keyword"]
        best[k] = max(best.get(k, 0), row["count"])
    rows = [{"keyword": k, "count": v} for k, v in best.items()]
    rows.sort(key=lambda r: -r["count"])
    return rows


def summarize(rows: list[dict], previous: dict | None = None) -> dict:
    """키워드 목록 → 저장·화면용. previous 가 있으면 **변화**까지 계산한다.

    목표 2단계는 "최적화가 노출을 움직였나"라서, 지금 순위보다 **지난번 대비
    변화**가 핵심이다.
    """
    prev_map = {r["keyword"]: r["count"]
                for r in ((previous or {}).get("keywords") or [])}
    out = []
    for r in rows:
        d = dict(r)
        if r["keyword"] in prev_map:
            d["delta"] = r["count"] - prev_map[r["keyword"]]
        else:
            d["delta"] = None          # 이번에 새로 등장
        out.append(d)
    return {
        "checkedAt": datetime.now().isoformat(timespec="seconds"),
        "keywords": out,
        "total": sum(r["count"] for r in rows),
        "prevAt": (previous or {}).get("checkedAt"),
    }


def collect(session=None, previous=None) -> dict:
    """통계 화면에서 유입 키워드를 긁어 정규화한다.

    로그인이 없으면 NaverLoginRequired 를 던진다.
    """
    from crawler.browser import BrowserSession

    own = session is None
    sess = session or BrowserSession()
    payloads: list = []
    try:
        page = sess.__enter__().page if own else sess.page

        def _grab(resp):
            u = resp.url
            if not any(k in u for k in ("statistic", "report", "keyword", "inflow")):
                return
            try:
                payloads.append(resp.json())
            except Exception:  # noqa: BLE001 — JSON 아닌 응답은 그냥 무시
                pass

        page.on("response", _grab)
        page.goto(BIZES_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(6000)
        if not is_logged_in(page):
            raise NaverLoginRequired(
                "크롤러 크롬에 네이버 로그인이 없습니다 — "
                "scripts/launch_chrome.bat 로 띄운 창에서 네이버에 한 번 "
                "로그인해 주세요(세션은 프로필에 남습니다).")

        biz = find_biz_id(page)
        if not biz:
            raise RuntimeError("스마트플레이스 업체를 찾지 못했습니다(권한 확인 필요)")

        page.goto(f"https://new.smartplace.naver.com/bizes/place/{biz}/statistic",
                  wait_until="domcontentloaded")
        page.wait_for_timeout(9000)
    finally:
        if own:
            sess.__exit__(None, None, None)

    rows: list[dict] = []
    for pl in payloads:
        rows = parse_keywords(pl)
        if rows:
            break
    if not rows:
        # 모양이 바뀌었으면 원자료를 남겨 다음 사람이 볼 수 있게 한다.
        raise RuntimeError(
            "통계에서 유입 키워드를 찾지 못했습니다(화면 개편 가능성). "
            f"응답 {len(payloads)}건 수집됨: "
            f"{json.dumps(payloads[:1], ensure_ascii=False)[:400]}")
    return summarize(rows, previous)
