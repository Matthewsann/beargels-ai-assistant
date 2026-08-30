"""릴스 발행 ↔ 매출 연결 — 목표의 마지막 고리.

페이지 목표(사장님 확정 2026-08-28)의 완성 기준은 "릴스가 나왔다"가 아니라
**노출 → 매장 방문 → 매출**이다. 매출 데이터(배민·쿠팡 주문)는 비서 쪽이
이미 Supabase 에 모으고 있으므로, 릴스 발행일과 붙이기만 하면 된다.

발행 전 7일 평균 매출 vs 발행 후 3일 평균 매출을 비교한다.
⚠️ 이건 상관관계지 인과가 아니다 — 화면에도 '참고용'으로만 표시한다.
날씨·요일·프로모션이 다 섞여 있다. 여러 릴스가 쌓여야 패턴이 보인다.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

BEFORE_DAYS = 7    # 발행 전 비교 구간
AFTER_DAYS = 3     # 발행 후 관찰 구간 (발행일 포함)

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_CACHE = os.path.join(_DATA_DIR, "sales_link_cache.json")
_TTL = 6 * 3600    # 지난 날짜 매출은 잘 안 변하므로 6시간 캐시


def _daily_sales(start: date, end: date) -> dict[str, int] | None:
    """구간의 날짜별 매출 합(원). DB를 못 읽으면 None."""
    try:
        from database.supabase_client import get_client
        rows = (get_client().table("orders")
                .select("ordered_date,price")
                .gte("ordered_date", start.isoformat())
                .lte("ordered_date", end.isoformat())
                .execute().data) or []
    except Exception as e:
        logger.debug("매출 조회 실패(무시): %s", e)
        return None
    out: dict[str, int] = {}
    for r in rows:
        d, p = r.get("ordered_date"), r.get("price")
        if d and isinstance(p, int):
            out[d] = out.get(d, 0) + p
    return out


def _load_cache() -> dict:
    try:
        with open(_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_cache(c: dict) -> None:
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(_CACHE, "w", encoding="utf-8") as f:
            json.dump(c, f, ensure_ascii=False)
    except OSError:
        pass


def effect(published_at: int) -> dict | None:
    """발행 전후 매출 비교. 데이터가 없으면 None.

    반환: {before_avg, after_avg, pct, after_days, partial}
      · pct: 전 대비 후 평균의 변화율(%)
      · partial: 발행 후 관찰 구간이 아직 다 안 지났으면 True
    """
    if not published_at:
        return None
    pub = datetime.fromtimestamp(published_at).date()
    today = date.today()
    if pub > today:
        return None

    key = f"{pub.isoformat()}"
    cache = _load_cache()
    hit = cache.get(key)
    if hit and time.time() - hit.get("at", 0) < _TTL:
        return hit.get("val")

    sales = _daily_sales(pub - timedelta(days=BEFORE_DAYS), pub + timedelta(days=AFTER_DAYS - 1))
    val = None
    if sales:
        before = [sales.get((pub - timedelta(days=i)).isoformat(), 0)
                  for i in range(1, BEFORE_DAYS + 1)]
        after_dates = [pub + timedelta(days=i) for i in range(AFTER_DAYS)]
        usable = [d for d in after_dates if d <= today]
        after = [sales.get(d.isoformat(), 0) for d in usable]
        # 수집이 아예 없던 날(0원)은 평균을 왜곡하므로 뺀다
        before_nz = [v for v in before if v > 0]
        after_nz = [v for v in after if v > 0]
        if before_nz and after_nz:
            b = sum(before_nz) / len(before_nz)
            a = sum(after_nz) / len(after_nz)
            val = {
                "before_avg": int(b),
                "after_avg": int(a),
                "pct": round((a - b) / b * 100, 1) if b else None,
                "after_days": len(after_nz),
                "partial": len(usable) < AFTER_DAYS,
            }

    cache[key] = {"at": time.time(), "val": val}
    _save_cache(cache)
    return val
