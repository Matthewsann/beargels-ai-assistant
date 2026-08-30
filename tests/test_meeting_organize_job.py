"""집 PC 일꾼의 회의 'AI로 정리' 잡 처리 — 실 Supabase 로 왕복 검증.

지키는 계약:
  1) AI 제안은 기존 결정사항·할 일을 **지우지 않고 덧붙인다**(사장님이
     이미 적어 둔 내용을 AI 가 밀어버리면 안 된다).
  2) 덧붙는 결정사항·업무엔 "(AI 제안 …)" 표시가 붙는다 — 확인이 필요하다는 뜻.
  3) 담당자·마감일은 비운 채로 들어간다(짐작 금지).
  4) 무료 AI 를 못 쓰면(MeetingAIUnavailable) 잡이 error 로 끝나고, 그래도
     기존 내용은 그대로 남는다.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assistant.meeting_ai import MeetingAIUnavailable  # noqa: E402
from database import meeting_store as mt  # noqa: E402
from worker import agent  # noqa: E402


@pytest.fixture
def meeting(monkeypatch):
    monkeypatch.setattr(agent.db, "worker_ping", lambda *a, **k: None)
    monkeypatch.setattr(agent.db, "log_error", lambda *a, **k: None)
    finished = {}
    monkeypatch.setattr(
        agent.db, "finish_job",
        lambda jid, status, message=None, count=None: finished.update(
            status=status, message=message, count=count))
    mid = mt.create_meeting(
        "일꾼 AI정리 테스트용 임시 회의", body="포장 누락 리뷰가 이번 달 4건.",
        decisions="이미 적어 둔 결정")
    mt.save_tasks(mid, [{"content": "이미 있던 할 일", "owner": "사장님"}])
    yield mid, finished
    mt.delete_meeting(mid)


def _job(mid):
    return {"id": 999999999, "message": str(mid)}


def test_appends_without_erasing_existing_content(monkeypatch, meeting):
    mid, finished = meeting
    monkeypatch.setattr(agent, "ai_organize", lambda m: {
        "decisions": ["세트 포장 전 대조"],
        "tasks": [{"content": "포장 대조 안내문 붙이기", "memo": "그림 참고"}],
    })

    agent.run_meeting_organize_job(_job(mid))

    m = mt.get_meeting(mid)
    lines = m["decisions"].splitlines()
    assert lines[0] == "이미 적어 둔 결정"          # 기존 것 그대로
    assert lines[1] == "세트 포장 전 대조 (AI 제안 — 확인 필요)"

    tasks = mt.get_tasks(mid)
    contents = [t["content"] for t in tasks]
    assert "이미 있던 할 일" in contents             # 기존 업무 안 지워짐
    added = [t for t in tasks if t["content"] == "포장 대조 안내문 붙이기 (AI 제안)"]
    assert len(added) == 1
    assert added[0]["owner"] is None                 # 담당자 짐작 안 함
    assert added[0]["due_date"] is None              # 마감일 짐작 안 함
    assert added[0]["memo"] == "그림 참고"

    assert finished["status"] == "done"
    assert "1건" in finished["message"]


def test_existing_task_owner_and_done_state_survive(monkeypatch, meeting):
    """기존 업무의 담당자·완료 여부는 저장 재작성 과정에서도 유지된다."""
    mid, finished = meeting
    existing_id = mt.get_tasks(mid)[0]["id"]
    mt.set_task_done(existing_id, True)

    monkeypatch.setattr(agent, "ai_organize",
                        lambda m: {"decisions": [], "tasks": [{"content": "새 업무"}]})
    agent.run_meeting_organize_job(_job(mid))

    tasks = {t["id"]: t for t in mt.get_tasks(mid)}
    assert tasks[existing_id]["owner"] == "사장님"
    assert tasks[existing_id]["done"] is True


def test_no_suggestions_reports_done_with_zero_count(monkeypatch, meeting):
    mid, finished = meeting
    monkeypatch.setattr(agent, "ai_organize",
                        lambda m: {"decisions": [], "tasks": []})
    agent.run_meeting_organize_job(_job(mid))
    assert finished["status"] == "done"
    assert finished["count"] == 0
    # 기존 내용은 손대지 않았다
    assert mt.get_meeting(mid)["decisions"] == "이미 적어 둔 결정"


def test_free_ai_unavailable_ends_job_as_error_without_touching_content(
        monkeypatch, meeting):
    mid, finished = meeting

    def boom(m):
        raise MeetingAIUnavailable("Gemini 429: 무료 한도 소진")

    monkeypatch.setattr(agent, "ai_organize", boom)
    agent.run_meeting_organize_job(_job(mid))

    assert finished["status"] == "error"
    assert "무료" in finished["message"]
    assert mt.get_meeting(mid)["decisions"] == "이미 적어 둔 결정"
    assert len(mt.get_tasks(mid)) == 1


def test_unknown_meeting_id_finishes_as_error(monkeypatch):
    monkeypatch.setattr(agent.db, "worker_ping", lambda *a, **k: None)
    finished = {}
    monkeypatch.setattr(
        agent.db, "finish_job",
        lambda jid, status, message=None, count=None: finished.update(
            status=status, message=message))
    agent.run_meeting_organize_job(_job(999999999))
    assert finished["status"] == "error"
