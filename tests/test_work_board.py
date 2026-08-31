"""업무 보드 — 비서가 매기는 우선순위 규칙 (2026-08-31).

역할 분담(사장님 확정): 비서는 **알려주고 우선순위를 매긴다**. 등록·담당자·
기한·완료는 사람이 한다. 그래서 이 파일이 지키는 것은 "순위가 말이 되는가"다.

우선순위를 컬럼에 저장하지 않고 매번 계산하는 이유도 여기서 지켜진다 —
날짜가 흐르면 아무것도 안 해도 순위가 저절로 따라 움직여야 한다.
"""

from datetime import date, timedelta

import pytest


@pytest.fixture()
def wk():
    from database import work_store as wk
    return wk


REF = date(2026, 9, 1)          # 기준일을 고정해 '오늘'에 흔들리지 않게


def task(due=None, owner=None, born=None):
    return {"due_date": due, "owner": owner, "created_at": born}


# --- 기한이 순위를 지배한다 ------------------------------------------------

@pytest.mark.parametrize("due,level,why", [
    (REF - timedelta(days=3), "hi",  "기한 3일 지났어요"),
    (REF - timedelta(days=1), "hi",  "어제까지였어요"),
    (REF,                     "hi",  "오늘까지예요"),
    (REF + timedelta(days=1), "hi",  "내일까지예요"),
    (REF + timedelta(days=4), "mid", "4일 남았어요"),
])
def test_기한별_등급과_설명(wk, due, level, why):
    got = wk.priority_of(task(due=str(due), owner="사장님"), REF)
    assert got["level"] == level
    assert got["why"] == why, "왜 지금인지 항상 한 줄로 설명돼야 한다"


def test_기한_지난_것이_가장_위(wk):
    late = wk.priority_of(task(due=str(REF - timedelta(days=1)), owner="A"), REF)
    today_ = wk.priority_of(task(due=str(REF), owner="A"), REF)
    soon = wk.priority_of(task(due=str(REF + timedelta(days=3)), owner="A"), REF)
    assert late["rank"] < today_["rank"] < soon["rank"]


# --- 기한이 없을 때 --------------------------------------------------------

def test_기한_없이_오래_묵으면_오래됨(wk):
    """기한을 넣게 만드는 장치 — 방치되면 스스로 올라온다."""
    old = task(born=str(REF - timedelta(days=wk.STALE_DAYS)), owner="A")
    assert wk.priority_of(old, REF)["level"] == "mid"
    assert "그대로예요" in wk.priority_of(old, REF)["why"]


def test_방금_등록한_건은_재촉하지_않는다(wk):
    fresh = task(born=str(REF - timedelta(days=1)), owner="A")
    assert wk.priority_of(fresh, REF)["level"] == "low"


def test_담당자가_없으면_올라온다(wk):
    """아무도 안 맡은 일이 조용히 묻히지 않게."""
    got = wk.priority_of(task(born=str(REF), owner=None), REF)
    assert got["level"] == "mid"
    assert got["why"] == "아무도 안 맡았어요"


# --- '오늘 이것부터' -------------------------------------------------------

def test_여유_있는_업무는_재촉하지_않는다(wk):
    tasks = [
        {"pri": {"level": "hi", "rank": 0}, "content": "급한 것"},
        {"pri": {"level": "low", "rank": 6}, "content": "여유"},
        {"pri": {"level": "mid", "rank": 4}, "content": "오래됨"},
    ]
    got = wk.top_priorities(tasks)
    assert [t["content"] for t in got] == ["급한 것", "오래됨"]


def test_상위_세_건까지만(wk):
    many = [{"pri": {"level": "hi", "rank": 0}, "content": f"t{i}"} for i in range(9)]
    assert len(wk.top_priorities(many)) == 3


# --- 담당자 묶기 -----------------------------------------------------------

def test_담당자_없음은_항상_맨_뒤(wk):
    rows = wk.owner_counts([
        {"owner": ""}, {"owner": "서주희"}, {"owner": ""}, {"owner": "정산"},
        {"owner": "서주희"},
    ])
    assert rows[0] == {"owner": "서주희", "n": 2}    # 많은 순
    assert rows[-1] == {"owner": "", "n": 2}, "담당자 없음은 맨 뒤"


# --- 두 표를 합쳐 다루는 id ------------------------------------------------

@pytest.mark.parametrize("raw,expect", [
    ("w:12", ("work", 12)),
    ("m:34", ("meeting", 34)),
    ("x:1", (None, None)),
    ("w:abc", (None, None)),
    ("12", (None, None)),
    ("", (None, None)),
    (None, (None, None)),
])
def test_업무_id_해석(wk, raw, expect):
    """화면이 돌려준 값을 그대로 믿지 않는다 — 이상하면 (None, None)."""
    assert wk.parse_id(raw) == expect


# --- 날짜 표기 -------------------------------------------------------------

@pytest.mark.parametrize("due,label", [
    (REF - timedelta(days=2), "D+2 지남"),
    (REF, "오늘"),
    (REF + timedelta(days=1), "내일"),
    (REF + timedelta(days=4), "9/5"),
    (None, ""),
])
def test_기한_표기(wk, due, label):
    assert wk.dday_label(str(due) if due else None, REF) == label


# --- 저장은 각자 자리에 ----------------------------------------------------

def test_회의_할_일은_회의_표에_그대로_둔다():
    """합치는 건 화면에서만 — 회의를 지우면 그 할 일만 같이 지워져야 한다."""
    import inspect
    from database import work_store as wk
    src = inspect.getsource(wk)
    assert "work_tasks" in src
    # 회의 할 일을 work_tasks 로 복사해 넣는 코드가 있으면 유령 업무가 남는다
    assert "insert" not in inspect.getsource(wk._meeting_rows)
