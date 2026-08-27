"""AI 공급자 장애 대응 회귀 테스트 (2026-08-16 점검).

- 무효 키(401)는 한 번 확인되면 이번 실행 동안 건너뛴다(로그 도배·지연 방지).
- 일시 장애(503·타임아웃)는 한 번 재시도한다 — 바로 템플릿 폴백으로
  떨어지면 멀쩡한 리뷰가 저품질 초안을 받는다.
"""

import pytest

import llm


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(llm, "_AUTH_DEAD", set())
    monkeypatch.setattr(llm, "_COOLDOWN", {})
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)   # 재시도 대기 생략
    yield


def _providers(monkeypatch, names):
    monkeypatch.setattr(llm, "available_providers", lambda: list(names))


def test_auth_dead_provider_is_skipped(monkeypatch):
    calls = {"claude": 0, "gemini": 0}

    def bad(*a):
        calls["claude"] += 1
        raise RuntimeError("Error code: 401 - authentication_error")

    def good(*a):
        calls["gemini"] += 1
        return "답글"

    _providers(monkeypatch, ["claude", "gemini"])
    monkeypatch.setattr(llm, "_CALLERS", {"claude": bad, "gemini": good})
    assert llm.complete(user="안녕") == "답글"
    assert llm.complete(user="안녕") == "답글"
    assert calls["claude"] == 1        # 401 확인 후엔 다시 두드리지 않는다
    assert calls["gemini"] == 2


def test_transient_error_retried_once(monkeypatch):
    calls = {"n": 0}

    def flaky(*a):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Gemini 오류 503: overloaded")
        return "답글"

    _providers(monkeypatch, ["gemini"])
    monkeypatch.setattr(llm, "_CALLERS", {"gemini": flaky})
    assert llm.complete(user="안녕") == "답글"
    assert calls["n"] == 2


def test_hard_error_raises(monkeypatch):
    def broken(*a):
        raise RuntimeError("Gemini 오류 400: bad request")

    _providers(monkeypatch, ["gemini"])
    monkeypatch.setattr(llm, "_CALLERS", {"gemini": broken})
    with pytest.raises(RuntimeError):
        llm.complete(user="안녕")


# --- 우선순위 사다리 (사장님 지시 2026-08-27) -----------------------------
# 제미나이 무료 상위 → 클로드(유료) → 제미나이 무료 하위.
# 돈은 '좋은 공짜가 떨어졌을 때만' 쓴다.

def test_ladder_order_is_free_paid_free(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    assert llm.available_steps() == [
        ("gemini", llm.GEMINI_MODEL),
        ("claude", None),
        ("gemini", llm.GEMINI_FALLBACK_MODEL),
    ]


def test_free_quota_falls_to_claude_not_to_the_weak_model(monkeypatch):
    """상위 무료가 한도 나면 **클로드로** 간다 — 하위 모델로 건너뛰지 않는다."""
    seen = []

    def gemini(system, user, max_tokens, model=None, images=None):
        seen.append(("gemini", model))
        raise RuntimeError("Gemini gemini-flash-latest 무료 한도 소진(429)")

    def claude(system, user, max_tokens, model=None, images=None):
        seen.append(("claude", model))
        return "답글"

    _providers(monkeypatch, ["gemini", "claude"])
    monkeypatch.setattr(llm, "_CALLERS", {"gemini": gemini, "claude": claude})
    assert llm.complete(user="안녕") == "답글"
    assert seen[0] == ("gemini", llm.GEMINI_MODEL)
    assert seen[1][0] == "claude", "한도가 나면 클로드 차례여야 한다"


def test_weak_free_model_is_the_last_resort(monkeypatch):
    """클로드까지 막히면 그제서야 하위 무료 모델."""
    seen = []

    def gemini(system, user, max_tokens, model=None, images=None):
        seen.append(model)
        if model == llm.GEMINI_FALLBACK_MODEL:
            return "답글"
        raise RuntimeError("Gemini 무료 한도 소진(429)")

    def claude(system, user, max_tokens, model=None, images=None):
        raise RuntimeError("Your credit balance is too low")

    _providers(monkeypatch, ["gemini", "claude"])
    monkeypatch.setattr(llm, "_CALLERS", {"gemini": gemini, "claude": claude})
    assert llm.complete(user="안녕") == "답글"
    assert seen == [llm.GEMINI_MODEL, llm.GEMINI_FALLBACK_MODEL]


def test_blocked_step_is_skipped_for_a_while(monkeypatch):
    """한도로 막힌 단계는 잠시 안 두드린다 — 매 호출 느려지지 않게."""
    calls = {"gemini": 0, "claude": 0}

    def gemini(system, user, max_tokens, model=None, images=None):
        calls["gemini"] += 1
        raise RuntimeError("Gemini 무료 한도 소진(429)")

    def claude(system, user, max_tokens, model=None, images=None):
        calls["claude"] += 1
        return "답글"

    _providers(monkeypatch, ["gemini", "claude"])
    monkeypatch.setattr(llm, "_CALLERS", {"gemini": gemini, "claude": claude})
    llm.complete(user="안녕")
    llm.complete(user="안녕")
    assert calls["claude"] == 2
    # 첫 호출에서 상위·하위 무료가 각각 한 번씩 막혔고, 두 번째 호출에선 건너뛴다
    assert calls["gemini"] == 1, "막힌 단계를 매번 다시 두드리면 안 된다"


def test_cooldown_expires_and_free_tier_is_tried_again(monkeypatch):
    """하루가 지나 한도가 풀리면 다시 무료부터 쓴다(영구 차단 아님)."""
    llm._cool_down("gemini", llm.GEMINI_MODEL)
    assert llm._cooling("gemini", llm.GEMINI_MODEL)
    llm._COOLDOWN[("gemini", llm.GEMINI_MODEL)] = llm.time.time() - 1
    assert not llm._cooling("gemini", llm.GEMINI_MODEL)
