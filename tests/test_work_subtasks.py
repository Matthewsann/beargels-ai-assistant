"""업무 보드 — 하위 업무 · 끝낸 업무 관리 (사장님 2026-09-11).

지키는 것:
  1) 하위는 상위 밑으로 묶이고, 상위는 '가장 급한 열린 하위'만큼 급해진다
     (하위 하나가 오늘까지인데 상위가 '여유'에 접혀 있으면 묻힌다).
  2) 하위만 맡은 사람도 담당자 필터에서 자기 일을 찾는다.
  3) 한 단계만 · 회의 할 일은 상위가 될 수 없다 · 칸이 없으면 안내만.
  4) 보드가 하위와 '끝낸 업무'를 실제로 그린다.
"""

import pathlib
from datetime import date, timedelta

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
REF = date(2026, 9, 1)


@pytest.fixture()
def wk():
    from database import work_store as wk
    return wk


def raw(i, content, owner=None, due=None, parent=None, done=False, born="2026-09-01"):
    return {"id": i, "content": content, "owner": owner, "due_date": due,
            "parent_id": parent, "done": done,
            "done_at": "2026-09-01T03:00:00+00:00" if done else None,
            "created_at": born, "memo": None}


def views(wk, rows):
    return [wk._view(r, "work", REF) for r in rows]


# --- 1) 묶기와 순위 ----------------------------------------------------------

def test_하위는_상위_밑으로_들어간다(wk):
    got = wk.nest(views(wk, [
        raw(1, "9월 메뉴판", owner="사장님"),
        raw(2, "사진 고르기", parent=1, done=True),
        raw(3, "인쇄 발주", parent=1),
        raw(4, "따로 업무", owner="A"),
    ]), REF)
    assert [t["content"] for t in got] == ["9월 메뉴판", "따로 업무"]
    top = got[0]
    assert top["sub_total"] == 2 and top["sub_open"] == 1
    assert [s["content"] for s in top["subs"]] == ["인쇄 발주", "사진 고르기"], \
        "열린 하위가 먼저, 끝낸 하위는 뒤"


def test_하위가_급하면_상위도_급해지고_이유를_밝힌다(wk):
    got = wk.nest(views(wk, [
        raw(1, "9월 메뉴판", owner="사장님"),                 # 혼자면 '여유'
        raw(2, "인쇄 발주", owner="A", parent=1, due=str(REF)),
    ]), REF)
    assert got[0]["pri"]["level"] == "hi"
    assert "하위 '인쇄 발주'" in got[0]["pri"]["why"]
    assert "오늘까지예요" in got[0]["pri"]["why"]


def test_끝낸_하위는_상위_순위에_영향이_없다(wk):
    got = wk.nest(views(wk, [
        raw(1, "9월 메뉴판", owner="사장님"),
        raw(2, "인쇄 발주", parent=1, due=str(REF - timedelta(days=3)), done=True),
    ]), REF)
    assert got[0]["pri"]["level"] == "low"


def test_하위를_누가_맡았으면_상위가_담당_없음으로_뜨지_않는다(wk):
    got = wk.nest(views(wk, [
        raw(1, "9월 메뉴판"),
        raw(2, "인쇄 발주", owner="A", parent=1),
    ]), REF)
    assert got[0]["pri"]["why"] != "아무도 안 맡았어요"


def test_상위를_못_찾은_하위는_버린다(wk):
    got = wk.nest(views(wk, [raw(2, "고아", parent=99)]), REF)
    assert got == []


# --- 2) 담당자 ---------------------------------------------------------------

def test_하위만_맡은_사람도_필터에_잡힌다(wk):
    tops = wk.nest(views(wk, [
        raw(1, "9월 메뉴판", owner="사장님"),
        raw(2, "인쇄 발주", owner="민지", parent=1),
        raw(3, "끝낸 것", owner="철수", parent=1, done=True),
    ]), REF)
    assert tops[0]["owners"] == ["사장님", "민지"], "끝낸 하위의 담당은 빼고"
    counts = {o["owner"]: o["n"] for o in wk.owner_counts(tops)}
    assert counts == {"사장님": 1, "민지": 1}


# --- 3) 등록 규칙 ------------------------------------------------------------

def test_회의_할_일에는_하위를_달_수_없다(wk):
    with pytest.raises(ValueError, match="회의"):
        wk._check_parent("m:3")


def test_칸이_없으면_안내만_한다(wk, monkeypatch):
    monkeypatch.setattr(wk, "subtasks_ready", lambda: False)
    with pytest.raises(ValueError, match="schema_v14"):
        wk._check_parent("w:3")


def test_상위를_끝내면_하위도_같이_끝내는_코드가_있다(wk):
    import inspect
    src = inspect.getsource(wk.set_done)
    assert 'eq("parent_id", task_id)' in src
    assert "_touch()" in src


def test_마이그레이션_파일은_한_칸과_연쇄삭제():
    sql = (ROOT / "database" / "schema_v14.sql").read_text(encoding="utf-8")
    assert "add column if not exists parent_id" in sql
    assert "on delete cascade" in sql


# --- 4) 화면 -----------------------------------------------------------------

@pytest.fixture()
def board(monkeypatch, wk):
    import sys
    sys.path.insert(0, str(ROOT / "service"))
    import service.app as m
    if not m.SERVICE_PATH:
        pytest.skip("SERVICE_PATH 가 없어 화면을 열 수 없음")
    tops = wk.nest(views(wk, [
        raw(1, "9월 메뉴판", owner="사장님"),
        raw(2, "인쇄 발주", owner="민지", parent=1),
        raw(3, "사진 고르기", parent=1, done=True),
    ]), REF)
    done = wk.nest(views(wk, [raw(7, "8월 재고 정리", owner="A", done=True)]), REF)
    for t in done:
        t["done_label"] = "9/1"
    monkeypatch.setattr(m.wk, "open_tasks", lambda: tops)
    monkeypatch.setattr(m.wk, "done_tasks", lambda: done)
    monkeypatch.setattr(m.wk, "subtasks_ready", lambda: True)
    monkeypatch.setattr(m, "_derived_work", lambda: [])
    if hasattr(m, "_inbox"):
        monkeypatch.setattr(m, "_inbox", lambda *a, **k: [])
    if hasattr(m, "_owner_alerts"):
        monkeypatch.setattr(m, "_owner_alerts", lambda *a, **k: [])
    return m.app.test_client().get(f"/{m.SERVICE_PATH}/work").get_data(as_text=True)


def test_보드가_하위와_진행률을_그린다(board):
    assert 'id="subs-w-1"' in board
    assert "인쇄 발주" in board and "☑ 1/2" in board
    assert 'data-owners="사장님|민지"' in board
    assert "openNew('w:1')" in board


def test_보드가_끝낸_업무를_그린다(board):
    assert 'id="doneList"' in board
    assert "8월 재고 정리" in board and "9/1 끝냄" in board
    assert 'id="d-w-7"' in board, "끝낸 줄은 열린 줄과 id 가 겹치면 안 된다"
