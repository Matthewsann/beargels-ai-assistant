"""방치된 '등록 대기(approved)' 답글 자동 복구 회귀 테스트.

배경: '답글 등록' 버튼은 mark_approved 후 request_post 를 부른다. 그 사이
통신이 끊기거나, 옛 '정시 일괄 등록' 시절에 쌓인 approved 는 잡이 없어
아무도 처리하지 않는데 화면엔 '등록 진행 중'으로 보인다(사장님 제보: 5건이
계속 대기).

핵심 계약: **잡이 아예 없는 건만** 다시 줄 세운다 — 대기/진행 중인 잡이
있으면 건드리지 않아야 같은 답글이 두 번 등록되지 않는다.
"""

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def agent(monkeypatch):
    """DB 를 가짜로 바꾼 worker.agent 모듈을 준다."""
    # supabase 실접속 없이 import 되도록 최소 스텁을 먼저 심는다.
    import worker.agent as ag

    fake = types.SimpleNamespace(
        approved=[], jobs={}, requested=[], errors=[],
        get_approved_reviews=lambda limit=100: fake.approved,
        latest_review_job=lambda kind, rid: fake.jobs.get((kind, rid)),
        request_post=lambda rid, by=None: fake.requested.append((rid, by)),
        log_error=lambda *a, **k: fake.errors.append((a, k)),
    )
    monkeypatch.setattr(ag, "db", fake)
    return ag, fake


def test_revives_only_rows_without_job(agent):
    ag, fake = agent
    fake.approved = [{"id": 1}, {"id": 2}, {"id": 3}]
    fake.jobs = {("post", 2): {"status": "pending"},      # 줄 서 있음
                 ("post", 3): {"status": "done"}}         # 이력 있음

    revived = ag.rescue_stuck_approved()

    assert revived == 1
    assert [rid for rid, _ in fake.requested] == [1]      # 잡 없던 것만


def test_no_rows_no_error_log(agent):
    ag, fake = agent
    fake.approved = [{"id": 9}]
    fake.jobs = {("post", 9): {"status": "pending"}}

    assert ag.rescue_stuck_approved() == 0
    assert fake.requested == []
    assert fake.errors == []          # 조용할 땐 오류로그를 남기지 않는다


def test_one_failure_does_not_stop_the_rest(agent):
    ag, fake = agent
    fake.approved = [{"id": 1}, {"id": 2}]

    def boom(rid, by=None):
        if rid == 1:
            raise RuntimeError("일시적 통신 오류")
        fake.requested.append((rid, by))

    fake.request_post = boom
    assert ag.rescue_stuck_approved() == 1
    assert [rid for rid, _ in fake.requested] == [2]


def test_rescue_is_throttled(agent, monkeypatch):
    ag, fake = agent
    fake.approved = [{"id": 1}]
    monkeypatch.setattr(ag, "_last_rescue", 0.0)
    monkeypatch.setattr(ag, "RESCUE_EVERY_SECONDS", 300)

    ag.maybe_rescue_stuck()          # 첫 호출 — 실행됨
    first = len(fake.requested)
    fake.approved = [{"id": 2}]
    ag.maybe_rescue_stuck()          # 바로 다시 — 주기 전이라 건너뜀

    assert first == 1
    assert len(fake.requested) == 1
