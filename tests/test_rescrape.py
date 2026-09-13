"""'이 리뷰 다시 긁기' (사장님 요청 2026-09-13).

배경: 사진 유실 사고(2026-08-31, 296건 중 167건 놓침) 뒤로, 화면의 리뷰가
실물과 다른 것 같을 때 전체 수집(수 분)을 돌리지 않고 **그 리뷰 하나만**
플랫폼에서 다시 읽어 덮어쓰는 길이 필요했다.
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
# service/app.py 는 같은 폴더의 schedule_page 등을 평평하게 import 한다
if str(ROOT / "service") not in sys.path:
    sys.path.insert(0, str(ROOT / "service"))


# --- 일꾼 쪽 ---------------------------------------------------------------

def _job(msg="7"):
    return {"id": 99, "kind": "rescrape", "message": msg}


def _row(**kw):
    base = {"id": 7, "platform": "baemin", "review_no": "123",
            "rating": 5, "content": "", "raw": {"text": "", "images": []}}
    base.update(kw)
    return base


@pytest.fixture()
def agent(monkeypatch):
    from worker import agent as ag
    done = {}
    monkeypatch.setattr(ag.db, "worker_ping", lambda *a, **k: None)
    monkeypatch.setattr(ag.db, "log_error", lambda *a, **k: None)
    monkeypatch.setattr(ag.db, "finish_job",
                        lambda jid, st, msg, n: done.update(
                            status=st, message=msg))
    monkeypatch.setattr(ag, "ensure_chrome", lambda: True)
    return ag, done


def _crawler_returning(monkeypatch, ag, revs):
    class _C:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def fetch_reviews(self, **kw):
            return revs

    import crawler.baemin as bm
    monkeypatch.setattr(bm, "BaeminCrawler", _C)


def test_rescrape_overwrites_the_one_review(agent, monkeypatch):
    """최근 목록에서 리뷰번호가 맞는 것을 찾아 그 한 건만 저장한다."""
    ag, done = agent
    fresh = {"platform": "baemin", "review_no": "123", "rating": 5,
             "content": "사진 있는 리뷰", "raw": {"text": "x", "images": ["u"]}}
    saved = []
    monkeypatch.setattr(ag.db, "get_review", lambda rid: _row())
    monkeypatch.setattr(ag.db, "save_reviews", lambda rows: saved.extend(rows))
    _crawler_returning(monkeypatch, ag,
                       [{"platform": "baemin", "review_no": "999"}, fresh])
    ag.run_rescrape_job(_job())
    assert saved == [fresh]                       # 다른 리뷰는 안 건드린다
    assert done["status"] == "done"
    # 결과 문구가 '무엇이 달라졌는지'를 말한다 — 사진 0→1장, 글 갱신
    assert "사진 0→1장" in done["message"]
    assert "리뷰 글 갱신" in done["message"]


def test_rescrape_says_when_nothing_changed(agent, monkeypatch):
    """다시 읽어도 같으면 '원본 그대로'라고 말한다 — 오류가 아니었다는 확인."""
    ag, done = agent
    same = _row()
    monkeypatch.setattr(ag.db, "get_review", lambda rid: dict(same))
    monkeypatch.setattr(ag.db, "save_reviews", lambda rows: len(rows))
    _crawler_returning(monkeypatch, ag, [dict(same)])
    ag.run_rescrape_job(_job())
    assert done["status"] == "done"
    assert "원본 그대로" in done["message"]


def test_rescrape_not_found_points_to_full_collect(agent, monkeypatch):
    """최근 목록에 없으면(오래된 리뷰) 실패 + '전체 리뷰 수집' 안내."""
    ag, done = agent
    monkeypatch.setattr(ag.db, "get_review", lambda rid: _row())
    monkeypatch.setattr(ag.db, "save_reviews",
                        lambda rows: pytest.fail("못 찾았는데 저장하면 안 된다"))
    _crawler_returning(monkeypatch, ag, [{"platform": "baemin",
                                          "review_no": "다른번호"}])
    ag.run_rescrape_job(_job())
    assert done["status"] == "error"
    assert "전체 리뷰 수집" in done["message"]


def test_rescrape_is_interactive_job():
    """직원이 화면 앞에서 기다리는 잡 — 빠른 폴링(1.5초) 대상이어야 한다."""
    from database import supabase_client as db
    assert "rescrape" in db.INTERACTIVE_JOB_KINDS


# --- 웹 쪽 -----------------------------------------------------------------

@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("SERVICE_PATH", "testkey")
    import service.app as app_mod
    monkeypatch.setattr(app_mod, "SERVICE_PATH", "testkey")
    return app_mod, app_mod.app.test_client()


def test_rescrape_route_returns_job_id(client, monkeypatch):
    app_mod, c = client
    monkeypatch.setattr(app_mod.db, "request_rescrape",
                        lambda rid, by=None: {"id": 42})
    res = c.post("/testkey/review/7/rescrape").get_json()
    assert res == {"ok": True, "job_id": 42}


def test_rescrape_state_route(client, monkeypatch):
    app_mod, c = client
    monkeypatch.setattr(app_mod.db, "get_job",
                        lambda jid: {"status": "done", "message": "사진 0→1장"})
    res = c.get("/testkey/rescrape-state/42").get_json()
    assert res["status"] == "done" and "사진" in res["message"]


def test_button_lives_on_staff_page():
    """버튼이 초안 카드에 실제로 있다 — 라우트만 있고 입구가 없으면 못 쓴다."""
    import pathlib
    html = pathlib.Path("service/templates/staff.html").read_text(encoding="utf-8")
    assert html.count("다시 긁기") >= 2          # 민감 카드 + 일반 카드
    assert "function rescrapeIt" in html
