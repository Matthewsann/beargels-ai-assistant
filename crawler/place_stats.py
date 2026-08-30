"""스마트플레이스 통계 — 유입 수집 (목표 2단계 '노출 상승').

1단계('최적화', crawler/place_audit.py)는 세팅이 채워졌는지만 본다. 그건
공개 페이지로 되지만, **최적화가 실제로 노출을 움직였는지**는 스마트플레이스
센터 통계에서만 보이고 그 화면은 **네이버 로그인이 필요하다.**

데이터 출처(2026-08-30 실측으로 확인):
    GET /api/proxy/bizadvisor/api/v3/sites/{siteId}/report
        ?dimensions=<축>&metrics=pv&startDate=&endDate=&useIndex=<지수>
    · dimensions=mapped_channel_name → [{"mapped_channel_name":"네이버지도","pv":401.0}]
    · dimensions=ref_keyword         → [{"ref_keyword":"송도베이글","pv":9.0}]
    · dimensions=date_time           → [{"date_time":"2026-08-24","pv":114.0}]

`mapped_channel_name` 의 '네이버지도' 가 목표 문구("네이버지도 노출")에 그대로
대응하는 값이라 이걸 대표 지표로 쓴다.

siteId 는 플레이스 공개 정보의 `PlaceDetailBase.siteId` 와 같은 값이다
(베어글스 = sp_20ed8dbf80bb34). .env `NAVER_PLACE_SITE_ID` 로 고정할 수 있고,
없으면 통계 화면에서 찾아낸다.

로그인이 없으면 **조용히 빈 값을 돌려주지 않고 예외를 던진다** — "유입 0회"와
"로그인이 풀렸다"가 화면에서 같아 보이면 노출이 떨어진 걸로 오해한다.

읽기 전용이다 — 스마트플레이스에 아무것도 쓰지 않는다.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

BIZES_URL = "https://new.smartplace.naver.com/bizes"
REPORT_URL = ("https://new.smartplace.naver.com/api/proxy/bizadvisor"
              "/api/v3/sites/{site}/report")

# 로그인이 안 됐을 때 화면에 뜨는 문구들(둘 다 관측됨, 2026-08-30).
_LOGIN_MARKERS = ("네이버 로그인이 필요한 기능입니다",
                  "권한을 보유한 업체가 없습니다")

# 검색어가 안 붙는 유입(지도 목록에서 바로 누른 경우 등). 키워드로 세면 안 된다.
NO_KEYWORD = "(검색어 없음)"

# 목표 문구가 가리키는 채널 — 이게 오르는 게 2단계의 성공이다.
MAP_CHANNEL = "네이버지도"

# useIndex 값(2026-08-30 실제 요청에서 확인). 이름이 비슷해 헷갈리기 쉬운데
# 잘못 넣으면 401 이 아니라 500 이 온다 — 인증이 아니라 지수 이름 문제라는 뜻.
IDX_ALL = "revenue-all-channel-detail"
IDX_SEARCH = "revenue-search-channel-detail"


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


def week_range(today: date | None = None) -> tuple[str, str]:
    """직전 '완결된 주'(월~일)를 돌려준다.

    통계 화면도 주 단위(월-일)로 보여준다. 진행 중인 주를 섞으면 지난주와
    비교할 때 며칠치 대 일주일치를 비교하게 되어 늘 줄어든 것처럼 보인다.
    """
    today = today or date.today()
    last_sunday = today - timedelta(days=today.weekday() + 1)
    return (last_sunday - timedelta(days=6)).isoformat(), last_sunday.isoformat()


def parse_rows(payload, dim: str) -> list[dict]:
    """[{<dim>: 이름, "pv": 수}] → [{"name":…, "count":…}] (순수 로직).

    pv 는 실수로 온다(21.0). 화면에는 정수로 보여준다.
    """
    out = []
    for row in payload or []:
        if not isinstance(row, dict):
            continue
        name = row.get(dim)
        pv = row.get("pv")
        if not isinstance(name, str) or not isinstance(pv, (int, float)):
            continue
        out.append({"name": name.strip(), "count": int(pv)})
    out.sort(key=lambda r: -r["count"])
    return out


def summarize(channels, keywords, daily, previous=None,
              period=None) -> dict:
    """수집분 → 저장·화면용. previous 가 있으면 **변화**까지 계산한다.

    목표 2단계의 질문은 "지금 몇 등이냐"가 아니라 "최적화 뒤에 늘었나"다.
    """
    prev = previous or {}
    prev_kw = {r["name"]: r["count"] for r in (prev.get("keywords") or [])}

    kw = []
    for r in keywords:
        if r["name"] == NO_KEYWORD:      # 검색어 없는 유입은 키워드가 아니다
            continue
        d = dict(r)
        d["delta"] = (r["count"] - prev_kw[r["name"]]
                      if r["name"] in prev_kw else None)
        kw.append(d)

    total = sum(r["count"] for r in daily) if daily else \
        sum(r["count"] for r in channels)
    map_pv = next((r["count"] for r in channels if r["name"] == MAP_CHANNEL), None)

    prev_total = prev.get("total")
    prev_map = prev.get("mapPv")
    return {
        "checkedAt": datetime.now().isoformat(timespec="seconds"),
        "period": period,
        "total": total,
        "totalDelta": (total - prev_total) if isinstance(prev_total, int) else None,
        "mapPv": map_pv,
        "mapDelta": (map_pv - prev_map)
                    if isinstance(map_pv, int) and isinstance(prev_map, int) else None,
        "channels": channels,
        "keywords": kw,
        "prevAt": prev.get("checkedAt"),
        "prevPeriod": prev.get("period"),
    }


def _report(page, site: str, dim: str, start: str, end: str, index: str,
            token: str = "") -> list:
    """통계 API 를 **페이지 안에서** 호출한다.

    쿠키만으로는 401 이다 — 이 API 는 Bearer 토큰을 함께 요구한다(2026-08-30
    확인). 토큰은 통계 화면이 스스로 보내는 요청에서 받아 그대로 재사용한다
    (쿠팡 크롤러가 Akamai 를 통과하는 것과 같은 원리: 페이지 자신의 자격을 쓴다).
    """
    url = (f"{REPORT_URL.format(site=site)}?dimensions={dim}&metrics=pv"
           f"&startDate={start}&endDate={end}&useIndex={index}")
    if dim in ("ref_keyword", "mapped_channel_name"):
        url += "&sort=pv"
    return page.evaluate(
        """async ([u, tok]) => {
             const h = {'accept': 'application/json, text/plain, */*'};
             if (tok) h['authorization'] = tok;
             const r = await fetch(u, {credentials: 'include', headers: h});
             if (!r.ok) return {__error: r.status};
             return await r.json();
           }""", [url, token])


def find_site_id(page) -> str | None:
    """통계에 쓰는 siteId(sp_...)를 찾는다.

    ① .env NAVER_PLACE_SITE_ID → ② 지금 화면 → ③ 공개 플레이스 페이지.
    ③이 필요한 이유: 통계 화면은 Next.js SPA 라 siteId 가 HTML 에 안 남는다.
    같은 값이 공개 플레이스의 `PlaceDetailBase.siteId` 에 들어 있다.
    """
    env = os.getenv("NAVER_PLACE_SITE_ID", "").strip()
    if env:
        return env
    m = re.search(r"sp_[0-9a-f]{10,}", page.content())
    if m:
        return m.group(0)

    place_id = os.getenv("NAVER_PLACE_ID", "").strip()
    if not place_id:
        return None
    try:
        page.goto(f"https://m.place.naver.com/restaurant/{place_id}/home",
                  wait_until="domcontentloaded")
        page.wait_for_timeout(6000)
        m = re.search(r'"siteId"\s*:\s*"(sp_[0-9a-f]+)"', page.content())
        return m.group(1) if m else None
    except Exception:  # noqa: BLE001
        return None


def collect(session=None, previous=None, today: date | None = None) -> dict:
    """지난주 유입(채널·키워드·일별)을 수집해 정규화한다.

    로그인이 없으면 NaverLoginRequired 를 던진다.
    """
    from crawler.browser import BrowserSession

    start, end = week_range(today)
    own = session is None
    sess = session or BrowserSession()
    tokens: list[str] = []
    try:
        page = sess.__enter__().page if own else sess.page

        def _grab_token(req):
            """통계 화면이 스스로 보내는 요청에서 Bearer 토큰을 빌린다."""
            if "bizadvisor" not in req.url:
                return
            auth = (req.headers or {}).get("authorization")
            if auth and auth not in tokens:
                tokens.append(auth)

        page.on("request", _grab_token)
        page.goto(BIZES_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(6000)
        if not is_logged_in(page):
            raise NaverLoginRequired(
                "크롤러 크롬에 네이버 로그인이 없습니다 — "
                "scripts/launch_chrome.bat 로 띄운 창에서 네이버에 한 번 "
                "로그인해 주세요(세션은 프로필에 남습니다).")

        biz = None
        m = re.search(r"/bizes/place/(\d+)", page.content())
        if m:
            biz = m.group(1)
        if biz:
            page.goto(f"https://new.smartplace.naver.com/bizes/place/{biz}/statistics",
                      wait_until="domcontentloaded")
            page.wait_for_timeout(10000)

        site = find_site_id(page)
        if not site:
            raise RuntimeError(
                "통계 siteId(sp_...)를 찾지 못했습니다 — "
                ".env 에 NAVER_PLACE_SITE_ID 를 넣어 고정할 수 있습니다.")

        # find_site_id 가 공개 플레이스 페이지까지 다녀왔을 수 있다. 통계 API 는
        # 같은 출처(new.smartplace)에서 불러야 하므로 반드시 돌아온 뒤 호출한다.
        if "new.smartplace.naver.com" not in (page.url or ""):
            page.goto(
                f"https://new.smartplace.naver.com/bizes/place/{biz}/statistics"
                if biz else BIZES_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(5000)

        token = tokens[-1] if tokens else ""
        if not token:
            raise RuntimeError(
                "통계 인증 토큰을 못 받았습니다 — 통계 화면이 안 열렸을 수 있습니다.")
        ch = _report(page, site, "mapped_channel_name", start, end,
                     IDX_ALL, token)
        kw = _report(page, site, "ref_keyword", start, end,
                     IDX_SEARCH, token)
        dt = _report(page, site, "date_time", start, end,
                     IDX_ALL, token)
    finally:
        if own:
            sess.__exit__(None, None, None)

    for name, payload in (("채널", ch), ("키워드", kw), ("일별", dt)):
        if isinstance(payload, dict) and payload.get("__error"):
            raise RuntimeError(f"{name} 통계 요청 실패(HTTP {payload['__error']})")

    channels = parse_rows(ch, "mapped_channel_name")
    keywords = parse_rows(kw, "ref_keyword")
    daily = parse_rows(dt, "date_time")
    if not (channels or keywords):
        raise RuntimeError("통계가 비어 있습니다(화면 개편 또는 권한 문제)")

    return summarize(channels, keywords, daily, previous,
                     period=f"{start} ~ {end}")
