"""네이버 검색광고 키워드도구 — 월간 검색수(수요의 크기)를 받아온다.

왜 필요한가(시장조사 검토 2026-09-04):
    지금 판정은 '이 말이 검색되나(자동완성 있음/없음)'와 '경쟁이 센가'만 본다.
    **얼마나 검색되나**를 몰라서, 경쟁 적은 순으로만 줄을 세우면 아무도 안 찾는
    말을 1등으로 밀어올린다. 이 모듈이 그 구멍을 메운다.

    무료다. 광고를 집행하지 않아도 광고주 계정만 있으면 쓸 수 있다
    (네이버 공식: "검색광고 회원이면 누구나 API 서비스를 사용할 수 있습니다").
    발급 절차는 docs/naver_searchad_setup.md.

    py -m sns_automation.keyword_volume 송도베이글 베이글산도

⚠️ 호출 제한이 빡빡하다. 네이버 공지: 키워드도구는 다른 기능보다 호출 속도가
1/5~1/6 수준이고 IP 기준으로도 걸린다. 429 가 나면 5~6배 긴 시간 쉬라고
안내한다. 우리는 주 1회 몇 번만 부르므로 문제 없지만, 루프로 두드리지 말 것.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import time
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

BASE = "https://api.searchad.naver.com"
URI = "/keywordstool"
MAX_HINTS = 5               # 공식: hintKeywords 는 한 번에 최대 5개
PAUSE = 1.5                 # 호출 사이 쉼(초) — 제한이 빡빡하다
TIMEOUT = 20

#: 월간 검색수가 10 미만이면 네이버가 숫자 대신 '< 10' 을 준다.
#: 1~9 의 중앙값으로 5 를 쓴다 — 0 으로 치면 '수요 없음'과 구분이 안 된다.
UNDER10 = 5

_NUM_RE = re.compile(r"[\d,]+")


class VolumeError(RuntimeError):
    """사람이 읽고 조치할 수 있는 실패 메시지."""


def configured() -> bool:
    """키 3개가 다 있나. 없으면 검색량 없이 지금처럼 동작한다."""
    return all(os.getenv(k, "").strip() for k in
               ("NAVER_AD_API_KEY", "NAVER_AD_SECRET_KEY", "NAVER_AD_CUSTOMER_ID"))


# ── 순수 함수(테스트 대상) ────────────────────────────────────

def sign(secret: str, timestamp: str, method: str, uri: str) -> str:
    """X-Signature — base64( HMAC-SHA256(비밀키, "타임스탬프.메서드.경로") ).

    ⚠️ 서명에 넣는 uri 는 **경로만**이다. 쿼리스트링은 넣지 않는다
    (공식 파이썬 샘플과 동일). 여기서 틀리면 403 이 난다.
    """
    msg = f"{timestamp}.{method}.{uri}"
    digest = hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def parse_count(value) -> int:
    """'1,230' → 1230, '< 10' → 5, None/'' → 0.

    응답 필드는 스펙상 전부 문자열이다. 숫자로 바로 계산하면 터진다.
    """
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if not s:
        return 0
    if "<" in s:
        return UNDER10
    m = _NUM_RE.search(s)
    return int(m.group(0).replace(",", "")) if m else 0


def clean_keyword(keyword: str) -> str:
    """키워드도구는 공백 없는 형태를 쓴다 — '송도 베이글' → '송도베이글'."""
    return re.sub(r"\s+", "", (keyword or "")).strip()


def to_rows(payload: dict) -> list[dict]:
    """응답 → [{keyword, pc, mobile, total, comp}] (검색수 많은 순)."""
    out = []
    for r in (payload or {}).get("keywordList") or []:
        pc = parse_count(r.get("monthlyPcQcCnt"))
        mo = parse_count(r.get("monthlyMobileQcCnt"))
        out.append({
            "keyword": r.get("relKeyword") or "",
            "pc": pc, "mobile": mo, "total": pc + mo,
            "comp": r.get("compIdx") or "",
        })
    out.sort(key=lambda r: -r["total"])
    return out


# ── 호출 ─────────────────────────────────────────────────────

def _call(params: dict) -> dict:
    import json
    api_key = os.getenv("NAVER_AD_API_KEY", "").strip()
    secret = os.getenv("NAVER_AD_SECRET_KEY", "").strip()
    customer = os.getenv("NAVER_AD_CUSTOMER_ID", "").strip()
    if not (api_key and secret and customer):
        raise VolumeError("네이버 검색광고 키가 .env 에 없어요 "
                          "(NAVER_AD_API_KEY / NAVER_AD_SECRET_KEY / NAVER_AD_CUSTOMER_ID).")
    ts = str(int(time.time() * 1000))
    url = f"{BASE}{URI}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "X-Timestamp": ts,
        "X-API-KEY": api_key,
        "X-Customer": customer,
        "X-Signature": sign(secret, ts, "GET", URI),
        "Content-Type": "application/json; charset=UTF-8",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        if e.code == 429:
            raise VolumeError("호출이 너무 잦아요(429) — 몇 분 뒤 다시. "
                              "키워드도구는 제한이 빡빡합니다.") from e
        if e.code in (401, 403):
            raise VolumeError(f"인증이 거절됐어요({e.code}). 키·고객ID를 확인하세요. {body}") from e
        raise VolumeError(f"검색광고 API 오류({e.code}): {body}") from e
    except Exception as e:  # noqa: BLE001
        raise VolumeError(f"검색광고 API 연결 실패: {str(e)[:150]}") from e


def lookup(keywords: list[str], *, related: bool = False) -> list[dict]:
    """키워드들의 월간 검색수. related=True 면 연관키워드도 함께 돌려준다.

    한 번에 5개까지라 알아서 나눠 부른다. 요청한 말만 필요하면 related=False.
    """
    want = [clean_keyword(k) for k in keywords if clean_keyword(k)]
    if not want:
        return []
    rows: list[dict] = []
    for i in range(0, len(want), MAX_HINTS):
        chunk = want[i:i + MAX_HINTS]
        rows += to_rows(_call({"hintKeywords": ",".join(chunk), "showDetail": "1"}))
        if i + MAX_HINTS < len(want):
            time.sleep(PAUSE)
    if related:
        return rows
    asked = {k for k in want}
    return [r for r in rows if clean_keyword(r["keyword"]) in asked]


def volume_map(keywords: list[str]) -> dict[str, int]:
    """{키워드(공백 제거): 월간 검색수} — 판정 규칙이 쓰는 형태."""
    return {clean_keyword(r["keyword"]): r["total"] for r in lookup(keywords)}


def main() -> int:
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = [a for a in sys.argv[1:] if a.strip()]
    if not args:
        print("사용법: py -m sns_automation.keyword_volume 키워드1 키워드2 ...")
        return 1
    try:
        rows = lookup(args, related=True)
    except VolumeError as e:
        print("실패:", e)
        return 1
    print(f"{'키워드':<24}{'PC':>8}{'모바일':>9}{'합계':>9}  경쟁")
    for r in rows[:30]:
        print(f"{r['keyword']:<24}{r['pc']:>8,}{r['mobile']:>9,}{r['total']:>9,}  {r['comp']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
