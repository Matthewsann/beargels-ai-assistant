"""플레이스 주간 유입 × 매장 매출 백필 (목표 3단계 '매출 상승').

일꾼은 매주 한 주씩만 쌓는다. 처음에는 비교할 과거가 없어서 "노출이 매출로
이어졌나"를 볼 수 없는데, 유입 통계 API 는 **과거 기간도 조회된다.**
그래서 이 스크립트로 지난 N주를 한 번에 채워 넣는다.

실행:
    python -m scripts.place_backfill          # 최근 13주
    python -m scripts.place_backfill 20       # 최근 20주

네이버 로그인이 된 크롬(scripts/launch_chrome.bat)이 필요하다. 읽기 전용.
⚠️ 로그인할 때 '로그인 상태 유지'를 켜야 크롬 재시작 후에도 세션이 남는다.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from crawler import place_stats as PS  # noqa: E402
from crawler.browser import BrowserSession  # noqa: E402
from database import mkt_store, supabase_client as db  # noqa: E402


def backfill(weeks: int = 13) -> list[dict]:
    periods = PS.week_starts(weeks)
    print(f"수집 주: {periods[0][0]} ~ {periods[-1][1]} ({len(periods)}주)")

    with BrowserSession() as sess:
        site, token = PS.prepare(sess)
        print("siteId:", site)
        series = PS.collect_series(sess, site, token, periods)
    print("유입 수집:", len(series), "주")

    # 매장 매출만 붙인다 — 배달은 플레이스와 무관하게 움직인다.
    totals = mkt_store.totals_by_date(
        mkt_store.sales_between(periods[0][0], periods[-1][1]))
    for w in series:
        w["storeSales"] = mkt_store.store_only_sum(totals, w["start"], w["end"])

    print(f"\n{'주간':<27}{'지도유입':>8}{'전체유입':>8}{'매장매출':>14}")
    for w in series:
        ss = w["storeSales"]
        amt = f"{ss['amount']:,}원" if ss["days"] else "장부 미반영"
        print(f"{w['period']:<27}{str(w['mapPv']):>8}{str(w['total']):>8}{amt:>14}")

    db.menu_set_setting("place_weekly", series)
    print(f"\n저장 완료 — 웹 /place 에서 보입니다({len(series)}주).")
    return series


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 13
    backfill(n)
