"""회의 'AI로 정리' 순수 로직 테스트 — 실제 AI 호출 없이 돈다.

지키는 계약:
  1) 무료 AI(Gemini)만 쓴다 — llm.complete 을 only=("gemini",) 로 부른다.
     (사장님 지시 2026-08-30: "api key로 유료면 사용 x")
  2) AI 응답에서 JSON 을 뽑아낸다 — 코드블록·잡담이 섞여 와도 견딘다.
  3) 담당자·마감일은 절대 지어내지 않는다(그 칸 자체를 응답에 안 받는다).
  4) 개수 상한(결정 6·업무 8)과 길이 상한을 지킨다.
  5) 논의 내용이 비어 있으면 AI 를 아예 부르지 않는다.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assistant import meeting_ai  # noqa: E402


def test_organize_skips_ai_call_when_body_empty(monkeypatch):
    called = []
    monkeypatch.setattr("llm.complete", lambda **kw: called.append(kw) or "무시")
    result = meeting_ai.organize({"title": "제목만 있음", "body": "  "})
    assert result == {"decisions": [], "tasks": []}
    assert called == []


def test_organize_calls_gemini_only(monkeypatch):
    seen = {}

    def fake_complete(**kw):
        seen.update(kw)
        return '{"decisions": [], "tasks": []}'

    monkeypatch.setattr("llm.complete", fake_complete)
    meeting_ai.organize({"title": "주간회의", "body": "포장 누락 이야기"})
    assert seen["only"] == ("gemini",)


def test_organize_raises_unavailable_when_ai_fails(monkeypatch):
    def boom(**kw):
        raise RuntimeError("Gemini 429: 무료 한도 소진")

    monkeypatch.setattr("llm.complete", boom)
    with pytest.raises(meeting_ai.MeetingAIUnavailable):
        meeting_ai.organize({"body": "내용"})


# ── 응답 파싱 ─────────────────────────────────────────────────

def test_parse_plain_json():
    out = meeting_ai.parse_response(
        '{"decisions": ["결정1"], "tasks": [{"content": "업무1", "memo": "메모"}]}')
    assert out == {"decisions": ["결정1"],
                   "tasks": [{"content": "업무1", "memo": "메모"}]}


def test_parse_strips_code_fence_and_chatter():
    raw = ('물론이죠! 아래와 같이 정리했습니다.\n```json\n'
          '{"decisions": ["세트 포장 전 대조"], "tasks": []}\n```\n감사합니다.')
    out = meeting_ai.parse_response(raw)
    assert out["decisions"] == ["세트 포장 전 대조"]


def test_parse_drops_empty_items():
    raw = '{"decisions": ["  ", "실결정"], "tasks": [{"content": "  "}, {"content": "실업무"}]}'
    out = meeting_ai.parse_response(raw)
    assert out["decisions"] == ["실결정"]
    assert out["tasks"] == [{"content": "실업무", "memo": ""}]


def test_parse_never_invents_owner_or_due_fields():
    """AI 가 owner/due_date 를 응답에 넣어도 무시한다 — 짐작 담당·기한 차단."""
    raw = ('{"decisions": [], "tasks": '
          '[{"content": "안내문 붙이기", "owner": "지은", "due_date": "2026-09-01"}]}')
    out = meeting_ai.parse_response(raw)
    assert out["tasks"] == [{"content": "안내문 붙이기", "memo": ""}]


def test_parse_enforces_count_and_length_caps():
    decisions = [f"결정{i}" for i in range(20)]
    tasks = [{"content": f"업무{i}"} for i in range(20)]
    import json
    raw = json.dumps({"decisions": decisions, "tasks": tasks})
    out = meeting_ai.parse_response(raw)
    assert len(out["decisions"]) == meeting_ai.MAX_DECISIONS
    assert len(out["tasks"]) == meeting_ai.MAX_TASKS


def test_parse_raises_on_garbage_response():
    with pytest.raises(meeting_ai.MeetingAIUnavailable):
        meeting_ai.parse_response("죄송합니다, 이해하지 못했어요.")


def test_parse_raises_on_malformed_json():
    with pytest.raises(meeting_ai.MeetingAIUnavailable):
        meeting_ai.parse_response('{"decisions": ["안 닫힌 문자열]}')
