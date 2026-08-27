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


# --- 클로드 키 여러 개 (사장님 전달 2026-08-28) ---------------------------
# 기존 키 크레딧이 마르면 다음 키로 넘어간다. 키가 더 생기면 .env 에
# ANTHROPIC_API_KEY_3, _4 … 로 이어 붙이기만 하면 된다.

def test_keys_are_ordered_and_deduped(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "one")
    monkeypatch.setenv("ANTHROPIC_API_KEY_2", "two")
    monkeypatch.setenv("ANTHROPIC_API_KEY_3", "two")   # 같은 키 중복 입력
    monkeypatch.setenv("ANTHROPIC_API_KEY_4", "three")
    assert llm.claude_keys() == [("ANTHROPIC_API_KEY", "one"),
                                 ("ANTHROPIC_API_KEY_2", "two"),
                                 ("ANTHROPIC_API_KEY_4", "three")]


def test_backup_key_alone_still_enables_claude(monkeypatch):
    """1번 칸이 비어도 예비 키만 있으면 클로드를 쓴다."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY_2", "backup")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("LLM_PROVIDER", "")
    assert llm.available_providers() == ["claude"]


def test_dry_key_falls_to_the_next_key(monkeypatch):
    """크레딧이 마른 키는 그 키만 쉬게 하고 다음 키로 넘어간다."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dry")
    monkeypatch.setenv("ANTHROPIC_API_KEY_2", "fresh")
    used = []

    def once(api_key, system, user, max_tokens, model=None, images=None):
        used.append(api_key)
        if api_key == "dry":
            raise RuntimeError("Your credit balance is too low")
        return "답글"

    monkeypatch.setattr(llm, "_claude_once", once)
    assert llm._call_claude("", "안녕", 100) == "답글"
    assert used == ["dry", "fresh"]
    # 마른 키는 다음 호출에서 건너뛴다 — 매번 두드리면 그만큼 느려진다
    used.clear()
    assert llm._call_claude("", "안녕", 100) == "답글"
    assert used == ["fresh"]


def test_all_keys_dry_raises_so_the_ladder_can_fall_through(monkeypatch):
    """키를 다 써도 안 되면 오류를 올려, 바깥 사다리가 제미나이로 내려간다."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dry1")
    monkeypatch.setenv("ANTHROPIC_API_KEY_2", "dry2")

    def once(api_key, *a, **k):
        raise RuntimeError("Your credit balance is too low")

    monkeypatch.setattr(llm, "_claude_once", once)
    with pytest.raises(RuntimeError):
        llm._call_claude("", "안녕", 100)


def test_non_credit_error_does_not_burn_the_other_keys(monkeypatch):
    """400 같은 오류는 키를 바꿔도 마찬가지 — 헛되이 다 두드리지 않는다."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("ANTHROPIC_API_KEY_2", "b")
    used = []

    def once(api_key, *a, **k):
        used.append(api_key)
        raise RuntimeError("400 invalid_request_error")

    monkeypatch.setattr(llm, "_claude_once", once)
    with pytest.raises(RuntimeError):
        llm._call_claude("", "안녕", 100)
    assert used == ["a"]
