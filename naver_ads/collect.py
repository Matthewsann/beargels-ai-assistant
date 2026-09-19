"""SPEC 7-3/7-4: /stats 일별 성과 수집 → daily_stats UPSERT (멱등).

    python -m naver_ads.collect                       # 최근 7일(어제까지)
    python -m naver_ads.collect --since 2025-11-30    # 백필(어제까지, 92일씩 쪼개서)
    python -m naver_ads.collect --since 2026-09-01 --until 2026-09-19

규칙:
- 당일은 확정 전이라 넣지 않는다. --until 이 오늘 이후면 어제로 내린다.
- /stats 는 한 번에 92일까지(실측 2026-09-20, code 11004). 그 안으로 쪼개 순차 호출.
- 호출 사이에 PAUSE 초 쉰다(429 대비). 429 자체는 client 가 3회 백오프.
- 구조(캠페인/광고그룹/소재)를 먼저 동기화해 엔티티 목록을 DB 에서 읽는다.
- 비즈머니 잔액을 함께 찍고 임계치 아래면 경고(SPEC 3-4).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, timedelta

from naver_ads import db
from naver_ads.client import NaverAdsClient, NaverAdsError
from naver_ads.structure import sync_structure

logger = logging.getLogger(__name__)

FIELDS = ["impCnt", "clkCnt", "salesAmt", "ctr", "cpc", "avgRnk", "ccnt"]
MAX_DAYS = 92               # 네이버 제한(11004). 이 안으로 쪼갠다
PAUSE = 0.7                 # 호출 간격(초)
BIZMONEY_WARN = 6000        # 일 예산 3,000원 × 2일치 아래면 경고

ENTITY_TABLES = (("campaign", "campaigns"), ("adgroup", "adgroups"), ("ad", "ads"))


def yesterday() -> date:
    return date.today() - timedelta(days=1)


def windows(since: date, until: date, size: int = MAX_DAYS):
    """[since, until] 을 size 일 이하 구간으로 자른다."""
    cur = since
    while cur <= until:
        end = min(cur + timedelta(days=size - 1), until)
        yield cur, end
        cur = end + timedelta(days=1)


def fetch_daily(client: NaverAdsClient, entity_id: str, since: date, until: date) -> list[dict]:
    r = client.get("/stats", id=entity_id, fields=json.dumps(FIELDS),
                   timeRange=json.dumps({"since": since.isoformat(), "until": until.isoformat()}))
    return r.get("data", [])


def collect(client: NaverAdsClient, con, since: date, until: date) -> dict:
    until = min(until, yesterday())
    if since > until:
        return {"rows": 0, "calls": 0, "note": "수집할 날짜 없음"}
    sync_structure(client, con)
    entities = [(etype, r["id"]) for etype, table in ENTITY_TABLES
                for r in con.execute(f"SELECT id FROM {table}")]

    rows, calls, fetched = [], 0, db.now_iso()
    for w_since, w_until in windows(since, until):
        for etype, eid in entities:
            data = fetch_daily(client, eid, w_since, w_until)
            calls += 1
            db.save_raw(f"stats_{etype}_{eid}_{w_since}_{w_until}", data)
            for d in data:
                day = d["dateStart"]
                if day > until.isoformat():          # 당일·미래는 절대 넣지 않는다
                    continue
                rows.append({
                    "stat_date": day, "entity_type": etype, "entity_id": eid,
                    "imp_cnt": d.get("impCnt"), "clk_cnt": d.get("clkCnt"), "cost": d.get("salesAmt"),
                    "ctr": d.get("ctr"), "cpc": d.get("cpc"), "avg_rnk": d.get("avgRnk"),
                    "ccnt": d.get("ccnt"), "fetched_at": fetched,
                })
            time.sleep(PAUSE)
    db.upsert(con, "daily_stats", rows, key=("stat_date", "entity_type", "entity_id"))

    bm = client.get("/billing/bizmoney")
    balance = bm.get("bizmoney")
    db.upsert(con, "bizmoney", [{"snap_date": date.today().isoformat(), "balance": balance,
                                 "fetched_at": fetched}], key=("snap_date",))
    con.commit()
    out = {"since": since.isoformat(), "until": until.isoformat(), "entities": len(entities),
           "calls": calls, "rows": len(rows), "bizmoney": balance}
    if balance is not None and balance < BIZMONEY_WARN:
        out["warning"] = f"비즈머니 잔액 {balance:,}원 — 임계치 {BIZMONEY_WARN:,}원 아래. 충전 안 하면 광고가 멈춘다"
        logger.warning(out["warning"])
    return out


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", type=date.fromisoformat, default=None, help="기본: until 기준 7일 전")
    ap.add_argument("--until", type=date.fromisoformat, default=None, help="기본: 어제(당일 제외)")
    a = ap.parse_args(argv)
    until = a.until or yesterday()
    since = a.since or until - timedelta(days=6)

    client = NaverAdsClient()
    con = db.connect()
    try:
        summary = collect(client, con, since, until)
    except NaverAdsError as e:
        print(f"실패: {e}")
        return 1
    finally:
        client.close()
        con.close()
    print("수집 완료:", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
