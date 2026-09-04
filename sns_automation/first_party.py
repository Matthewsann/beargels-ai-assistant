"""우리가 이미 가진 데이터를 콘텐츠 기획이 읽을 수 있게 꺼내온다.

왜 필요한가(시장조사 검토 2026-09-04):
    "시장조사가 부족한가?"를 따져보니, 부족한 건 밖에서 사 올 데이터가 아니라
    **이미 매주 쌓고 있는데 기획이 안 보던 것**이었다. 기획 프롬프트가 보던
    것은 브랜드 문서·편집 문법·인스타 계정·네이버 경쟁도뿐이고, 정작 아래 둘이
    빠져 있었다.

    ① 스마트플레이스 유입 검색어 — 사람들이 **실제로 무엇을 쳐서 우리 가게에
       들어왔는지**. 전국 검색량보다 강한 신호다(검색량은 '몇 명이 쳤나'지만
       이건 '몇 명이 쳐서 우리한테 왔나'). 주 1회 자동 수집되어
       menu_settings.place_keywords 에 이미 들어 있었다.
    ② 상품별 실매출 — **지금 무엇이 팔리는지**. product_sales_daily 에 매일
       쌓이는데 기획은 7월에 손으로 정리한 리뷰 분석 문서를 보고 있었다.

    실측(2026-08-24~30): 유입 690회 중 씨앗 키워드 6개가 닿는 건 12개뿐이고
    38개(유입 123회)는 조사 대상에도 없었다. 유입 2위 덩어리인 '타임스페이스'
    계열 30회가 통째로 안 보였다.

읽기만 한다 — 이 모듈은 아무것도 쓰지 않는다. 집 PC 일꾼 전용이다
(직원 웹은 Supabase 를 이렇게 무겁게 읽지 않는다).
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta

logger = logging.getLogger(__name__)

#: 우리 상호 — 이 말로 들어온 사람은 이미 우리를 알고 찾아온 것이라
#: '새로 발견되는 말'을 찾는 조사에서는 뺀다(경쟁을 조사할 이유도 없다).
BRAND = ("베어글스", "beargels", "베어그", "bear글스")

#: '송도'라는 이름의 다른 동네. naver_search 와 같은 목록을 쓴다.
OTHER_PLACE = ("부산", "울산", "대구", "속초", "제주", "여수", "포항", "창원")

#: 메뉴가 아닌 상품(이용권·쿠폰류) — 콘텐츠 소재가 될 수 없다.
NOT_MENU = ("패스", "쿠폰", "이용권", "충전", "선불", "예약금", "배송비", "봉투")


def _norm(s: str) -> str:
    return re.sub(r"[^\w가-힣]+", "", (s or "")).lower()


# ── ① 스마트플레이스 유입 검색어 ────────────────────────────────

def inflow(setting: dict | None = None) -> dict:
    """주간 유입 스냅샷 그대로. 없으면 빈 dict.

    crawler/place_stats.py 가 주 1회 모아 menu_settings.place_keywords 에
    저장한 것을 읽기만 한다.
    """
    if setting is not None:
        return setting or {}
    try:
        from database import supabase_client as db
        return db.get_setting("place_keywords") or {}
    except Exception as e:  # noqa: BLE001 — DB 가 막혀도 기획은 계속돼야 한다
        logger.debug("유입 키워드 없음: %s", e)
        return {}


def inflow_keywords(setting: dict | None = None, *, drop_brand: bool = True) -> list[dict]:
    """유입 검색어 목록 [{name, count, delta}] — 많이 들어온 순.

    drop_brand: 상호 검색은 뺀다. 이미 우리를 아는 사람이라 '새 손님을 데려올
    말'을 고르는 데는 쓸 수 없다(실측에서 상위 2개가 상호 검색이라 그냥 두면
    조사 예산을 상호에 다 쓴다).
    """
    rows = (inflow(setting).get("keywords") or [])
    out = []
    for r in rows:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        if drop_brand and any(b in _norm(name) for b in map(_norm, BRAND)):
            continue
        out.append({"name": name, "count": int(r.get("count") or 0),
                    "delta": r.get("delta")})
    out.sort(key=lambda r: -r["count"])
    return out


def _ok_seed(word: str) -> bool:
    """조사 씨앗으로 쓸 만한 말인가 — 다른 동네·경쟁 상호만 걸러낸다.

    naver_search.is_useful 과 달리 '지역명이 들어 있어야 한다'는 조건을 걸지
    않는다. 유입 검색어는 **이미 우리 가게로 사람을 데려온 것이 증명된 말**이라
    지역 조건을 다시 물을 필요가 없다(예: '베이글산도' 주 6회).
    """
    w = _norm(word)
    if any(_norm(x) in w for x in OTHER_PLACE):
        return False
    try:
        from .naver_search import _rivals
        if any(_norm(x) in w for x in _rivals()):
            return False
    except Exception:  # noqa: BLE001
        pass
    return len(w) >= 3


def inflow_seeds(limit: int = 6, setting: dict | None = None) -> list[str]:
    """네이버 경쟁 조사의 씨앗 — 실제로 손님을 데려온 말에서 뽑는다.

    코드에 박아둔 씨앗 6개는 사람이 상상해서 적은 것이라, 실측 유입의 24%에만
    닿았다. 이 함수를 쓰면 조사 대상이 매주 저절로 갱신된다.
    """
    seeds = []
    for r in inflow_keywords(setting):
        if r["count"] <= 0 or not _ok_seed(r["name"]):
            continue
        seeds.append(r["name"])
        if len(seeds) >= limit:
            break
    return seeds


def inflow_as_prompt_context(setting: dict | None = None, limit: int = 12) -> str:
    """기획 프롬프트에 넣을 글 — 가장 강한 수요 신호."""
    data = inflow(setting)
    rows = inflow_keywords(data)[:limit]
    if not rows:
        return ""
    head = "[실제로 우리 가게를 찾아 들어온 검색어 — 가장 강한 근거]"
    if data.get("period"):
        head += f" {data['period']}"
        if data.get("total"):
            head += f", 유입 {data['total']}회"
    lines = [head]
    for r in rows:
        d = r.get("delta")
        move = f" (전주 대비 {d:+d})" if isinstance(d, int) and d else ""
        lines.append(f"· {r['name']} {r['count']}회{move}")
    lines.append("이 말들이 지금 우리 가게를 찾게 만드는 말이다. 주제와 blog_keyword 는 "
                 "여기서 먼저 고르고, 늘고 있는 말(+)을 우선한다.")
    return "\n".join(lines)


# ── ② 상품별 실매출 ────────────────────────────────────────────

def _clean_product(name: str) -> str:
    """포스 상품명을 사람이 읽는 메뉴 이름에 가깝게 다듬는다."""
    s = re.sub(r"^\s*(\[[^\]]{1,12}\]|[A-Z]\))\s*", "", name or "").strip()
    s = re.sub(r"\s*\((1~?2?인분?|기본|1인)\)\s*$", "", s).strip()
    return s or (name or "").strip()


def _group_key(name: str) -> str:
    """세트 구성이 다른 같은 메뉴를 하나로 묶는 이름.

    포스에는 '베이글 샌드위치 + 음료 세트', '+ 음료 + 베이글 하나더' 처럼
    구성만 다른 상품이 여럿이라, 그대로 줄 세우면 상위 8칸 중 5칸을 한 메뉴가
    먹고 계절 한정 같은 신호가 밀려난다. '+' 앞까지를 한 메뉴로 본다.
    """
    return re.split(r"\s*\+", _clean_product(name))[0].strip()


def top_products(days: int = 30, limit: int = 8, rows: list[dict] | None = None) -> list[dict]:
    """최근 N일 매출 상위 메뉴 [{name, amount}] — 세트 구성은 합치고 이용권은 뺀다."""
    if rows is None:
        try:
            from database import mkt_store
            today = date.today()
            rows = mkt_store.product_sales_between(today - timedelta(days=days), today)
        except Exception as e:  # noqa: BLE001
            logger.debug("상품 매출 없음: %s", e)
            return []
    agg: dict[str, int] = {}
    for r in rows or []:
        name = (r.get("product") or "").strip()
        if not name or any(k in name for k in NOT_MENU):
            continue
        key = _group_key(name)
        if not key:
            continue
        agg[key] = agg.get(key, 0) + int(r.get("amount") or 0)
    top = sorted(agg.items(), key=lambda t: -t[1])[:limit]
    return [{"name": n, "amount": a} for n, a in top if a > 0]


def sales_as_prompt_context(days: int = 30, limit: int = 8,
                            rows: list[dict] | None = None) -> str:
    """기획 프롬프트에 넣을 글 — 지금 실제로 팔리는 것."""
    items = top_products(days, limit, rows)
    if not items:
        return ""
    lines = [f"[최근 {days}일 실제로 잘 팔린 메뉴 — 포스 매출 순]"]
    for i, p in enumerate(items, 1):
        lines.append(f"{i}. {p['name']} ({p['amount'] // 10000}만원)")
    lines.append("소재는 이 중에서 고르는 것이 안전하다 — 이미 팔리는 것을 더 팔리게 "
                 "하는 쪽이 새 메뉴를 알리는 것보다 빠르다. 계절 한정은 순위가 "
                 "올라오고 있는지 함께 본다.")
    return "\n".join(lines)
