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
