"""작업 집는 순서 회귀 테스트 — 직원이 기다리는 작업이 먼저다.

증상: '답글 등록'을 눌렀는데 3분을 기다려도 '아직 확인이 안 돼요'만 뜬다
(사장님 제보, 집 PC는 켜져 있음). 원인은 오래된 순으로만 집어서, 앞에
리뷰수집(수 분 걸림) 같은 작업이 있으면 등록이 그 뒤로 밀리는 것.

계약: pending 중 post/post_edit/regen/wake 가 있으면 그것부터 집는다.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _Query:
    """supabase 쿼리 빌더 흉내 — 걸린 조건을 기록하고 결과를 돌려준다."""

    def __init__(self, rows, log):
        self._rows, self._log = rows, log
        self._kinds = None

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) == val]
        return self

    def in_(self, col, vals):
        self._kinds = list(vals)
        self._rows = [r for r in self._rows if r.get(col) in vals]
        return self

    def order(self, col, desc=False):
        self._rows = sorted(self._rows, key=lambda r: r.get(col) or "",
                            reverse=desc)
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def update(self, patch):
        self._log.append(("update", patch))
        return self

    def execute(self):
        self._log.append(("select", self._kinds, [r["id"] for r in self._rows]))
        return type("R", (), {"data": list(self._rows)})()


class _Client:
    def __init__(self, rows, log):
        self._rows, self._log = rows, log

    def table(self, name):
        return _Query(list(self._rows), self._log)


def _run(rows, monkeypatch):
    from database import supabase_client as db

    log = []
    monkeypatch.setattr(db, "get_client", lambda: _Client(rows, log))
    return db.claim_next_job(), log


def test_interactive_job_wins_over_older_collect(monkeypatch):
    rows = [
        {"id": 1, "kind": "collect", "status": "pending", "requested_at": "1"},
        {"id": 2, "kind": "post", "status": "pending", "requested_at": "9"},
    ]
    job, _ = _run(rows, monkeypatch)
    assert job["id"] == 2          # 나중에 들어왔어도 직원이 기다리는 작업


def test_oldest_interactive_first(monkeypatch):
    rows = [
        {"id": 1, "kind": "post", "status": "pending", "requested_at": "5"},
        {"id": 2, "kind": "post_edit", "status": "pending", "requested_at": "3"},
    ]
    job, _ = _run(rows, monkeypatch)
    assert job["id"] == 2


def test_falls_back_to_other_kinds(monkeypatch):
    rows = [{"id": 7, "kind": "collect", "status": "pending",
             "requested_at": "1"}]
    job, _ = _run(rows, monkeypatch)
    assert job["id"] == 7


def test_running_jobs_are_not_claimed(monkeypatch):
    rows = [{"id": 3, "kind": "post", "status": "running", "requested_at": "1"}]
    job, _ = _run(rows, monkeypatch)
    assert job is None
