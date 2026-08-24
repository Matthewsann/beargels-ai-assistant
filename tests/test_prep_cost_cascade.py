"""반제품 원가가 자재로 흘러가는지 — 회귀 테스트.

실사고(2026-08-24): '요거트 크림치즈(반제품)' 의 메뉴 원가는 7,939.8 원으로
잡혔는데 짝인 자재의 pack_cost 가 0 원으로 남아, 그 반제품을 쓰는
'새콤달콤 요거트 크림치즈'·'상콤달콤 요거트 크림치즈 L' 두 메뉴가 원가
0 원으로 보였다.

원인은 시드 경로였다. 자재·레시피를 일괄 주입하면 반제품의 레시피가 처음
채워지면서 반제품 원가가 잡히는데, 그 값을 자재로 흘려보내는 prep_sync 를
부르지 않았다. 웹의 레시피 편집 경로에는 _prep_cascade 가 붙어 있었지만
시드에는 없었다.
"""

import pytest

from database import supabase_client as db


class _Recorder:
    """table(...).select/upsert/eq/execute 체인을 흉내내며 아무것도 안 한다."""

    def __init__(self, rows_by_table):
        self._rows = rows_by_table
        self._name = None

    def table(self, name):
        self._name = name
        return self

    def select(self, *_a, **_k):
        return self

    def upsert(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def execute(self):
        return type("R", (), {"data": self._rows.get(self._name, [])})()


@pytest.fixture
def seeded(monkeypatch):
    """시드가 반제품 레시피 한 줄을 새로 넣는 상황."""
    ing = {"id": 1, "name": "요거트 크림치즈(반제품)", "unit": "g",
           "pack_qty": 850, "pack_cost": 0, "category": "반제품 재료",
           "supplier": "직접제조", "note": "제조 레시피: PREP-004"}
    client = _Recorder({
        "ingredients": [ing],
        "menu_items": [{"sku": "PREP-004"}, {"sku": "CREAM-002"}],
        "menu_recipes": [],
    })
    monkeypatch.setattr(db, "get_client", lambda: client)
    monkeypatch.setattr(db, "ingredients_all", lambda: [ing])
    monkeypatch.setattr(db, "recipes_all", lambda: [])
    monkeypatch.setattr(db, "recompute_costs",
                        lambda skus, **_k: {s: 1.0 for s in skus})
    calls = []
    monkeypatch.setattr(db, "prep_sync",
                        lambda *a, **k: (calls.append((a, k)), (1, {"CREAM-002": 560.5}))[1])
    return calls


SPEC = {
    "ingredients": [],
    "recipes": [{"sku": "PREP-004", "ingredient": "요거트 크림치즈(반제품)",
                 "unit": "g", "qty": 600}],
}


def test_시드가_반제품_동기화를_부른다(seeded):
    """이 호출이 빠지면 반제품을 쓰는 메뉴가 원가 0 원으로 남는다."""
    db.seed_ingredients_bulk(SPEC)
    assert seeded, "seed_ingredients_bulk 가 prep_sync 를 부르지 않았다"


def test_시드_결과에_동기화_건수가_담긴다(seeded):
    out = db.seed_ingredients_bulk(SPEC)
    assert out["preps_synced"] == 1
    # 동기화로 뒤이어 재계산된 메뉴도 결과에 합쳐져야 화면이 바로 갱신된다
    assert out["recomputed"] >= 1
