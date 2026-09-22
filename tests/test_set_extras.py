"""세트 옵션 추가금 — 가장 불리한 조합은 '가장 비싼 것'이 아니라 원가율이 가장 나쁜 것."""
from database.supabase_client import _set_cost, extra_key

COST = {"SAND": 3000, "AME": 600, "LATTE": 1100, "ADE": 1400}
COMPS = [
    {"component_sku": "SAND", "qty": 1, "choice_group": None},
    {"component_sku": "AME", "qty": 1, "choice_group": "음료"},
    {"component_sku": "LATTE", "qty": 1, "choice_group": "음료"},
    {"component_sku": "ADE", "qty": 1, "choice_group": "음료"},
]


def test_without_extras_picks_most_expensive():
    r = _set_cost("SET", COMPS, COST.get, price=12000, extras=None)
    assert r["cost"] == 4400 and r["extra"] == 0
    assert r["picks"][0]["component_sku"] == "ADE"


def test_extras_change_worst_combination():
    # 에이드 +1,000 이면 에이드 33.8% < 라떼(+0) 34.2% → 라떼가 가장 불리
    ex = {extra_key("ADE", "음료"): 1000}
    r = _set_cost("SET", COMPS, COST.get, price=12000, extras=ex)
    assert r["picks"][0]["component_sku"] == "LATTE" and r["extra"] == 0
    # 라떼 +500(32.8%) 보다 에이드 +1,000(33.8%) 이 더 불리 — 가장 비싼 원가가
    # 아니라 원가율로 고른다는 점이 핵심
    ex[extra_key("LATTE", "음료")] = 500
    r = _set_cost("SET", COMPS, COST.get, price=12000, extras=ex)
    assert r["picks"][0]["component_sku"] == "ADE" and r["extra"] == 1000
    assert r["cost"] == 4400
    # 원가율을 기본과 같게(B 기준) 받으면 기본 옵션이 가장 불리한 축이 된다
    ex[extra_key("LATTE", "음료")] = 1700; ex[extra_key("ADE", "음료")] = 2700
    r = _set_cost("SET", COMPS, COST.get, price=12000, extras=ex)
    assert r["picks"][0]["component_sku"] == "AME"


def test_unknown_cost_blocks_set():
    assert _set_cost("SET", COMPS, {**COST, "ADE": None}.get, price=12000) is None


def test_no_price_falls_back_to_most_expensive():
    ex = {extra_key("ADE", "음료"): 5000}
    r = _set_cost("SET", COMPS, COST.get, price=None, extras=ex)
    assert r["picks"][0]["component_sku"] == "ADE" and r["extra"] == 5000
