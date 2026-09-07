"""경영 대시보드 잠금 — 비밀번호 화면 (사장님 지시 2026-09-07).

계약:
  · 메뉴는 누구에게나 보이고, 쿠키 없이 /sales 를 열면 404 가 아니라
    비밀번호 화면(200)이 뜬다
  · 틀린 비밀번호 → 같은 화면 + 안내, 쿠키 없음
  · 맞는 비밀번호 → /sales 로 보내고 1년 쿠키(해시)를 심는다
  · 옛 방식 `/sales?k=<키>` 도 그대로 열린다
DB 를 안 쓰도록 OWNER_KEY 환경변수로 키를 준다.
"""
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

KEY, PW = "testkey", "080808"


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setenv("SERVICE_PATH", KEY)
    monkeypatch.setenv("OWNER_KEY", PW)
    import service.app as m
    importlib.reload(m)
    m.app.testing = True
    return m


def test_쿠키_없으면_비밀번호_화면(svc):
    r = svc.app.test_client().get(f"/{KEY}/sales")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'name="pw"' in body and "/sales/unlock" in body
    assert "비밀번호가 맞지 않아요" not in body


def test_틀린_비밀번호는_안내만(svc):
    r = svc.app.test_client().post(f"/{KEY}/sales/unlock", data={"pw": "123456"})
    assert r.status_code == 200
    assert "비밀번호가 맞지 않아요" in r.get_data(as_text=True)
    assert "bg_owner" not in (r.headers.get("Set-Cookie") or "")


def test_맞는_비밀번호는_쿠키_심고_대시보드로(svc):
    r = svc.app.test_client().post(f"/{KEY}/sales/unlock", data={"pw": PW})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/{KEY}/sales")
    cookie = r.headers.get("Set-Cookie") or ""
    assert f"bg_owner={svc._owner_token(PW)}" in cookie
    assert "Max-Age=31536000" in cookie
    assert PW not in cookie                      # 쿠키에 비밀번호 원문은 안 남는다


def test_옛_주소_방식도_그대로(svc):
    r = svc.app.test_client().get(f"/{KEY}/sales?k={PW}")
    assert r.status_code == 302
    assert "bg_owner=" in (r.headers.get("Set-Cookie") or "")


def test_틀린_k는_여전히_404(svc):
    assert svc.app.test_client().get(f"/{KEY}/sales?k=nope").status_code == 404


def test_사이드바에_메뉴가_항상_보인다(svc):
    body = svc.app.test_client().get(f"/{KEY}/sales").get_data(as_text=True)
    assert "📊 경영 대시보드" in body
