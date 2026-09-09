"""화면 동기화 — 한 사람이 바꾼 게 남의 폰에도 보이는가 (2026-09-09).

직원 여럿이 같은 화면을 띄워 두면 한 사람의 체크가 남에겐 안 보였다.
푸시가 안 되는 서버(PythonAnywhere)라 '바뀜 표식'을 두고 화면이 몇 초마다
묻는다. 여기서 지키는 것:
  1) 업무를 쓰는 모든 함수가 표식을 갱신한다 — 하나라도 빠지면 그 변경은
     남의 화면에 영원히 안 보인다.
  2) 묻는 창구는 캐시되지 않고, 모르는 주제는 404.
  3) 동기화 대상 화면이 스니펫을 정말 물고 있다.
"""

import inspect
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TPL = ROOT / "service" / "templates"


# --- 1) 쓰기마다 표식 --------------------------------------------------------

@pytest.mark.parametrize("fn", ["add_task", "update_task", "set_done", "delete_task"])
def test_업무_쓰기_함수는_전부_표식을_갱신한다(fn):
    from database import work_store as wk
    src = inspect.getsource(getattr(wk, fn))
    assert "_touch()" in src, f"work_store.{fn} 이 표식을 안 갱신하면 남의 화면에 안 보인다"


@pytest.mark.parametrize("fn", ["save_tasks", "set_task_done", "update_task"])
def test_회의_할_일_쓰기도_표식을_갱신한다(fn):
    """회의 할 일은 업무 보드에 섞여 보이므로 같은 표식을 써야 한다."""
    from database import meeting_store as mt
    src = inspect.getsource(getattr(mt, fn))
    assert "_touch()" in src, f"meeting_store.{fn} 이 표식을 안 갱신한다"


def test_두_저장소가_같은_주제를_쓴다():
    """보드가 두 표를 합쳐 보이니 표식도 하나여야 한다."""
    from database import meeting_store as mt, work_store as wk
    assert 'touch_version("work")' in inspect.getsource(wk._touch)
    assert 'touch_version("work")' in inspect.getsource(mt._touch)


def test_표식_갱신_실패가_쓰기를_막지_않는다():
    """표식은 부가 기능 — kv 가 잠깐 안 돼도 업무 저장은 돼야 한다."""
    from database import supabase_client as db
    src = inspect.getsource(db.touch_version)
    assert "try:" in src and "except" in src


# --- 2) 묻는 창구 --------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    import sys
    sys.path.insert(0, str(ROOT / "service"))
    from service.app import SERVICE_PATH, app
    if not SERVICE_PATH:
        pytest.skip("SERVICE_PATH 가 없어 화면을 열 수 없음")
    return app.test_client(), SERVICE_PATH


def test_표식_창구는_캐시되지_않는다(client):
    c, key = client
    r = c.get(f"/{key}/version/work")
    assert r.status_code == 200
    assert "no-store" in (r.headers.get("Cache-Control") or "")
    assert isinstance(r.get_json().get("v"), str)


def test_모르는_주제는_404(client):
    c, key = client
    assert c.get(f"/{key}/version/nope").status_code == 404


def test_비밀_주소가_틀리면_404(client):
    c, _ = client
    assert c.get("/wrongkey/version/work").status_code == 404


# --- 3) 화면이 스니펫을 물고 있다 -----------------------------------------------

@pytest.mark.parametrize("page", ["work.html", "work_week.html", "home.html"])
def test_동기화_대상_화면은_스니펫을_포함한다(page):
    src = (TPL / page).read_text(encoding="utf-8")
    assert '{% include "_livesync.html" %}' in src


def test_보드는_편집_중엔_다시_그리지_않는다():
    """편집 창이 열린 채 reload 되면 쓰던 글이 날아간다."""
    src = (TPL / "work.html").read_text(encoding="utf-8")
    assert "window.bgBusy" in src and "taskDlg" in src


def test_스니펫은_안_보일_땐_묻지_않는다():
    """백그라운드 탭이 계속 두드리면 서버 CPU 만 먹는다."""
    src = (TPL / "_livesync.html").read_text(encoding="utf-8")
    assert "document.hidden" in src and "visibilitychange" in src
    assert re.search(r"if \(.*document\.hidden.*\) return", src)
