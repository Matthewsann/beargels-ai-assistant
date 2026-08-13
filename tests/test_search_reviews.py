"""전체 리뷰 검색(search_reviews) 회귀 테스트.

'답글 있음'의 기준이 두 가지라 헷갈리기 쉽다:
  ① 우리가 웹에서 등록한 것(reply_status == 'posted')
  ② 플랫폼에 이미 달려 있던 것(platform_replied)
둘 중 하나면 '답글 있음'이다. 이 계약이 깨지면 화면 필터가 거짓말을 한다.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _Q:
    """supabase 쿼리 빌더 흉내 — 걸린 조건만 기록한다(정확한 SQL 흉내 아님)."""

    def __init__(self, rows, calls):
        self.rows, self.calls = rows, calls
        self.not_ = self

    def select(self, *a, **k):
        self.calls.append(("select", k.get("count")))
        return self

    def eq(self, col, val):
        self.calls.append(("eq", col, val))
        self.rows = [r for r in self.rows if r.get(col) == val]
        return self

    def neq(self, col, val):
        self.calls.append(("neq", col, val))
        self.rows = [r for r in self.rows if r.get(col) != val]
        return self

    def is_(self, col, val):
        self.calls.append(("not_is", col, val))
        return self

    def ilike(self, col, pat):
        self.calls.append(("ilike", col, pat))
        needle = pat.strip("%")
        self.rows = [r for r in self.rows if needle in (r.get(col) or "")]
        return self

    def or_(self, expr):
        self.calls.append(("or", expr))
        return self

    def order(self, col, desc=False):
        self.calls.append(("order", col, desc))
        return self

    def range(self, a, b):
        self.calls.append(("range", a, b))
        self.rows = self.rows[a:b + 1]
        return self

    def execute(self):
        rows = self.rows
        return type("R", (), {"data": rows, "count": len(rows)})()


class _Client:
    def __init__(self, rows, calls):
        self.rows, self.calls = rows, calls

    def table(self, name):
        return _Q(list(self.rows), self.calls)


ROWS = [
    {"id": 1, "platform": "baemin", "rating": 5, "content": "맛있어요",
     "reply_status": "posted", "platform_replied": True},
    {"id": 2, "platform": "coupang", "rating": 2, "content": "늦게 왔어요",
     "reply_status": "drafted", "platform_replied": False},
    {"id": 3, "platform": "baemin", "rating": 4, "content": "또 시킬게요",
     "reply_status": None, "platform_replied": None},
]


def _call(monkeypatch, **kw):
    from database import supabase_client as db
    calls = []
    monkeypatch.setattr(db, "get_client", lambda: _Client(ROWS, calls))
    rows, total = db.search_reviews(**kw)
    return rows, total, calls


def test_platform_filter(monkeypatch):
    rows, total, _ = _call(monkeypatch, platform="baemin")
    assert {r["id"] for r in rows} == {1, 3}
    assert total == 2


def test_rating_filter(monkeypatch):
    rows, _, _ = _call(monkeypatch, rating=2)
    assert [r["id"] for r in rows] == [2]


def test_content_search(monkeypatch):
    rows, _, calls = _call(monkeypatch, q="늦게")
    assert [r["id"] for r in rows] == [2]
    assert ("ilike", "content", "%늦게%") in calls


def test_replied_yes_covers_both_sources(monkeypatch):
    """우리가 등록했거나 플랫폼에 이미 달렸으면 '답글 있음'."""
    _, _, calls = _call(monkeypatch, replied=True)
    ors = [c for c in calls if c[0] == "or"]
    assert ors, "답글 있음은 두 조건의 OR 이어야 한다"
    assert "reply_status.eq.posted" in ors[0][1]
    assert "platform_replied" in ors[0][1]


def test_replied_no_excludes_both_sources(monkeypatch):
    _, _, calls = _call(monkeypatch, replied=False)
    assert ("neq", "reply_status", "posted") in calls
    assert ("not_is", "platform_replied", "true") in calls


def test_sort_and_paging(monkeypatch):
    _, _, calls = _call(monkeypatch, sort="low", limit=10, offset=20)
    assert ("order", "rating", False) in calls
    assert ("range", 20, 29) in calls


def test_default_sort_is_newest(monkeypatch):
    _, _, calls = _call(monkeypatch)
    assert ("order", "written_date", True) in calls


def test_db_failure_returns_empty_not_crash(monkeypatch):
    from database import supabase_client as db

    def boom():
        raise RuntimeError("네트워크 오류")

    monkeypatch.setattr(db, "get_client", boom)
    assert db.search_reviews() == ([], 0)
