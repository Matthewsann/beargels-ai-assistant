"""AI 공급자 선택 계층 — 어떤 AI를 쓸지 한 곳에서 정한다.

왜 필요한가:
    Claude(Anthropic) 크레딧이 떨어지면 글감 추천·초안 작성·리뷰 답글이 전부 멈춘다.
    공급자를 바꿔 끼울 수 있게 해두면, 무료 등급(Gemini)으로 계속 운영할 수 있고
    결제가 풀리면 자동으로 다시 Claude 를 쓴다.

고르는 순서(.env 기준):
    1) LLM_PROVIDER 가 지정돼 있으면 그것만 쓴다 (claude / gemini)
    2) 아니면 ANTHROPIC_API_KEY → GEMINI_API_KEY 순으로 있는 것을 쓴다
    3) Claude 가 크레딧 부족(400 credit balance)이면 Gemini 로 자동 우회한다

쓰는 쪽은 provider 를 몰라도 된다:
    from llm import complete
    text = complete(system="너는 …", user="…", max_tokens=2000)

Gemini 는 REST 로 직접 호출한다(추가 패키지 설치 불필요 — requests 만 쓴다).
무료 키 발급: https://aistudio.google.com/apikey  (구글 계정, 카드 불필요)
"""

from __future__ import annotations

import logging
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
# 답글은 300~500자짜리 짧은 글이라 깊게 생각할 게 없다. effort 를 낮추면
# 같은 모델에서 품질은 그대로면서 토큰(=돈)과 응답시간이 크게 준다.
# ⚠️ 이름을 BEARGELS_ 로 시작하게 둔다 — CLAUDE_EFFORT 같은 흔한 이름은
# 개발 도구가 쓰는 환경변수와 부딪혀 .env 값이 조용히 무시된다(2026-08-18 실측).
CLAUDE_EFFORT = os.getenv("BEARGELS_LLM_EFFORT", "low")
# 지시문(말투 규칙·배운 것·예시)은 매번 똑같다 → 캐시에 올려 90% 싸게 읽는다.
# 캐시가 걸리려면 앞부분이 1024토큰쯤은 돼야 해서 짧은 지시문엔 안 붙인다.
CACHE_MIN_CHARS = int(os.getenv("CLAUDE_CACHE_MIN_CHARS", "2000"))
# 'gemini-flash-latest' 는 구글이 최신 플래시로 자동 연결해 주는 별칭이라
# 모델 은퇴에 강하다. 구모델은 무료쿼터가 0이 되거나 404 로 사라진다
# (2.5-flash 404, 2.0-flash 쿼터0 — 2026-08-06 실키로 확인).
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_FALLBACK_MODEL = "gemini-3.5-flash-lite"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class NoProviderError(RuntimeError):
    """쓸 수 있는 AI 가 하나도 없을 때."""


def _key(name: str) -> str:
    return (os.getenv(name) or "").strip()


def available_providers() -> list[str]:
    forced = (os.getenv("LLM_PROVIDER") or "").strip().lower()
    if forced in ("claude", "anthropic"):
        return ["claude"] if _key("ANTHROPIC_API_KEY") else []
    if forced == "gemini":
        return ["gemini"] if _key("GEMINI_API_KEY") else []
    out = []
    if _key("ANTHROPIC_API_KEY"):
        out.append("claude")
    if _key("GEMINI_API_KEY"):
        out.append("gemini")
    return out


def provider_name() -> str | None:
    """지금 쓰게 될 AI 이름(없으면 None). 화면에 '무엇으로 쓰는 중'을 보여줄 때 사용."""
    ps = available_providers()
    return ps[0] if ps else None


def available() -> bool:
    return bool(available_providers())


# ---------------------------------------------------------------------------
# 공급자별 호출
# ---------------------------------------------------------------------------

def _call_claude(system: str, user: str, max_tokens: int) -> str:
    from anthropic import Anthropic
    client = Anthropic(api_key=_key("ANTHROPIC_API_KEY"))
    kwargs = {"model": CLAUDE_MODEL, "max_tokens": max_tokens,
              "messages": [{"role": "user", "content": user}]}
    if CLAUDE_EFFORT:
        kwargs["output_config"] = {"effort": CLAUDE_EFFORT}
    if system:
        # 긴 지시문은 캐시 블록으로 보낸다(같은 지시문이 반복되므로).
        kwargs["system"] = ([{"type": "text", "text": system,
                              "cache_control": {"type": "ephemeral"}}]
                            if len(system) >= CACHE_MIN_CHARS else system)
    msg = client.messages.create(**kwargs)
    if getattr(msg, "usage", None) is not None:
        logger.debug("claude 토큰 in=%s cache_read=%s out=%s",
                     msg.usage.input_tokens,
                     getattr(msg.usage, "cache_read_input_tokens", 0),
                     msg.usage.output_tokens)
    return "".join(b.text for b in msg.content if b.type == "text")


# 최신 Gemini(3.x)는 기본으로 '생각(thinking)'을 하는데, 그 토큰이
# maxOutputTokens 예산을 먹어치워 답글이 문장 중간에 잘린다(MAX_TOKENS,
# thoughtsTokenCount 767/800 실측 2026-08-06 — "구린 답글"의 원인).
# thinkingLevel=MINIMAL 로 끄면 정상. 구모델은 이 필드를 모르니(400)
# thinkingBudget=0 → 설정 없음 순으로 폴백한다.
_THINKING_CONFIGS = (
    {"thinkingLevel": "MINIMAL"},
    {"thinkingBudget": 0},
    None,
)


def _call_gemini(system: str, user: str, max_tokens: int, model: str | None = None) -> str:
    model = model or GEMINI_MODEL
    resp = None
    for tc in _THINKING_CONFIGS:
        gen_cfg = {"maxOutputTokens": max_tokens}
        if tc:
            gen_cfg["thinkingConfig"] = tc
        body = {
            "contents": [{"parts": [{"text": user}]}],
            "generationConfig": gen_cfg,
        }
        if system:
            body["system_instruction"] = {"parts": [{"text": system}]}
        resp = requests.post(
            GEMINI_URL.format(model=model),
            params={"key": _key("GEMINI_API_KEY")},
            json=body, timeout=120,
        )
        if resp.status_code != 400:
            break                      # 400(필드 미지원)일 때만 다음 설정 시도
    if resp.status_code in (404, 429) and model != GEMINI_FALLBACK_MODEL:
        # 404 = 모델 이름이 바뀜(구글이 자주 교체한다).
        # 429 = 그 모델의 무료 한도 소진. 한도는 **모델마다 다르다** — 기본
        #       gemini-flash-latest 는 3.7-flash 로 풀려 하루 20건뿐이라
        #       리뷰가 하루 60건씩 들어오면 답글이 곧 멈춘다(2026-08-17 실측).
        #       flash-lite 는 한도가 넉넉해 거기로 넘긴다.
        why = "없음" if resp.status_code == 404 else "무료 한도 소진"
        logger.warning("Gemini 모델 %s %s → %s 로 재시도",
                       model, why, GEMINI_FALLBACK_MODEL)
        return _call_gemini(system, user, max_tokens, GEMINI_FALLBACK_MODEL)
    if resp.status_code != 200:
        raise RuntimeError(f"Gemini 오류 {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    cands = data.get("candidates") or []
    if not cands:
        raise RuntimeError(f"Gemini 응답이 비었습니다: {str(data)[:200]}")
    parts = (cands[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts)
    if not text.strip():
        reason = cands[0].get("finishReason", "")
        raise RuntimeError(f"Gemini 가 빈 답을 보냈습니다(finishReason={reason})")
    return text


_CALLERS = {"claude": _call_claude, "gemini": _call_gemini}


def _is_credit_error(e: Exception) -> bool:
    m = str(e).lower()
    return ("credit balance" in m or "billing" in m
            or "insufficient" in m or "quota" in m or "429" in m)


def _is_auth_error(e: Exception) -> bool:
    """키 자체가 무효(401 등) — 재시도해도 영영 실패한다."""
    m = str(e).lower()
    return ("401" in m or "authentication_error" in m
            or "api key is invalid" in m or "invalid x-api-key" in m)


def _is_transient_error(e: Exception) -> bool:
    """일시 장애(과부하·타임아웃) — 잠깐 뒤 다시 하면 대개 된다."""
    m = str(e).lower()
    return ("503" in m or "500 " in m or "502" in m or "504" in m
            or "overloaded" in m or "timed out" in m or "timeout" in m)


# 키가 무효(401)로 확인된 공급자 — 이 프로세스 동안 다시 시도하지 않는다.
# (무효 키를 매 호출마다 먼저 두드려 로그가 401 로 도배되고 응답도 느려졌다,
#  2026-08-16 점검. 키를 갈아끼우면 일꾼 재시작으로 풀린다.)
_AUTH_DEAD: set = set()


# ---------------------------------------------------------------------------
# 공개 함수
# ---------------------------------------------------------------------------

def complete(system: str = "", user: str = "", max_tokens: int = 1500) -> str:
    """AI 에게 물어 답 텍스트를 받는다. 공급자는 자동 선택 · 실패 시 다음 것으로 넘어간다."""
    providers = available_providers()
    if not providers:
        raise NoProviderError(
            "쓸 수 있는 AI 가 없어요. .env 에 ANTHROPIC_API_KEY 또는 GEMINI_API_KEY 를 넣어주세요. "
            "(Gemini 무료 키: https://aistudio.google.com/apikey)"
        )
    last = None
    for name in providers:
        if name in _AUTH_DEAD:
            continue                    # 무효 키 — 두드리지 않는다(로그 도배 방지)
        # 일시 장애(503·타임아웃)는 잠깐 쉬고 한 번 더 — 바로 템플릿 폴백으로
        # 떨어지면 멀쩡한 리뷰가 저품질 초안을 받는다(2026-08-16 점검).
        for attempt in range(2):
            try:
                return _CALLERS[name](system, user, max_tokens)
            except Exception as e:  # noqa: BLE001
                last = e
                if _is_auth_error(e):
                    _AUTH_DEAD.add(name)
                    logger.warning("%s 키가 무효(401) — 이번 실행 동안 건너뜀. "
                                   "키를 갈아끼우면 일꾼 재시작으로 복구.", name)
                    break
                if _is_transient_error(e) and attempt == 0:
                    logger.warning("%s 일시 장애(%s) → 5초 뒤 재시도",
                                   name, str(e)[:80])
                    time.sleep(5)
                    continue
                if _is_credit_error(e):
                    logger.warning("%s 사용 불가(크레딧/한도) → 다음 공급자로", name)
                break
    if last:
        raise last
    raise NoProviderError("AI 호출에 모두 실패했습니다.")
