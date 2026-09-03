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
    assert llm.complete(user="안녕", quality=True) == "답글"
    assert llm.complete(user="안녕", quality=True) == "답글"
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
    assert llm.complete(user="안녕", quality=True) == "답글"
    assert calls["n"] == 2


def test_busy_step_gets_short_cooldown(monkeypatch):
    """재시도까지 503이면 그 단계를 잠깐 피한다 — 안 그러면 혼잡 시간대에
    재생성 매 건이 아픈 모델부터 두드려 40초~1분씩 걸린다(2026-09-04 실측)."""
    calls = {"gemini": 0, "claude": 0}

    def busy(*a):
        calls["gemini"] += 1
        raise RuntimeError("Gemini 오류 503: overloaded")

    def good(*a):
        calls["claude"] += 1
        return "답글"

    _providers(monkeypatch, ["gemini", "claude"])
    monkeypatch.setattr(llm, "_CALLERS", {"gemini": busy, "claude": good})
    assert llm.complete(user="안녕", quality=True) == "답글"
    assert calls["gemini"] == 2            # 첫 건: 시도 + 재시도까지는 한다
    # 다음 건: 쿨다운 덕에 제미나이를 아예 안 두드리고 바로 건강한 단계로
    assert llm.complete(user="안녕", quality=True) == "답글"
    assert calls["gemini"] == 2
    assert calls["claude"] == 2
    # 쿨다운은 짧다(혼잡은 금방 풀린다) — 한도용 20분짜리가 아니어야 한다
    until = llm._COOLDOWN[("gemini", llm.GEMINI_MODEL)]
    assert until - llm.time.time() <= llm._BUSY_COOLDOWN_SEC + 1


def test_hard_error_raises(monkeypatch):
    def broken(*a):
        raise RuntimeError("Gemini 오류 400: bad request")

    _providers(monkeypatch, ["gemini"])
    monkeypatch.setattr(llm, "_CALLERS", {"gemini": broken})
    with pytest.raises(RuntimeError):
        llm.complete(user="안녕", quality=True)


# --- 우선순위 사다리 (사장님 지시 2026-08-27) -----------------------------
# 제미나이 무료 상위 → 클로드(유료) → 제미나이 무료 하위.
# 돈은 '좋은 공짜가 떨어졌을 때만' 쓴다.

def test_default_ladder_is_free_only(monkeypatch):
    """기본은 **무료만** — .env 에 유효한 클로드 키가 있어도 자동으로는 안 쓴다
    (사장님 원칙 2026-08-30: 유료 사용에 의지 금지, 유지비 최소화)."""
    monkeypatch.setenv("LLM_PROVIDER", "")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setattr(llm, "PAID_OK", False)
    assert llm.available_steps() == [
        ("gemini", llm.GEMINI_MODEL),
        ("gemini", llm.GEMINI_FALLBACK_MODEL),
    ]


def test_paid_optin_restores_free_paid_free_ladder(monkeypatch):
    """LLM_PAID_OK=true 를 명시한 날만 무료→유료→무료 사다리가 된다."""
    monkeypatch.setenv("LLM_PROVIDER", "")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setattr(llm, "PAID_OK", True)
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
    assert llm.complete(user="안녕", quality=True) == "답글"
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
    assert llm.complete(user="안녕", quality=True) == "답글"
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
    llm.complete(user="안녕", quality=True)
    llm.complete(user="안녕", quality=True)
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
    """유료 옵트인 상태에선, 1번 칸이 비어도 예비 키만 있으면 클로드를 쓴다."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY_2", "backup")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("LLM_PROVIDER", "")
    monkeypatch.setattr(llm, "PAID_OK", True)
    assert llm.available_providers() == ["claude"]
    # 옵트인이 없으면 키가 있어도 자동 목록에 안 들어간다
    monkeypatch.setattr(llm, "PAID_OK", False)
    assert llm.available_providers() == []


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


def test_only_never_falls_back_to_excluded_provider(monkeypatch):
    """only=("gemini",) 는 제미나이가 실패해도 클로드(유료)로 넘어가지 않는다.

    회의 AI 정리 기능은 유료 크레딧을 절대 쓰지 않기로 확정됐다
    (사장님 지시 2026-08-30) — prefer 는 실패 시 다른 공급자로 새지만,
    only 는 새지 않아야 그 약속을 지킬 수 있다.
    """
    calls = {"claude": 0, "gemini": 0}

    def bad_gemini(*a):
        calls["gemini"] += 1
        raise RuntimeError("Gemini 오류 429: 무료 한도 소진")

    def claude(*a):
        calls["claude"] += 1
        return "유료로 만든 답"

    _providers(monkeypatch, ["gemini", "claude"])
    # 무료 상위 모델이 마르면 무료 하위 모델로도 한 번 더 내려간다(정상 동작) —
    # 여기서 확인할 건 그 중 어느 단계도 클로드로는 새지 않는다는 것.
    monkeypatch.setattr(llm, "GEMINI_FALLBACK_MODEL", llm.GEMINI_MODEL)
    monkeypatch.setattr(llm, "_CALLERS", {"claude": claude, "gemini": bad_gemini})
    with pytest.raises(Exception):
        llm.complete(user="회의 내용", only=("gemini",))
    assert calls["claude"] == 0        # 유료 쪽은 아예 두드리지 않는다
    assert calls["gemini"] == 1


def test_only_with_no_matching_provider_raises_no_provider_error(monkeypatch):
    """무료 키가 아예 없으면(only 필터 결과가 빈 목록) 조용히 실패로 알려준다."""
    _providers(monkeypatch, ["claude"])       # 클로드만 있고 제미나이는 없음
    with pytest.raises(llm.NoProviderError):
        llm.complete(user="회의 내용", only=("gemini",))


# --- 무료 한도 배분 (사장님 지시 2026-08-30: 유료에 의지 X, 유지비 최소) ----
# 상위 무료(flash-latest, 하루 ~20건)는 손님에게 나가는 **답글 몫**으로 아껴
# 두고, 내부용(블로그·회의·소개글)은 하위 무료부터 + 유료 제외.

def test_bulk_default_goes_lite_first_and_never_pays(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    used = []

    def gemini(system, user, max_tokens, model=None, images=None):
        used.append(("gemini", model))
        return "결과"

    def claude(*a, **k):
        raise AssertionError("내부용 작업이 유료 클로드를 두드렸다")

    monkeypatch.setattr(llm, "_CALLERS", {"gemini": gemini, "claude": claude})
    assert llm.complete(user="블로그 글감") == "결과"
    assert used[0] == ("gemini", llm.GEMINI_FALLBACK_MODEL), "하위 무료부터"


def test_quality_path_keeps_the_owner_ladder(monkeypatch):
    """답글(quality=True)은 사장님이 정한 사다리 그대로 — 상위 무료부터."""
    monkeypatch.setenv("LLM_PROVIDER", "")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    used = []

    def gemini(system, user, max_tokens, model=None, images=None):
        used.append(("gemini", model))
        return "답글"

    monkeypatch.setattr(llm, "_CALLERS", {"gemini": gemini, "claude": lambda *a, **k: "x"})
    assert llm.complete(user="답글", quality=True) == "답글"
    assert used[0] == ("gemini", llm.GEMINI_MODEL), "상위 무료부터"


def test_reply_generation_declares_itself_quality():
    """답글 생성 경로가 quality=True 를 쓰는지 — 안 쓰면 예약이 무너진다."""
    import inspect

    import assistant.beargels as B
    assert "quality=True" in inspect.getsource(B._ask_claude)


def test_gemini_keys_rotate_on_quota(monkeypatch):
    """키가 여럿이면 한도 마른 키를 건너뛰고 다음 키로 — 무료 한도 ×N."""
    monkeypatch.setenv("GEMINI_API_KEY", "k1")
    monkeypatch.setenv("GEMINI_API_KEY_2", "k2")
    used = []

    def once(api_key, *a, **k):
        used.append(api_key)
        if api_key == "k1":
            raise RuntimeError("Gemini gemini-flash-latest 무료 한도 소진(429): x")
        return "답글"

    monkeypatch.setattr(llm, "_gemini_once", once)
    assert llm._call_gemini("", "안녕", 100) == "답글"
    assert used == ["k1", "k2"]
    # 마른 키는 잠시 건너뛴다
    used.clear()
    assert llm._call_gemini("", "안녕", 100) == "답글"
    assert used == ["k2"]


def test_gemini_all_keys_dry_raises_for_the_ladder(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k1")
    monkeypatch.setenv("GEMINI_API_KEY_2", "")

    def once(api_key, *a, **k):
        raise RuntimeError("Gemini gemini-flash-latest 무료 한도 소진(429): x")

    monkeypatch.setattr(llm, "_gemini_once", once)
    with pytest.raises(RuntimeError):
        llm._call_gemini("", "안녕", 100)


def test_bulk_work_never_falls_to_paid_without_gemini(monkeypatch):
    """제미나이 키가 없으면 내부 대량 작업(블로그·사진 태깅)은 **멈춘다** —
    유료로 새지 않는다.

    실사고 구멍(2026-08-30 비용 감사): quality=False 분기가 제미나이 단이
    비면 `or steps` 로 원래 사다리(유료 포함)로 되돌아갔다. 유료 옵트인이
    켜진 기기에서 제미나이 키만 빠지면 블로그 한 번에 ~7회, 사진 태깅은
    묶음마다 유료 호출이 조용히 나갔다.
    """
    monkeypatch.setattr(llm, "PAID_OK", True)          # 유료 옵트인 켜진 상태
    monkeypatch.setattr(llm, "gemini_keys", lambda: [])  # 제미나이 키 없음
    monkeypatch.setattr(llm, "claude_keys", lambda: [("ANTHROPIC_API_KEY", "a")])
    monkeypatch.setattr(llm, "_key", lambda n: "a" if n.startswith("ANTHROPIC") else "")
    called = []
    monkeypatch.setitem(llm._CALLERS, "claude",
                        lambda *a, **k: called.append("claude") or "x")
    with pytest.raises(llm.NoProviderError):
        llm.complete(user="블로그 초안", max_tokens=100)   # quality=False 기본
    assert called == [], "무료가 없는데 유료를 두드렸다"


def test_paid_optin_call_still_works_without_gemini(monkeypatch):
    """반대로, 유료를 쓰기로 정한 호출(인스타)은 제미나이가 없어도 돌아야 한다."""
    monkeypatch.setattr(llm, "PAID_OK", True)
    monkeypatch.setattr(llm, "gemini_keys", lambda: [])
    monkeypatch.setattr(llm, "claude_keys", lambda: [("ANTHROPIC_API_KEY", "a")])
    monkeypatch.setattr(llm, "_key", lambda n: "a" if n.startswith("ANTHROPIC") else "")
    monkeypatch.setitem(llm._CALLERS, "claude", lambda *a, **k: "자막")
    assert llm.complete(user="릴스 자막", only=("claude",), paid=True) == "자막"
