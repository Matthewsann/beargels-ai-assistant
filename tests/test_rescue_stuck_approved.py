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
    import time

    ag, fake = agent
    fake.approved = [{"id": 1}]
    # '마지막 점검이 주기보다 오래전'인 상태로 맞춘다(부팅 직후 monotonic 이
    # 작아도 테스트가 흔들리지 않게 절대값 대신 현재 시각 기준으로).
    monkeypatch.setattr(ag, "RESCUE_EVERY_SECONDS", 300)
    monkeypatch.setattr(ag, "_last_rescue", time.monotonic() - 301)

    ag.maybe_rescue_stuck()          # 첫 호출 — 실행됨
    first = len(fake.requested)
    fake.approved = [{"id": 2}]
    ag.maybe_rescue_stuck()          # 바로 다시 — 주기 전이라 건너뜀

    assert first == 1
    assert len(fake.requested) == 1


# ---------------------------------------------------------------------------
# 중복 등록 요청 — 두 번째 잡은 '실패'가 아니라 '건너뜀'이어야 한다
# (사장님 제보 2026-08-16: '리뷰 2783 가 등록 대기 상태가 아닙니다')
# ---------------------------------------------------------------------------

import pytest


@pytest.mark.parametrize("status,expect", [
    ("posted", "이미 등록됨"),
    ("drafted", "앞선 요청에서 처리됨"),
    ("skipped", "넘어가기"),
])
def test_duplicate_post_job_is_skipped_not_error(agent, status, expect):
    ag, fake = agent
    finished = []
    fake.get_review = lambda rid: {"id": rid, "reply_status": status}
    fake.finish_job = lambda jid, st, msg, n: finished.append((st, msg))
    fake.worker_ping = lambda *a, **k: None

    ag.run_post_job({"id": 1, "message": "2783"})

    assert finished, "잡을 닫아야 한다"
    st, msg = finished[0]
    assert st == "done", "중복 요청은 오류가 아니다"
    assert expect in msg and "2783" in msg


def test_missing_review_is_skipped_cleanly(agent):
    ag, fake = agent
    finished = []
    fake.get_review = lambda rid: None
    fake.finish_job = lambda jid, st, msg, n: finished.append((st, msg))
    fake.worker_ping = lambda *a, **k: None

    ag.run_post_job({"id": 2, "message": "999"})
    assert finished[0][0] == "done"


# ---------------------------------------------------------------------------
# 답글 '수정' 중 플랫폼 기한 만료 — run_post_job 처럼 조용히 정리해야 한다
# (사장님 제보 2026-08-25: 쿠팡 답글 수정 실패 id 89. run_post_job 은 이미
#  ReplyDeadlineError 를 봐주고 error_log 에 안 남기는데, run_post_edit_job
#  에는 그 처리가 없어 매일 새벽 점검이 똑같은 걸 계속 다시 보고했다.)
# ---------------------------------------------------------------------------

def test_edit_job_deadline_over_does_not_log_error(agent, monkeypatch):
    ag, fake = agent
    finished, errors = [], []
    # platform 은 baemin 으로 둔다 — coupang 이면 _refresh_reply_id 가 실제
    # 쿠팡 크롤러를 부르려 해 이 테스트의 관심사(예외 처리 분기)와 무관한
    # 네트워크 의존이 생긴다.
    fake.get_review = lambda rid: {
        "id": rid, "reply_status": "posted", "platform": "baemin",
        "review_no": "1", "raw": None}
    fake.finish_job = lambda jid, st, msg, n: finished.append((st, msg))
    fake.worker_ping = lambda *a, **k: None
    fake.log_error = lambda *a, **k: errors.append((a, k))
    monkeypatch.setattr(ag, "ensure_chrome", lambda: None)

    from crawler import review_reply as rr

    class _Boom:
        def __init__(self, *a, **k):
            pass

        def run(self, confirm=True):
            raise rr.ReplyDeadlineError(
                "쿠팡 답글 작성 기한이 지난 리뷰예요 — 등록할 수 없습니다.")

    monkeypatch.setattr(rr, "ReplyToReviewAction", _Boom)

    ag.run_post_edit_job({"id": 1, "message": "211"})

    assert errors == [], "재시도해도 안 되는 기한 만료는 error_log 에 쌓지 않는다"
    assert finished, "잡을 닫아야 한다"
    st, msg = finished[0]
    assert st == "error" and "211" in msg and "기한" in msg
