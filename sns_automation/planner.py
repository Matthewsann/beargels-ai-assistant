"""기획·분석 에이전트 (Planner/Analyst).

파이프라인의 '양끝'을 담당한다:
  앞단 — 주간 촬영 체크리스트 생성 (뭘 찍을지 AI가 미리 계획)
  뒷단 — 훅 라이브러리 기록 + 인사이트 스크린샷 분석 → 다음 계획에 반영

데이터는 로컬 JSON으로 저장 (웹 파이프라인과 같은 로컬 기반):
  data/weekly_plan.json   ← 이번 주 촬영 체크리스트
  data/hook_library.json  ← 사용한 훅 + 성과 기록 (피드백 루프의 재료)
  data/insights_log.json  ← 인사이트 분석 기록
"""

import asyncio
import json
import logging
import os
import re
import time

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(__file__))
DATA_DIR = os.getenv("PIPELINE_DATA_DIR") or os.path.join(_ROOT, "data")

PLAN_FILE = os.path.join(DATA_DIR, "weekly_plan.json")
HOOKS_FILE = os.path.join(DATA_DIR, "hook_library.json")
INSIGHTS_FILE = os.path.join(DATA_DIR, "insights_log.json")

# 발행 리듬 추천 (growth_strategy.md — 릴스 주 3~4회, 손님 몰리기 30분 전)
PUBLISH_RHYTHM = "추천 발행: 월·수·금 (점심 러시 30분 전 11:30, 또는 오후 간식 14:30)"

# ── AI 없이도 돌아가는 기본 주제 풀 (크레딧 없을 때 순환 사용) ──
FALLBACK_TOPICS = [
    {
        "title": "크림치즈 듬뿍 바르는 순간",
        "pillar": "메뉴 클로즈업", "template": "T1",
        "hook": "크림치즈, 이만큼 들어갑니다", "menu": "송도 크림치즈 베이글",
        "guide": "크림치즈를 두툼하게 바르는 손동작을 세로로 가까이. 5~10초. 갓 바른 결이 보이게 클로즈업.",
        "publish_day": "월",
    },
    {
        "title": "제철 과일산도 단면",
        "pillar": "메뉴 클로즈업", "template": "T1",
        "hook": "이번 주 신상, 과일 통째로", "menu": "송도 과일 산도",
        "guide": "산도를 반으로 가른 단면을 정면 클로즈업. 크림·과일이 꽉 찬 게 보이게. 천천히 들어올리기.",
        "publish_day": "수",
    },
    {
        "title": "베이글 데우는 법 (꿀팁)",
        "pillar": "정보·팁", "template": "T4",
        "hook": "베이글 겉바속촉 데우는 법", "menu": "송도 베이글",
        "guide": "1·2·3 스텝으로 데우는 과정. 각 스텝 짧게. 마지막에 겉바속촉 단면.",
        "publish_day": "금",
    },
    {
        "title": "사장 vs 알바 대결",
        "pillar": "비하인드", "template": "T5",
        "hook": "누가 제일 잘 썰까?", "menu": "베어글스 송도",
        "guide": "같은 재료를 여러 명이 써는 걸 각각 짧게. 대결 구도. 표정·손 클로즈업 섞기.",
        "publish_day": "월",
    },
    {
        "title": "말차 소금크림 라떼",
        "pillar": "신메뉴", "template": "T2",
        "hook": "단짠, 말차에 소금크림", "menu": "말차 소금크림 라떼",
        "guide": "소금크림 얹는 순간 + 잔에 붓는 컷. 위에서 아래로 층 보이게.",
        "publish_day": "수",
    },
    {
        "title": "잠봉뵈르 베이글",
        "pillar": "메뉴 클로즈업", "template": "T1",
        "hook": "송도에서 이 조합은 여기뿐", "menu": "잠봉뵈르 베이글",
        "guide": "잠봉·버터 층이 보이게 단면 클로즈업. 한 입 베어무는 컷 있으면 더 좋음.",
        "publish_day": "금",
    },
    {
        "title": "송도 감성 매장 한 바퀴",
        "pillar": "매장 분위기", "template": "T3",
        "hook": "송도에 이런 카페가 있어요", "menu": "베어글스 송도",
        "guide": "입구→좌석→진열대 순으로 천천히 걸으며 촬영. 창가 빛 좋은 시간대에.",
        "publish_day": "월",
    },
    {
        "title": "오픈 준비 비하인드",
        "pillar": "비하인드", "template": "T5",
        "hook": "아침 7시, 베이글 굽는 중", "menu": "베어글스 송도",
        "guide": "반죽·굽기·진열 과정을 짧게 여러 컷. 김 나는 갓 구운 베이글 필수.",
        "publish_day": "수",
    },
]


# ── 저장/불러오기 ──

def _load(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _save(path: str, data) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── 주간 촬영 체크리스트 ──

def get_plan() -> dict:
    return _load(PLAN_FILE, {"created": 0, "items": []})


def save_plan(plan: dict) -> None:
    _save(PLAN_FILE, plan)


def mark_plan_item(index: int, project_id: str) -> None:
    plan = get_plan()
    if 0 <= index < len(plan.get("items", [])):
        plan["items"][index]["project_id"] = project_id
        save_plan(plan)


# ── 훅 라이브러리 ──

def get_hook_library() -> list[dict]:
    return _load(HOOKS_FILE, [])


def record_hook(project_id: str, title: str, hook: str, file: str) -> dict:
    """완성본 저장 시 자동 기록. 발행/성과는 나중에 채운다."""
    lib = get_hook_library()
    entry = {
        "id": f"h{int(time.time())}{len(lib)}",
        "project_id": project_id, "title": title, "hook": hook,
        "file": file, "created": int(time.time()),
        "published": False, "published_at": 0,
        "reach": None, "saves": None, "shares": None, "likes": None,
    }
    lib.append(entry)
    _save(HOOKS_FILE, lib)
    return entry


def mark_published(project_id: str) -> None:
    lib = get_hook_library()
    for e in lib:
        if e["project_id"] == project_id and not e["published"]:
            e["published"] = True
            e["published_at"] = int(time.time())
    _save(HOOKS_FILE, lib)


def record_result(entry_id: str, reach=None, saves=None, shares=None, likes=None) -> bool:
    lib = get_hook_library()
    for e in lib:
        if e["id"] == entry_id:
            for k, v in (("reach", reach), ("saves", saves),
                         ("shares", shares), ("likes", likes)):
                if v is not None:
                    e[k] = v
            _save(HOOKS_FILE, lib)
            return True
    return False


def _hook_summary() -> str:
    """AI 프롬프트에 넣을 훅 성과 요약 (피드백 루프)."""
    lib = [e for e in get_hook_library() if e["published"]]
    if not lib:
        return "(아직 발행 기록 없음)"
    lines = []
    for e in lib[-15:]:
        perf = []
        for k, label in (("reach", "도달"), ("saves", "저장"),
                         ("shares", "공유"), ("likes", "좋아요")):
            if e.get(k) is not None:
                perf.append(f"{label} {e[k]}")
        lines.append(f"- 「{e['hook']}」 ({e['title']}) — {', '.join(perf) or '성과 미입력'}")
    return "\n".join(lines)


# ── 인사이트 기록 ──

def get_insights_log() -> list[dict]:
    return _load(INSIGHTS_FILE, [])


def add_insight(note: str, summary: str, ai: bool) -> dict:
    log = get_insights_log()
    entry = {"date": int(time.time()), "note": note, "summary": summary, "ai": ai}
    log.append(entry)
    _save(INSIGHTS_FILE, log)
    return entry


def _insights_summary() -> str:
    log = get_insights_log()
    if not log:
        return "(아직 인사이트 분석 기록 없음)"
    return "\n\n".join(e["summary"] for e in log[-3:] if e.get("summary"))


# ── AI 호출 (Claude) ──

_PLAN_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "릴스 주제 (짧게)"},
                        "pillar": {"type": "string", "description": "콘텐츠 기둥 (메뉴 클로즈업/신메뉴/매장 분위기/비하인드/정보·팁/후기)"},
                        "template": {"type": "string", "description": "템플릿 T1~T5"},
                        "hook": {"type": "string", "description": "첫 3초 훅 자막 (짧고 강하게)"},
                        "menu": {"type": "string", "description": "하단 자막용 메뉴/키워드"},
                        "guide": {"type": "string", "description": "촬영 가이드 — 뭘 어떤 각도로 몇 초 찍을지 구체적으로"},
                        "publish_day": {"type": "string", "description": "추천 발행 요일 (월/수/금 중)"},
                    },
                    "required": ["title", "pillar", "template", "hook", "menu", "guide", "publish_day"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["items"],
        "additionalProperties": False,
    },
}

_HOOKS_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "hooks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "훅 자막 후보 3개, 각각 다른 각도(호기심/숫자·구체성/지역·한정)",
            },
        },
        "required": ["hooks"],
        "additionalProperties": False,
    },
}

_INSIGHT_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "핵심 진단 2~3문장 (사장님이 읽을 쉬운 말)"},
            "works_well": {"type": "array", "items": {"type": "string"}, "description": "잘 되고 있는 것"},
            "improve": {"type": "array", "items": {"type": "string"}, "description": "다음 주에 바꿀 것 (구체적 행동)"},
        },
        "required": ["summary", "works_well", "improve"],
        "additionalProperties": False,
    },
}


def _knowledge() -> str:
    from .caption_generator import _load_knowledge
    return _load_knowledge() or "(지침 없음)"


def _market() -> str:
    """메타 그래프로 수집한 '지금 잘 되는 게시물' 요약. 없으면 빈 문자열.

    `python -m sns_automation.market_scan` 을 돌려야 채워진다
    (설정: docs/meta_graph_setup.md).
    """
    try:
        from .market_scan import as_prompt_context
        return as_prompt_context()
    except Exception as e:  # 데이터가 없거나 모듈 문제여도 기획은 계속돼야 한다
        logger.debug("시장 스캔 컨텍스트 없음: %s", e)
        return ""


def _json_from(text: str) -> dict:
    """모델이 준 답에서 JSON 을 관대하게 꺼낸다 (```json 울타리·앞뒤 설명 무시)."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("응답에 JSON이 없습니다: " + text[:120])
    return json.loads(text[a:b + 1])


async def _ask(system: str, user: str, schema: dict,
               images: list[tuple[str, bytes]] | None = None) -> dict:
    """AI에게 물어 schema 모양의 JSON을 받는다.

    유료 Claude API 는 쓰지 않는다(사장님 지시 2026-08-30) —
    llm.complete(only=("gemini",)) 라 무료 Gemini 가 없으면 그대로 실패하고,
    각 호출부의 폴백(기본 풀/템플릿)으로 떨어진다.
    """
    import llm

    sys_full = (
        f"{system}\n\n"
        "반드시 아래 JSON 스키마에 맞는 **JSON 하나만** 출력한다. "
        "설명·인사말·코드펜스 금지.\n"
        f"{json.dumps(schema, ensure_ascii=False)}"
    )
    text = await asyncio.to_thread(
        llm.complete, system=sys_full, user=user, max_tokens=2048,
        images=images or None, only=("gemini",),
    )
    return _json_from(text)


async def generate_weekly_plan(count: int = 3) -> dict:
    """이번 주 촬영 체크리스트 생성. AI 실패/키 없음 → 기본 풀에서 순환."""
    ai = False
    items: list[dict] = []
    try:
        system = (
            "너는 베이글 카페 '베어글스 송도점'의 인스타그램 콘텐츠 기획자다.\n"
            "아래 지침·전략과 지금까지의 발행 성과를 근거로, 이번 주에 촬영할 "
            f"릴스 {count}개를 계획한다. 사장님 혼자 반나절 배치 촬영으로 소화할 수 "
            "있어야 한다. 성과가 좋았던 훅/포맷은 변형해 재활용하고, 미입력이면 "
            "콘텐츠 기둥을 골고루 섞는다.\n\n"
            f"[브랜드·전략 지침]\n{_knowledge()}\n\n"
            f"[지금까지 발행한 훅과 성과]\n{_hook_summary()}\n\n"
            f"[최근 인사이트 분석]\n{_insights_summary()}\n\n"
            f"{_market()}"
        )
        data = await _ask(system, f"이번 주 촬영 체크리스트 {count}개를 만들어줘.", _PLAN_SCHEMA)
        items = data["items"][:count]
        ai = True
    except Exception as e:
        logger.warning("AI 주간 계획 실패 → 기본 풀 사용: %s", e)
        prev = get_plan()
        offset = int(prev.get("offset", 0))
        pool = FALLBACK_TOPICS
        items = [dict(pool[(offset + i) % len(pool)]) for i in range(count)]

    plan = {
        "created": int(time.time()),
        "ai": ai,
        "offset": (int(get_plan().get("offset", 0)) + count) if not ai else 0,
        "items": items,
    }
    save_plan(plan)
    return plan


async def suggest_hooks(title: str, menu: str, base_hook: str = "") -> dict:
    """같은 소재로 훅 자막 3버전 (1→N 변형용)."""
    try:
        system = (
            "너는 인스타 릴스 훅 카피라이터다. 첫 1.5초에 스크롤을 멈추게 하는 "
            "짧은 화면 자막을 쓴다. 과장 금지('미쳤다','인생' 금지), 12자 내외, "
            "각각 다른 각도로: ①호기심 ②숫자·구체성 ③지역(송도)·한정.\n\n"
            f"[브랜드 지침 요약]\n{_knowledge()[:3000]}\n\n"
            f"[성과가 좋았던 훅 참고]\n{_hook_summary()}\n\n"
            f"{_market()}"
        )
        user = f"주제: {title}\n메뉴: {menu}\n"
        if base_hook:
            user += f"현재 훅: {base_hook}\n"
        user += "훅 자막 후보 3개를 만들어줘."
        data = await _ask(system, user, _HOOKS_SCHEMA)
        return {"ai": True, "hooks": data["hooks"][:3]}
    except Exception as e:
        logger.warning("AI 훅 추천 실패 → 기본 변형 사용: %s", e)
        m = (menu or title).removeprefix("송도 ").strip()
        hooks = [h for h in [base_hook] if h]
        hooks += [f"이 {m}, 송도에서만", f"{m}, 오늘도 갓 준비했어요"]
        return {"ai": False, "hooks": hooks[:3]}


async def analyze_insights(images: list[bytes], note: str = "") -> dict:
    """인사이트 스크린샷(들) 분석 → 요약/개선점. 기록에 저장(다음 계획에 반영됨)."""
    try:
        from .caption_generator import _prepare_image
        system = (
            "너는 베이글 카페 '베어글스 송도점'의 인스타그램 성과 분석가다.\n"
            "사장님이 올린 인스타 인사이트 스크린샷을 읽고, 쉬운 말로 진단한다.\n"
            "알고리즘 기준: 공유 > 시청지속(훅) > 저장 > 좋아요. "
            "숫자를 근거로 말하고, 다음 주에 할 행동을 구체적으로 제안한다.\n\n"
            f"[전략 지침]\n{_knowledge()[:4000]}\n\n"
            f"[발행한 훅 기록]\n{_hook_summary()}"
        )
        imgs = [_prepare_image(b) for b in images if b]
        user = "인사이트 스크린샷을 분석해줘."
        if note:
            user += f"\n사장님 메모: {note}"
        data = await _ask(system, user, _INSIGHT_SCHEMA, images=imgs)
        summary = data["summary"]
        if data.get("works_well"):
            summary += "\n\n잘 되는 것:\n" + "\n".join(f"• {w}" for w in data["works_well"])
        if data.get("improve"):
            summary += "\n\n다음 주에 바꿀 것:\n" + "\n".join(f"• {w}" for w in data["improve"])
        add_insight(note, summary, ai=True)
        return {"ai": True, "summary": summary}
    except Exception as e:
        logger.warning("AI 인사이트 분석 실패: %s", e)
        summary = "(AI 분석 실패 — 크레딧/키 확인. 메모만 기록해뒀어요.)"
        add_insight(note, note or summary, ai=False)
        return {"ai": False, "summary": summary}
