"""SPEC 7-2: 캠페인/광고그룹/소재 구조를 받아 SQLite 에 UPSERT (성과 ID 조인용 마스터).

    python -m naver_ads.structure

PLACE 캠페인은 키워드를 직접 등록하지 않으므로 /ncc/adkeywords 를 부르지 않는다(SPEC 3-3).
"""

from __future__ import annotations

import logging
import sys

from naver_ads import db
from naver_ads.client import NaverAdsClient, NaverAdsError

logger = logging.getLogger(__name__)


def sync_structure(client: NaverAdsClient, con) -> dict:
    """구조 3단을 받아 저장한다. 돌려주는 건 개수 요약."""
    fetched = db.now_iso()
    campaigns = client.get("/ncc/campaigns")
    db.save_raw("campaigns", campaigns)
    db.upsert(con, "campaigns", [{
        "id": c["nccCampaignId"], "name": c.get("name"), "campaign_type": c.get("campaignTp"),
        "status": c.get("status"), "daily_budget": c.get("dailyBudget"),
        "reg_tm": c.get("regTm"), "fetched_at": fetched,
    } for c in campaigns], key=("id",))

    n_groups = n_ads = n_kw = 0
    skipped_kw = []
    for c in campaigns:
        cid = c["nccCampaignId"]
        groups = client.get("/ncc/adgroups", nccCampaignId=cid)
        db.save_raw(f"adgroups_{cid}", groups)
        n_groups += db.upsert(con, "adgroups", [{
            "id": g["nccAdgroupId"], "campaign_id": cid, "name": g.get("name"),
            "bid_amt": g.get("bidAmt"), "status": g.get("status"),
            "daily_budget": g.get("dailyBudget"), "reg_tm": g.get("regTm"), "fetched_at": fetched,
        } for g in groups], key=("id",))

        for g in groups:
            gid = g["nccAdgroupId"]
            ads = client.get("/ncc/ads", nccAdgroupId=gid)
            db.save_raw(f"ads_{gid}", ads)
            n_ads += db.upsert(con, "ads", [{
                "id": a["nccAdId"], "adgroup_id": gid, "status": a.get("status"),
                "inspect_status": a.get("inspectStatus"), "fetched_at": fetched,
            } for a in ads], key=("id",))

            if c.get("campaignTp") == "PLACE":
                skipped_kw.append(gid)          # 키워드 등록 유형이 아니다 — 건너뛴다
                continue
            try:
                kws = client.get("/ncc/adkeywords", nccAdgroupId=gid)
            except NaverAdsError as e:          # 404 도 정상으로 본다(SPEC 3-3)
                if e.status == 404:
                    kws = []
                else:
                    raise
            db.save_raw(f"adkeywords_{gid}", kws)
            n_kw += len(kws)
    con.commit()
    return {"campaigns": len(campaigns), "adgroups": n_groups, "ads": n_ads,
            "adkeywords_raw": n_kw, "adkeywords_skipped_place": len(skipped_kw)}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    client = NaverAdsClient()
    con = db.connect()
    try:
        summary = sync_structure(client, con)
    except NaverAdsError as e:
        print(f"실패: {e}")
        return 1
    finally:
        client.close()
        con.close()
    print("구조 저장 완료:", summary, f"→ {db.DB_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
