"""AI 공급자 선택 계층 — 어떤 AI를 쓸지 한 곳에서 정한다.

왜 필요한가:
    Claude(Anthropic) 크레딧이 떨어지면 글감 추천·초안 작성·리뷰 답글이 전부 멈춘다.
    공급자를 바꿔 끼울 수 있게 해두면, 무료 등급(Gemini)으로 계속 운영할 수 있고
    결제가 풀리면 자동으로 다시 Claude 를 쓴다.

고르는 순서 — 사장님 지시(2026-08-27):
    ① 제미나이 무료 **상위** 모델(gemini-flash-latest)  ← 공짜, 품질 좋음
    ② 클로드 API(유료)                                   ← 상위 무료가 한도 나면
    ③ 제미나이 무료 **하위** 모델(flash-lite)            ← 마지막 보루, 한도 넉넉
  즉 돈은 '좋은 공짜가 떨어졌을 때만' 쓴다. 셋 다 막히면 템플릿으로 떨어진다.
  (LLM_PROVIDER 를 지정하면 그 공급자만 쓴다 — claude / gemini)
  한도·크레딧으로 막힌 단계는 잠시 쉬었다(_COOLDOWN_SEC) 다시 시도한다.

쓰는 쪽은 provider 를 몰라도 된다:
    from llm import complete
    text = complete(system="너는 …", user="…", max_tokens=2000)

Gemini 는 REST 로 직접 호출한다(추가 패키지 설치 불필요 — requests 만 쓴다).
무료 키 발급: https://aistudio.google.com/apikey  (구글 계정, 카드 불필요)
"""

from __future__ import annotations

import base64
import logging
import os
import pathlib
import time

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
# 불만·민감 리뷰만은 더 큰 모델로 쓴다. 평범한 감사 답글은 작은 모델로 충분하지만
# (예시를 그대로 따라 쓰면 되니까), 사과·해명이 필요한 글은 문장 하나가
# 가게 평판을 좌우한다 — 여기서 아끼면 남는 게 없다.
CLAUDE_MODEL_SENSITIVE = os.getenv("CLAUDE_MODEL_SENSITIVE", "claude-sonnet-4-6")
# effort(생각 깊이 조절)는 모델마다 받는 게 다르다. Haiku 4.5·Sonnet 4.5 는
# 이 값을 보내면 400 으로 거절한다 → 받는 모델에만 붙인다.
_EFFORT_MODELS = ("opus-5", "opus-4-8", "opus-4-7", "opus-4-6",
                  "sonnet-5", "sonnet-4-6", "fable-5")
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
    """쓸 수 있는 공급자를 **우선순위 순으로**.

    ⚠️ 제미나이가 먼저다 — 무료이기 때문이다(사장님 지시 2026-08-27).
       클로드는 무료 한도가 떨어졌을 때만 쓴다.
    """
    forced = (os.getenv("LLM_PROVIDER") or "").strip().lower()
    if forced in ("claude", "anthropic"):
        return ["claude"] if claude_keys() else []
    if forced == "gemini":
        return ["gemini"] if _key("GEMINI_API_KEY") else []
    out = []
    if _key("GEMINI_API_KEY"):
        out.append("gemini")
    # 유료 클로드는 **명시적 옵트인(LLM_PAID_OK=true / LLM_PROVIDER=claude)**
    # 일 때만 자동 목록에 들어간다 — .env 에 유효한 키가 있다는 것만으로
    # 돈이 나가면 안 된다(사장님 원칙 2026-08-30, 아래 PAID_OK 주석 참고).
    if PAID_OK and claude_keys():   # 1번 키가 비어도 예비 키만 있으면 쓴다
        out.append("claude")
    return out


# 유료 클로드를 자동 폴백에 끼울지 — 기본은 **아니오**.
# 2026-08-27 엔 "무료 상위가 마르면 유료 클로드를 사이에 넣으라"였지만,
# 2026-08-30 사장님이 원칙을 바꿨다: "유료 사용에 의지해서는 안 됨.
# 비용을 줄이고 목적을 달성하면서 유지비를 최소화." .env 에 유효한
# ANTHROPIC 키가 있어도(지금 2개 들어있다) 그것만으로 돈이 나가면 안 된다.
# 유료를 정말 쓰고 싶은 날만 .env 에 LLM_PAID_OK=true (또는
# LLM_PROVIDER=claude 강제)를 넣는다 — 명시적 옵트인.
PAID_OK = os.getenv("LLM_PAID_OK", "").strip().lower() in ("1", "true", "yes")


def available_steps() -> list[tuple[str, str | None]]:
    """실제로 두드릴 순서 — (공급자, 모델). 모델이 None 이면 그쪽 기본 모델.

    기본(무료만): 제미나이 상위(품질 좋음, 하루 20건 남짓) → 제미나이
    하위(flash-lite, 한도 넉넉·글이 무딤). 상위가 마르면 하위로 떨어진다.
    LLM_PAID_OK=true 를 명시한 경우에만 그 사이에 유료 클로드가 낀다:
        제미나이 상위 → (옵트인 시) 클로드 → 제미나이 하위
    """
    names = available_providers()   # 유료 제외는 available_providers 가 한다
    steps: list[tuple[str, str | None]] = [
        ("gemini", GEMINI_MODEL) if n == "gemini" else (n, None) for n in names
    ]
    if "gemini" in names and GEMINI_FALLBACK_MODEL != GEMINI_MODEL:
        steps.append(("gemini", GEMINI_FALLBACK_MODEL))     # 마지막 보루
    return steps


def provider_name() -> str | None:
    """지금 쓰게 될 AI 이름(없으면 None). 화면에 '무엇으로 쓰는 중'을 보여줄 때 사용."""
    ps = available_providers()
    return ps[0] if ps else None


def available() -> bool:
    return bool(available_providers())


# ---------------------------------------------------------------------------
# 공급자별 호출
# ---------------------------------------------------------------------------

def claude_keys() -> list[tuple[str, str]]:
    """쓸 수 있는 클로드 키를 **쓸 순서대로** [(환경변수 이름, 키), ...].

    앞의 키가 크레딧이 마르면 다음 키로 넘어간다(사장님 지시 2026-08-28:
    "기존 크레딧 다 떨어지면 새 키를 다음 걸로"). 키를 더 받으면 .env 에
    ANTHROPIC_API_KEY_3, _4 … 로 이어 붙이기만 하면 된다.
    """
    out, seen = [], set()
    names = ["ANTHROPIC_API_KEY"] + [f"ANTHROPIC_API_KEY_{i}"
                                     for i in range(2, 10)]
    for name in names:
        k = _key(name)
        if k and k not in seen:      # 같은 키를 두 번 두면 헛되이 두 번 두드린다
            seen.add(k)
            out.append((name, k))
    return out


def _call_claude(system: str, user: str, max_tokens: int, model=None,
                 images: list | None = None) -> str:
    """클로드에 묻는다. 키가 여럿이면 **살아 있는 키를 찾아** 쓴다.

    크레딧이 마른 키·무효한 키는 그 키만 잠시 쉬게 하고(_cool_down) 다음
    키로 넘어간다. 키를 전부 써도 안 되면 마지막 오류를 올려, 바깥 사다리가
    제미나이 하위 모델로 내려가게 한다.
    """
    keys = claude_keys()
    if not keys:
        raise NoProviderError("클로드 키가 없습니다(.env ANTHROPIC_API_KEY).")
    last = None
    for env_name, api_key in keys:
        if _cooling("claude", env_name):
            continue                 # 이 키는 방금 마른 걸 확인했다
        try:
            return _claude_once(api_key, system, user, max_tokens, model, images)
        except Exception as e:  # noqa: BLE001
            last = e
            if _is_credit_error(e) or _is_auth_error(e):
                _cool_down("claude", env_name)
                why = "크레딧 소진" if _is_credit_error(e) else "키 무효"
                nxt = "다음 키로" if env_name != keys[-1][0] else "남은 키 없음"
                logger.warning("클로드 %s %s → %s", env_name, why, nxt)
                continue
            raise                    # 그 밖의 오류는 키를 바꿔도 마찬가지다
    raise last


def _claude_once(api_key: str, system: str, user: str, max_tokens: int,
                 model=None, images: list | None = None) -> str:
    """키 하나로 한 번 부른다(키 고르기는 _call_claude 가 한다)."""
    from anthropic import Anthropic
    client = Anthropic(api_key=api_key)
    model = model or CLAUDE_MODEL
    if images:
        content = [{"type": "image",
                    "source": {"type": "base64", "media_type": mime,
                               "data": base64.b64encode(raw).decode()}}
                   for mime, raw in images]
        content.append({"type": "text", "text": user})
    else:
        content = user
    kwargs = {"model": model, "max_tokens": max_tokens,
              "messages": [{"role": "user", "content": content}]}
    if CLAUDE_EFFORT and any(m in model for m in _EFFORT_MODELS):
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


def gemini_keys() -> list[tuple[str, str]]:
    """쓸 수 있는 제미나이 무료 키를 순서대로 [(환경변수 이름, 키), ...].

    무료 한도(특히 상위 flash-latest 는 하루 20건 남짓)는 **키마다 따로**
    잡힌다 — 키를 하나 더 만들면(무료, 카드 불필요) 한도가 두 배가 된다.
    유료에 의지하지 않고 품질을 지키는 가장 싼 길이라(사장님 지시 2026-08-30:
    유지비 최소화), .env 에 GEMINI_API_KEY_2, _3 … 을 이어 붙이면 429 때
    자동으로 다음 키로 넘어간다. 클로드 키 로테이션과 같은 방식.
    """
    out, seen = [], set()
    names = ["GEMINI_API_KEY"] + [f"GEMINI_API_KEY_{i}" for i in range(2, 10)]
    for name in names:
        k = _key(name)
        if k and k not in seen:
            seen.add(k)
            out.append((name, k))
    return out


def _call_gemini(system: str, user: str, max_tokens: int, model: str | None = None,
                 images: list | None = None) -> str:
    """키가 여럿이면 한도가 남은 키를 찾아 쓴다 — 전부 마르면 429 를 올린다."""
    keys = gemini_keys()
    if not keys:
        raise NoProviderError("제미나이 키가 없습니다(.env GEMINI_API_KEY).")
    last = None
    for env_name, api_key in keys:
        if _cooling("gemini-key", (model or GEMINI_MODEL, env_name)):
            continue                    # 이 키는 방금 이 모델 한도가 마른 걸 확인
        try:
            return _gemini_once(api_key, system, user, max_tokens, model, images)
        except Exception as e:  # noqa: BLE001
            last = e
            if "한도 소진(429)" in str(e):
                _cool_down("gemini-key", (model or GEMINI_MODEL, env_name))
                nxt = ("다음 키로" if env_name != keys[-1][0]
                       else "남은 키 없음 — 사다리로")
                logger.warning("제미나이 %s 한도 소진(%s) → %s",
                               env_name, model or GEMINI_MODEL, nxt)
                continue
            raise                        # 그 밖의 오류는 키를 바꿔도 마찬가지다
    raise last


def _gemini_once(api_key: str, system: str, user: str, max_tokens: int,
                 model: str | None = None, images: list | None = None) -> str:
    model = model or GEMINI_MODEL
    parts = [{"inline_data": {"mime_type": mime,
                              "data": base64.b64encode(raw).decode()}}
             for mime, raw in (images or [])]
    parts.append({"text": user})
    resp = None
    for tc in _THINKING_CONFIGS:
        gen_cfg = {"maxOutputTokens": max_tokens}
        if tc:
            gen_cfg["thinkingConfig"] = tc
        body = {
            "contents": [{"parts": parts}],
            "generationConfig": gen_cfg,
        }
        if system:
            body["system_instruction"] = {"parts": [{"text": system}]}
        resp = requests.post(
            GEMINI_URL.format(model=model),
            params={"key": api_key},
            json=body, timeout=120,
        )
        if resp.status_code != 400:
            break                      # 400(필드 미지원)일 때만 다음 설정 시도
    if resp.status_code in (404, 429):
        # 404 = 모델 이름이 바뀜(구글이 자주 교체한다).
        # 429 = 그 모델의 무료 한도 소진. 한도는 **모델마다 다르다** — 기본
        #       gemini-flash-latest 는 3.7-flash 로 풀려 하루 20건뿐이라
        #       리뷰가 하루 60건씩 들어오면 답글이 곧 멈춘다(2026-08-17 실측).
        #
        # ⚠️ 여기서 바로 flash-lite 로 떨어지지 않는다. 그렇게 하면 상위 무료가
        #    마르는 순간 **유료 클로드를 건너뛰고** 제일 무딘 모델로 가버린다
        #    (사장님 지시 2026-08-27: 좋은 무료 → 클로드 → 낮은 무료).
        #    올려 보내면 complete() 의 사다리가 다음 단으로 넘긴다.
        why = "모델 없음(404)" if resp.status_code == 404 else "무료 한도 소진(429)"
        raise RuntimeError(f"Gemini {model} {why}: {resp.text[:200]}")
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


def _call_gemini_any(system, user, max_tokens, model=None, images=None):
    """gemini 는 claude 쪽 모델 이름을 받지 않는다 — 그런 이름은 무시한다.

    사다리(available_steps)가 넘겨주는 'gemini-…' 이름은 그대로 쓴다 —
    상위/하위 무료 모델을 가르는 게 그 이름이다.
    """
    if model and not str(model).startswith("gemini"):
        model = None
    return _call_gemini(system, user, max_tokens, model, images)


_CALLERS = {"claude": _call_claude, "gemini": _call_gemini_any}


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


# 한도·크레딧으로 막힌 단계는 잠시 건너뛴다. 안 그러면 답글 한 건마다 죽은
# 단계를 먼저 두드려(429 한 번, 402 한 번) 매번 느려진다. 무료 한도는 하루가
# 지나면 풀리고 크레딧은 충전하면 풀리므로 **영구 차단은 하지 않는다** —
# 잠깐 쉬었다 다시 올라가 본다(기본 20분).
_COOLDOWN_SEC = int(os.getenv("LLM_COOLDOWN_SEC", "1200"))
_COOLDOWN: dict = {}


def _cooling(name: str, model: str | None) -> bool:
    until = _COOLDOWN.get((name, model))
    return bool(until and time.time() < until)


def _cool_down(name: str, model: str | None) -> None:
    _COOLDOWN[(name, model)] = time.time() + _COOLDOWN_SEC


# ---------------------------------------------------------------------------
# 공개 함수
# ---------------------------------------------------------------------------

def complete(system: str = "", user: str = "", max_tokens: int = 1500,
             model: str | None = None, images: list | None = None,
             prefer: str | None = None, only: tuple[str, ...] | None = None,
             quality: bool = False, paid: bool = False) -> str:
    """AI 에게 물어 답 텍스트를 받는다. 공급자는 자동 선택 · 실패 시 다음 것으로 넘어간다.

    model: 이번 호출에만 쓸 Claude 모델(없으면 CLAUDE_MODEL). 불만 리뷰처럼
           품질이 중요한 곳에서 더 큰 모델을 지정하는 데 쓴다.
    images: 함께 보여줄 사진 [(mime, 바이트), ...]. 블로그 사진함 태깅처럼
            'AI 가 사진을 실제로 보고 판단해야' 하는 곳에서 쓴다.
            → 편하게 쓰려면 see() 를 부르면 파일 경로만 넘겨도 된다.
    prefer: 이 호출만 특정 공급자를 먼저 쓴다("gemini" 등). 무료 등급으로 충분한
            작업이 Claude 크레딧을 갉아먹지 않게 하는 용도. 그 공급자가 없거나
            실패하면 평소 순서(무료→유료)로 넘어간다.
    only: 이 목록에 있는 공급자만 쓴다 — 없거나 실패하면 **다른 공급자로 넘어가지
          않고 그대로 실패한다**. 회의 AI 정리처럼 "무료가 아니면 아예 안 쓴다"가
          확정된 기능에 쓴다(사장님 지시 2026-08-30). only=("gemini",) 로 부르면
          유료 클로드는 절대 두드리지 않는다 — prefer 와 달리 새는 구멍이 없다.
    quality: True = **손님에게 나가는 글**(리뷰 답글). 좋은 무료 모델부터,
          마르면 사장님이 정한 사다리대로(무료상위→클로드→무료하위).
          False(기본) = 내부용 대량 작업(블로그·회의·소개글). **무료 하위
          모델부터** 쓰고 유료는 아예 안 두드린다 — 상위 무료의 하루 한도
          (~20건)를 답글 몫으로 아껴 두기 위해서다. 예전엔 블로그가 아침에
          상위 한도를 먼저 태워, 정작 답글이 무딘 하위 모델로 밀렸다
          (2026-08-30, 초안 무수정률 85%→13% 붕괴의 배경. 사장님 지시:
          유료에 의지하지 말고 유지비 최소화로 목적 달성).
    paid: True = **이 호출은 유료 클로드를 써도 된다**는 호출부의 명시적 옵트인.
          전역 LLM_PAID_OK 와 달리 이 호출에만 적용된다. 인스타 릴스처럼
          "유료만 쓰고 무료 한도는 답글 몫으로 남긴다"(사장님 확정 2026-08-30)가
          정해진 기능이 only=("claude",) 와 함께 쓴다. 무료 사다리(답글·블로그)는
          건드리지 않는다.
    """
    steps = available_steps()
    if not quality:
        # 대량 작업: 하위 무료 먼저, 상위 무료는 예비, 유료는 제외.
        gem = [s for s in steps if s[0] == "gemini"]
        # 제미나이가 없으면 **그냥 비운다**(예전엔 `or steps` 로 원래 사다리로
        # 되돌아갔다). 그 폴백은 유료 옵트인이 켜진 기기에서 제미나이 키만
        # 빠지면 블로그·사진 태깅 같은 대량 작업이 통째로 유료로 새는 구멍이었다
        # (2026-08-30 비용 감사). 무료가 없으면 내부 작업은 멈추고 알린다.
        # 인스타처럼 유료를 쓰기로 정한 호출은 아래 paid 분기가 다시 채운다.
        steps = sorted(gem, key=lambda s: 0 if s[1] == GEMINI_FALLBACK_MODEL
                       else 1)
    if paid and claude_keys() and not any(s[0] == "claude" for s in steps):
        # 호출 단위 유료 옵트인 — 전역 게이트(PAID_OK)를 이 호출에만 연다.
        # quality 분기 뒤에 두어 '대량 작업' 정리에 지워지지 않게 한다.
        steps.append(("claude", None))
    if only:
        steps = [s for s in steps if s[0] in only]
    if prefer:
        steps = ([s for s in steps if s[0] == prefer]
                 + [s for s in steps if s[0] != prefer])
    if not steps:
        raise NoProviderError(
            "쓸 수 있는 AI 가 없어요. .env 에 ANTHROPIC_API_KEY 또는 GEMINI_API_KEY 를 넣어주세요. "
            "(Gemini 무료 키: https://aistudio.google.com/apikey)"
        )
    last = None
    for name, step_model in steps:
        if name in _AUTH_DEAD:
            continue                    # 무효 키 — 두드리지 않는다(로그 도배 방지)
        if _cooling(name, step_model):
            continue                    # 한도/크레딧으로 막힌 단계 — 잠시 쉰다
        use_model = step_model or model
        # 일시 장애(503·타임아웃)는 잠깐 쉬고 한 번 더 — 바로 템플릿 폴백으로
        # 떨어지면 멀쩡한 리뷰가 저품질 초안을 받는다(2026-08-16 점검).
        for attempt in range(2):
            try:
                return _CALLERS[name](system, user, max_tokens, use_model, images)
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
                    _cool_down(name, step_model)
                    logger.warning("%s(%s) 사용 불가(크레딧/한도) → 다음 단계로",
                                   name, step_model or "기본")
                break
    if last:
        raise last
    raise NoProviderError("AI 호출에 모두 실패했습니다.")


# ---------------------------------------------------------------------------
# 사진을 보여주며 묻기
# ---------------------------------------------------------------------------

_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
         ".webp": "image/webp", ".gif": "image/gif"}
# 사진 1장이 너무 크면 요청이 무거워지고 무료 한도도 빨리 닳는다.
# 태깅은 '무엇이 찍혔나'만 보면 되므로 긴 변 768px 로 줄여 보낸다.
SEE_MAX_PX = int(os.getenv("LLM_SEE_MAX_PX", "768"))


def _as_jpeg(path, max_px: int = SEE_MAX_PX) -> tuple[str, bytes]:
    """어떤 사진이든 작은 JPEG 바이트로 바꿔 준다(HEIC·회전·초대형 대응)."""
    import io as _io
    from PIL import Image, ImageOps
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)          # 폰 세로사진이 눕는 것 방지
    img.thumbnail((max_px, max_px), Image.LANCZOS)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = _io.BytesIO()
    img.save(buf, "JPEG", quality=82)
    return "image/jpeg", buf.getvalue()


def see(paths, system: str = "", user: str = "", max_tokens: int = 1500,
        model: str | None = None, prefer: str | None = None) -> str:
    """사진 파일 경로들을 보여주며 AI 에게 묻는다.

        llm.see(["a.jpg", "b.HEIC"], user="이 사진들에 뭐가 찍혔는지 알려줘")

    HEIC·초대형·눕는 사진을 알아서 작은 JPEG 로 바꿔 보낸다.
    """
    if isinstance(paths, (str, pathlib.Path)):
        paths = [paths]
    return complete(system=system, user=user, max_tokens=max_tokens, model=model,
                    images=[_as_jpeg(p) for p in paths], prefer=prefer)
