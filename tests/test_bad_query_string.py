"""query string 에 UTF-8 로 못 읽는 원문 바이트가 섞여도 안 죽는지 확인.

증상(오류기록 id 88, 2026-08-24): 옛 북마크·기기 인코딩 문제로 query string 에
EUC-KR 등 원문 바이트가 그대로 섞여 오면(예: /todo?plat=<0xb8>), werkzeug 가
request.args 접근 시 UnicodeDecodeError 를 던져 요청 전체가 500 으로 죽었다.

계약: 못 읽는 바이트가 있어도 request.args 접근은 죽지 않는다(그 값만
대체 문자로 바뀌고 나머지 파라미터는 정상 처리된다).
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def app_mod(monkeypatch):
    monkeypatch.setenv("SERVICE_PATH", "testkey")
    import importlib

    import service.app as m
    importlib.reload(m)
    return m


def test_args_survives_non_utf8_bytes(app_mod):
    with app_mod.app.test_request_context(
            "/testkey/todo",
            environ_overrides={"QUERY_STRING": "plat=\xb8"}):
        from flask import request
        # 안 죽고 대체 문자(U+FFFD)로 들어온다 — 예외를 던지지 않는 게 계약.
        assert request.args.get("plat") is not None


def test_args_normal_case_unaffected(app_mod):
    with app_mod.app.test_request_context("/testkey/todo?plat=baemin"):
        from flask import request
        assert request.args.get("plat") == "baemin"
