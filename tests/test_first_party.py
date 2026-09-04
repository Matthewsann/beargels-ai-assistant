"""1차 데이터(유입 검색어·상품 매출)가 콘텐츠 기획에 들어가는지 (DB·네트워크 불필요).

왜 이 테스트가 있나(시장조사 검토 2026-09-04):
    "시장조사가 부족한가?"를 따져보니 밖에서 사 올 데이터가 아니라 **이미 매주
    쌓고 있는데 기획이 안 보던 것**이 문제였다. 실측: 유입 검색어 50개 중 코드에
    박아둔 씨앗 6개가 닿는 건 12개뿐이었고, 유입 2위 덩어리인 '타임스페이스'
    계열 30회는 조사 대상에조차 없었다.

계약:
  · 유입 검색어에서 조사 씨앗을 뽑는다 — 상호 검색은 빼고, 많이 들어온 순
  · 유입은 이미 우리 가게로 사람을 데려온 말이라 '지역명 포함' 조건을 다시 안 건다
  · 매출은 세트 구성 차이를 한 메뉴로 합친다(안 그러면 상위 칸을 한 메뉴가 먹는다)
  · 이용권·쿠폰은 메뉴가 아니라 소재에서 뺀다
  · **모르는 숫자는 프롬프트에 쓰지 않는다** — 웹 수집분의 좋아요·릴스 비중
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sns_automation import first_party as fp  # noqa: E402
from sns_automation import market_scan, naver_search as ns  # noqa: E402

#: 2026-08-24~30 실제 수집분에서 추린 모양
INFLOW = {
    "period": "2026-08-24 ~ 2026-08-30", "total": 690, "mapPv": 472,
    "keywords": [
        {"name": "베어글스송도", "count": 22, "delta": -6},
        {"name": "베어글스", "count": 20, "delta": -25},
        {"name": "송도베이글", "count": 16, "delta": 7},
        {"name": "송도타임스페이스맛집", "count": 9, "delta": 3},
        {"name": "송도타임스페이스베이글", "count": 8, "delta": None},
        {"name": "베이글산도", "count": 6, "delta": 4},
        {"name": "부산송도베이글", "count": 5, "delta": None},
        {"name": "동춘동베이글", "count": 5, "delta": None},
        {"name": "송도베어글스", "count": 3, "delta": -7},
    ],
}


# ── 유입 검색어 ──────────────────────────────────────────────

def test_inflow_keywords_drops_brand_and_sorts():
    rows = fp.inflow_keywords(INFLOW)
    names = [r["name"] for r in rows]
    assert "베어글스" not in names and "베어글스송도" not in names
    assert "송도베어글스" not in names            # 상호가 뒤에 붙어도 뺀다
    assert names[0] == "송도베이글"                # 많이 들어온 순
    assert rows[0]["count"] == 16 and rows[0]["delta"] == 7
    # 상호를 남기고 싶으면 그렇게도 된다
    assert "베어글스" in [r["name"] for r in fp.inflow_keywords(INFLOW, drop_brand=False)]


def test_seed_filter_keeps_proven_terms_without_region():
    """유입 검색어는 이미 우리 가게로 사람을 데려왔으니 지역명을 다시 묻지 않는다."""
    assert fp._ok_seed("베이글산도")               # 지역명이 없어도 통과
    assert fp._ok_seed("동춘동베이글")             # 씨앗 목록에 없던 인접 동네
    assert not fp._ok_seed("부산송도베이글")        # 같은 이름 다른 동네
    assert not fp._ok_seed("송도베이글리스트")      # 경쟁 상호
    assert not fp._ok_seed("가")                   # 너무 짧다
    # naver_search 쪽은 지역명을 요구한다 — 두 규칙이 다르다는 것이 요점
    assert not ns.is_useful("베이글산도")


def test_inflow_seeds_picks_top_usable():
    seeds = fp.inflow_seeds(limit=4, setting=INFLOW)
    assert seeds == ["송도베이글", "송도타임스페이스맛집", "송도타임스페이스베이글", "베이글산도"]
    assert "부산송도베이글" not in seeds


def test_inflow_prompt_shows_counts_and_movement():
    text = fp.inflow_as_prompt_context(INFLOW, limit=4)
    assert "유입 690회" in text and "2026-08-24" in text
    assert "송도베이글 16회 (전주 대비 +7)" in text
    assert "송도타임스페이스베이글 8회" in text     # delta 가 없으면 증감을 안 쓴다
    assert "(전주 대비 +0)" not in text
    assert "베어글스" not in text
    assert fp.inflow_as_prompt_context({}) == ""


# ── 상품 매출 ───────────────────────────────────────────────

SALES = [
    {"product": "[SET] 베이글 샌드위치 + 음료 + 베이글 하나더 (1~2인분)", "amount": 2_073_745},
    {"product": "[SET] 베이글 샌드위치 + 음료 세트 (1인)", "amount": 1_197_558},
    {"product": "E)아메리카노", "amount": 1_076_778},
    {"product": "올나잇패스8/1~8/31", "amount": 837_000},
    {"product": "생과일 수박 주스", "amount": 699_383},
    {"product": "망고 하나가득 산도", "amount": 516_944},
    {"product": "[COUPLE] 베이글 2종", "amount": 430_000},
    {"product": "종이봉투", "amount": 12_000},
]


def test_top_products_groups_sets_and_drops_non_menu():
    items = fp.top_products(rows=SALES, limit=6)
    names = [i["name"] for i in items]
    assert names[0] == "베이글 샌드위치"                  # 세트 구성 차이를 합친다
    assert items[0]["amount"] == 2_073_745 + 1_197_558
    assert "올나잇패스8/1~8/31" not in names             # 이용권은 메뉴가 아니다
    assert "종이봉투" not in names
    assert "아메리카노" in names                          # 'E)' 접두어 제거
    assert "베이글 2종" in names                          # '[COUPLE]' 접두어 제거
    assert "망고 하나가득 산도" in names                   # 계절 한정이 밀려나지 않는다


def test_sales_prompt_lists_menu_in_won():
    text = fp.sales_as_prompt_context(rows=SALES, limit=4)
    assert "1. 베이글 샌드위치 (327만원)" in text
    assert "포스 매출 순" in text
    assert fp.sales_as_prompt_context(rows=[]) == ""


# ── 씨앗 합치기 ─────────────────────────────────────────────

def test_merged_seeds_prefers_inflow_then_fills_from_defaults(monkeypatch):
    monkeypatch.setattr(fp, "inflow_seeds", lambda limit=6: ["송도베이글", "베이글산도"])
    seeds = ns.merged_seeds(limit=5)
    assert seeds[:2] == ["송도베이글", "베이글산도"]
    assert len(seeds) == 5
    assert all(s in seeds for s in ["베이글산도"])
    # 기본 씨앗으로 채우되 중복은 없다
    assert len(set(ns.norm(s) for s in seeds)) == 5


def test_merged_seeds_falls_back_when_no_inflow(monkeypatch):
    def boom(limit=6):
        raise RuntimeError("DB 없음")
    monkeypatch.setattr(fp, "inflow_seeds", boom)
    seeds = ns.merged_seeds(limit=4)
    assert seeds == list(ns.SEEDS)[:4]          # 유입이 없어도 조사는 돈다


# ── 모르는 숫자는 쓰지 않는다 ────────────────────────────────

def _scan(source):
    return {
        "source": source,
        "hashtags": {
            "송도베이글": {
                "count": 16, "reels_ratio": 0.0,
                "caption_length_median": 447, "hashtag_count_median": 6,
                "hooks": ["쫀득함으로 소문난 베이글집"],
                "top_posts": [{"hook": "쫀득함으로 소문난 베이글집",
                               "likes": None if source == "web" else 87,
                               "comments": None, "type": "IMAGE"}],
            }
        },
    }


def test_web_scan_never_prints_unknown_numbers():
    """웹 격자 수집은 좋아요를 못 가져온다 — '♥0'·'릴스 비중 0%'는 거짓말이었다."""
    text = market_scan.as_prompt_context(_scan("web"))
    assert "♥" not in text
    assert "릴스 비중" not in text
    assert "최근 게시물" in text and "알 수 없다" in text
    assert "쫀득함으로 소문난 베이글집" in text        # 사실인 것은 남는다
    assert "캡션 중앙값 447자" in text


def test_api_scan_still_prints_real_numbers():
    text = market_scan.as_prompt_context(_scan("api"))
    assert "(♥87)" in text and "릴스 비중 0%" in text
    assert "잘 되는 게시물" in text


def test_summarize_marks_whether_ranking_is_real():
    web = market_scan.summarize([{"caption": "가나다라", "like_count": None,
                                  "comments_count": None, "media_type": "IMAGE"}])
    api = market_scan.summarize([{"caption": "가나다라", "like_count": 5,
                                  "comments_count": 1, "media_type": "REELS"}])
    assert web["ranked_by_engagement"] is False
    assert api["ranked_by_engagement"] is True


# ── 기획 프롬프트에 실제로 들어가는가 ──────────────────────────

def test_planner_prompt_includes_first_party_blocks(monkeypatch):
    from sns_automation import planner
    monkeypatch.setattr(planner, "_brand_core", lambda: "브랜드")
    monkeypatch.setattr(planner, "_editing_rules", lambda: "문법")
    monkeypatch.setattr(planner, "_hook_summary", lambda: "훅")
    monkeypatch.setattr(planner, "_brief_feedback", lambda: "")
    monkeypatch.setattr(planner, "_naver", lambda: "[네이버 검색 실측]")
    monkeypatch.setattr(planner, "_market", lambda: "[시장]")
    monkeypatch.setattr(planner, "_place", lambda: "[유입 검색어] 송도베이글 16회")
    monkeypatch.setattr(planner, "_sales", lambda: "[잘 팔린 메뉴] 베이글 샌드위치")
    p = planner._ideas_system()
    assert "송도베이글 16회" in p and "베이글 샌드위치" in p
    # 근거의 우선순위가 프롬프트에 명시돼야 한다
    assert "근거의 우선순위" in p
    # '[네이버 검색 실측]' 은 규칙 문장에도 나오므로 **블록 자리**(마지막 등장)와 비교한다
    naver_block = p.rindex("[네이버 검색 실측]")
    assert p.index("[유입 검색어]") < naver_block
    assert p.index("[잘 팔린 메뉴]") < naver_block
    assert p.index("[유입 검색어]") < p.index("[잘 팔린 메뉴]")
