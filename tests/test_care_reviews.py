"""⚠️ 관리 필요 리뷰(별점 5점 미만 + CS) 회귀 테스트.

만점 리뷰에 묻혀 정작 손봐야 할 리뷰를 놓치지 않게 따로 뺀 화면
(사장님 요청 2026-08-16).

계약:
  · 기본(all)  = 별점 ≤4 **또는** CS 유형(불만·민감)
  · low        = 별점 ≤4 만
  · cs         = CS 유형만
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _Q:
    def __init__(self, calls):
        self.calls = calls

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self.calls.append(("eq", col, val))
        return self

    def lte(self, col, val):
        self.calls.append(("lte", col, val))
        return self

    def in_(self, col, vals):
        self.calls.append(("in", col, tuple(vals)))
        return self

    def or_(self, expr):
        self.calls.append(("or", expr))
        return self

    def order(self, col, desc=False):
        self.calls.append(("order", col, desc))
        return self

    def range(self, a, b):
        self.calls.append(("range", a, b))
        return self

    def execute(self):
        return type("R", (), {"data": [{"id": 1}], "count": 1})()


class _Client:
    def __init__(self, calls):
        self.calls = calls

    def table(self, name):
        return _Q(self.calls)


def _call(monkeypatch, **kw):
    from database import supabase_client as db
    calls = []
    monkeypatch.setattr(db, "get_client", lambda: _Client(calls))
    rows, total = db.get_attention_reviews(**kw)
    return rows, total, calls


def test_default_is_low_rating_or_cs(monkeypatch):
    _, _, calls = _call(monkeypatch)
    expr = next(c[1] for c in calls if c[0] == "or")
    assert "rating.lte.4" in expr
    assert "complaint" in expr and "escalate" in expr


def test_low_mode_is_rating_only(monkeypatch):
    _, _, calls = _call(monkeypatch, mode="low")
    assert ("lte", "rating", 4) in calls
    assert not [c for c in calls if c[0] == "or"]


def test_cs_mode_is_kind_only(monkeypatch):
    _, _, calls = _call(monkeypatch, mode="cs")
    assert ("in", "kind", ("complaint", "escalate")) in calls
    assert not [c for c in calls if c[0] == "lte"]


def test_platform_and_paging(monkeypatch):
    _, _, calls = _call(monkeypatch, platform="baemin", limit=30, offset=60)
    assert ("eq", "platform", "baemin") in calls
    assert ("range", 60, 89) in calls


def test_sort_low_orders_by_rating(monkeypatch):
    _, _, calls = _call(monkeypatch, sort="low")
    assert ("order", "rating", False) in calls


def test_db_failure_returns_empty(monkeypatch):
    from database import supabase_client as db

    def boom():
        raise RuntimeError("네트워크")

    monkeypatch.setattr(db, "get_client", boom)
    assert db.get_attention_reviews() == ([], 0)
