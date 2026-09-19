"""SPEC 7-1 확인용: GET /ncc/campaigns 한 번 → 200 이면 campaignTp 만 출력.

    python -m naver_ads.check_campaigns
"""

from __future__ import annotations

import logging
import sys

from naver_ads.client import NaverAdsClient, NaverAdsError

TARGET = "cmp-a001-06-000000010065791"


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # 윈도우 콘솔 cp949 대비
    client = NaverAdsClient()
    try:
        campaigns = client.get("/ncc/campaigns")
    except NaverAdsError as e:
        print(f"실패: {e}")
        return 1
    finally:
        client.close()

    print(f"200 OK — 캠페인 {len(campaigns)}개")
    found = False
    for c in campaigns:
        mark = " ← 대상" if c.get("nccCampaignId") == TARGET else ""
        print(f"{c.get('nccCampaignId')}  campaignTp={c.get('campaignTp')}{mark}")
        found = found or bool(mark)
    if not found:
        print(f"⚠ 대상 캠페인 {TARGET} 이 목록에 없다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
