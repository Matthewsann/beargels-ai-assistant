"""경영 대시보드 잠금 — 열 때마다 비밀번호 (사장님 지시 2026-09-08).

계약:
  · 메뉴는 누구에게나 보이고, /sales 를 열면 **언제나** 비밀번호 화면(200)
  · 맞는 비밀번호 → 그 응답으로 대시보드를 바로 그린다(리다이렉트도 쿠키도 없음)
  · 기억하지 않는다 — 응답에 Set-Cookie 가 없고, 다시 열면 또 묻는다
  · 틀린 비밀번호 → 같은 화면 + 안내
  · 화면 안 월 이동만 30분짜리 서명 토큰으로 통하고, 낡거나 위조면 다시 묻는다
  · 옛 `/sales?k=<키>` 바로열기는 없앴다 — 그래도 비밀번호 화면
DB 를 안 쓰도록 OWNER_KEY 환경변수로 키를 준다.
"""
import importlib
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# service/app.py 가 `import schedule_page` 로 옆 파일을 부른다 (다른 테스트와 같은 방식)
if str(ROOT / "service") not in sys.path:
    sys.path.insert(0, str(ROOT / "service"))

KEY, PW = "testkey", "080808"
LOCK_MARK = 'name="pw"'


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setenv("SERVICE_PATH", KEY)
    monkeypatch.setenv("OWNER_KEY", PW)
    import service.app as m
    importlib.reload(m)
    m.app.testing = True
    return m


@pytest.fixture
def fake_dash(svc, monkeypatch):
    """대시보드 조립은 DB 를 쓰므로 가짜로 바꾼다 — 여기 관심사는 잠금뿐."""
    monkeypatch.setattr(svc, "render_template",
                        lambda name, **kw: f"<!--{name}-->" + (
                            LOCK_MARK if name == "sales_lock.html" else "TABS"))
    return svc


def test_그냥_열면_언제나_비밀번호_화면(svc):
    r = svc.app.test_client().get(f"/{KEY}/sales")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert LOCK_MARK in body and "/sales/unlock" in body
    assert "맞지 않아요" not in body
    assert "Set-Cookie" not in r.headers


def test_틀린_비밀번호는_안내만(svc):
    r = svc.app.test_client().post(f"/{KEY}/sales/unlock", data={"pw": "123456"})
    assert r.status_code == 200
    assert "비밀번호가 맞지 않아요" in r.get_data(as_text=True)
    assert "bg_owner=" not in (r.headers.get("Set-Cookie") or "")


def test_맞는_비밀번호는_그_자리에서_대시보드(fake_dash):
    r = fake_dash.app.test_client().post(f"/{KEY}/sales/unlock", data={"pw": PW})
    assert r.status_code == 200
    assert "sales.html" in r.get_data(as_text=True)
    # 기억하지 않는다 — 쿠키를 심지 않고 캐시도 막는다
    assert "bg_owner=" not in (r.headers.get("Set-Cookie") or "")
    assert "no-store" in r.headers.get("Cache-Control", "")


def test_한_번_열어도_다음에_또_묻는다(fake_dash):
    c = fake_dash.app.test_client()
    assert "sales.html" in c.post(f"/{KEY}/sales/unlock", data={"pw": PW}).get_data(as_text=True)
    again = c.get(f"/{KEY}/sales")          # 같은 클라이언트(쿠키 유지)로 다시
    assert "sales_lock.html" in again.get_data(as_text=True)


def test_옛_k_주소로는_안_열린다(svc):
    r = svc.app.test_client().get(f"/{KEY}/sales?k={PW}")
    assert r.status_code == 200
    assert LOCK_MARK in r.get_data(as_text=True)


def test_화면_안_월이동은_토큰으로_통한다(fake_dash):
    tok = fake_dash._nav_token(PW)
    r = fake_dash.app.test_client().post(f"/{KEY}/sales/unlock",
                                         data={"t": tok, "ym": "2026-07"})
    assert "sales.html" in r.get_data(as_text=True)


def test_낡거나_위조된_토큰은_다시_묻는다(fake_dash):
    old = fake_dash._nav_token(PW, born=int(time.time()) - fake_dash.NAV_TOKEN_SECONDS - 10)
    forged = fake_dash._nav_token("wrongkey")
    c = fake_dash.app.test_client()
    for bad in (old, forged, "garbage", ""):
        r = c.post(f"/{KEY}/sales/unlock", data={"t": bad})
        assert "sales_lock.html" in r.get_data(as_text=True), bad


def test_목표_저장은_살아있는_토큰이_있어야(fake_dash, monkeypatch):
    monkeypatch.setattr(fake_dash.mkt_store, "set_sales_goal",
                        lambda ym, s, d: {ym: {"store": s, "delivery": d}})
    c = fake_dash.app.test_client()
    bad = c.post(f"/{KEY}/sales/goal", json={"ym": "2026-09", "store": 1})
    assert bad.status_code == 401
    ok = c.post(f"/{KEY}/sales/goal",
                json={"t": fake_dash._nav_token(PW), "ym": "2026-09", "store": 1})
    assert ok.status_code == 200 and ok.get_json()["ok"] is True


def test_사이드바에_메뉴가_항상_보인다(svc):
    body = svc.app.test_client().get(f"/{KEY}/sales").get_data(as_text=True)
    assert "📊 경영 대시보드" in body
