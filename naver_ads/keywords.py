"""SPEC 7-6: 키워드 리서치 — /keywordstool 연관 키워드 + 월간 검색수 → data/keyword_research_YYYYMMDD.csv

    python -m naver_ads.keywords                      # SPEC 2-3 시드 10개
    python -m naver_ads.keywords 송도베이글 송도카페    # 직접 지정

플레이스 광고는 키워드를 입찰하지 않는다. 이 결과의 쓸모는 "송도 상권에서 실제 검색되는 표현"을
찾아 스마트플레이스 업체정보·메뉴명·소개글·광고 소재 문구에 심는 것이다.
⚠ 호출 제한이 빡빡하다 — 한 번에 5개, 호출 사이 1.5초. 응답 숫자는 문자열이고 10 미만은 '< 10'.
"""

from __future__ import annotations

import csv
import re
import sys
import time
from datetime import date

from naver_ads import db
from naver_ads.client import NaverAdsClient, NaverAdsError

SEEDS = ["송도 베이글", "송도 카페", "송도 브런치", "송도동 카페", "센트럴파크 카페",
         "타임스페이스 카페", "인천 베이글", "크림치즈 베이글", "송도 샌드위치", "송도 커피"]
COLUMNS = ["keyword", "monthlyPcQcCnt", "monthlyMobileQcCnt", "compIdx",
           "monthlyAveragePcClkCnt", "monthlyAveragePcCtr"]
MAX_HINTS, PAUSE = 5, 1.5


def clean(k: str) -> str:
    return re.sub(r"\s+", "", k or "")


def count(v) -> int:
    """'1,230' → 1230, '< 10' → 5(1~9 중앙값), 없음 → 0."""
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if s.startswith("<"):
        return 5
    m = re.search(r"[\d,]+", s)
    return int(m.group().replace(",", "")) if m else 0


def research(client: NaverAdsClient, seeds: list[str]) -> list[dict]:
    hints = [clean(s) for s in seeds if clean(s)]
    seen, rows = set(), []
    for i in range(0, len(hints), MAX_HINTS):
        chunk = hints[i:i + MAX_HINTS]
        payload = client.get("/keywordstool", hintKeywords=",".join(chunk), showDetail="1")
        db.save_raw(f"keywordstool_{i // MAX_HINTS}", payload)
        for r in payload.get("keywordList") or []:
            kw = r.get("relKeyword") or ""
            if not kw or kw in seen:
                continue
            seen.add(kw)
            rows.append({"keyword": kw, **{c: r.get(c) for c in COLUMNS[1:]},
                         "_total": count(r.get("monthlyPcQcCnt")) + count(r.get("monthlyMobileQcCnt")),
                         "_seed": kw in hints})
        if i + MAX_HINTS < len(hints):
            time.sleep(PAUSE)
    rows.sort(key=lambda r: -r["_total"])
    return rows


def write_csv(rows: list[dict]) -> str:
    out = db.DATA_DIR / f"keyword_research_{date.today():%Y%m%d}.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    return str(out)


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    seeds = [a for a in (argv if argv is not None else sys.argv[1:]) if a.strip()] or SEEDS
    client = NaverAdsClient()
    try:
        rows = research(client, seeds)
    except NaverAdsError as e:
        print(f"실패: {e}")
        return 1
    finally:
        client.close()
    path = write_csv(rows)
    print(f"연관 키워드 {len(rows)}개 → {path}")
    print(f"{'키워드':<22}{'PC':>8}{'모바일':>8}{'합계':>8}  경쟁")
    for r in rows[:40]:
        mark = "★" if r["_seed"] else " "
        print(f"{mark}{r['keyword']:<21}{count(r['monthlyPcQcCnt']):>8}{count(r['monthlyMobileQcCnt']):>8}{r['_total']:>8}  {r['compIdx']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
