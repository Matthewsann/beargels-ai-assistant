"""회의 'AI로 정리' — 논의 내용에서 결정사항·업무 제안을 뽑는다.

⚠️ 무료 AI(Gemini)만 쓴다 — 유료 클로드로는 절대 넘어가지 않는다
(사장님 지시 2026-08-30, "api key로 유료면 사용 x"). 무료 한도가 찼으면
그냥 실패로 알린다. 회의록 정리에 사장님 API 크레딧을 쓸 이유는 없다는 판단.

worker/agent.py 의 run_meeting_organize_job() 이 이 모듈을 부른다.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

MAX_DECISIONS = 6
MAX_TASKS = 8


class MeetingAIUnavailable(RuntimeError):
    """무료 AI 를 못 썼다(한도 소진·키 없음·응답 이상 등).

    ⚠️ 여기서 잡히면 그대로 실패다 — llm.complete(only=("gemini",)) 자체가
    유료 클로드로 새지 않으므로, 이 예외는 "무료가 안 됐다"만 뜻한다.
    """


_SYSTEM = """너는 카페 회의록에서 실행 가능한 정보를 뽑아내는 보조원이다.
회의 내용(직원이 편하게 적은 메모)을 읽고, 그 안에 이미 담긴 내용만 정리한다.
없는 사실을 지어내지 않는다 — 애매하면 그 항목을 만들지 않는다.

반드시 아래 JSON 형식으로만 답한다(설명·인사말·코드블록 없이 JSON 객체 하나만):
{"decisions": ["결정한 것 한 줄", ...], "tasks": [{"content": "업무 내용", "memo": "짧은 참고(없으면 빈 문자열)"}, ...]}

규칙:
- decisions: 회의에서 실제로 결정됐다고 읽히는 것만 담는다. 없으면 빈 배열.
- tasks: "누가 무엇을 하기로" 류의 실행할 일만. 담당자·마감일은 절대 짐작해서
  채우지 않는다 — 원문에 이름·날짜가 없으면 그 항목 자체는 만들되 담당자·
  마감일 정보는 그냥 두면 된다(응답 형식에 그 칸이 아예 없다).
- 최대 decisions 6개, tasks 8개까지만. 원문에 없는 걸 부풀리지 않는다.
- 한국어로, 회의 원문의 표현을 최대한 그대로 쓴다."""


def organize(meeting: dict) -> dict:
    """meeting(dict: title/attendees/body 키)을 읽어 제안을 뽑는다.

    반환: {"decisions": [str, ...], "tasks": [{"content": str, "memo": str}, ...]}
    본문이 비어 있으면 뽑을 게 없으니 AI 를 부르지 않고 빈 결과를 돌려준다.
    """
    body = (meeting.get("body") or "").strip()
    if not body:
        return {"decisions": [], "tasks": []}

    import llm

    user = (
        f"제목: {meeting.get('title') or ''}\n"
        f"참석자: {meeting.get('attendees') or ''}\n\n"
        f"논의 내용:\n{body}"
    )
    try:
        raw = llm.complete(system=_SYSTEM, user=user, max_tokens=900,
                           only=("gemini",))
    except Exception as e:  # noqa: BLE001
        raise MeetingAIUnavailable(str(e)) from e
    return parse_response(raw)


def parse_response(raw: str) -> dict:
    """AI 응답 텍스트에서 JSON 을 뽑는다 — 코드블록이 섞여 와도 견딘다."""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise MeetingAIUnavailable(f"AI 응답을 이해하지 못했습니다: {raw[:150]}")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise MeetingAIUnavailable(f"AI 응답이 JSON 이 아닙니다: {raw[:150]}") from e

    decisions = [str(d).strip() for d in (data.get("decisions") or [])
                if str(d).strip()][:MAX_DECISIONS]

    tasks = []
    for t in (data.get("tasks") or [])[:MAX_TASKS]:
        if not isinstance(t, dict):
            continue
        content = str(t.get("content") or "").strip()
        if not content:
            continue
        tasks.append({"content": content[:300],
                      "memo": str(t.get("memo") or "").strip()[:500]})
    return {"decisions": decisions, "tasks": tasks}
