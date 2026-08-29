"""회의 기록 화면의 순수 로직 회귀 테스트 (DB·브라우저 없이 돈다).

여기서 지키는 계약:
  1) 검색어에 쉼표·괄호가 섞여도 PostgREST or_ 문법이 안 깨진다.
  2) 폼에서 온 할 일 줄이 담당·기한·완료와 **짝이 안 어긋난 채** 모인다.
     (빈 줄을 건너뛰면서 인덱스가 밀리면 남의 담당자가 붙는다.)
  3) 수정 화면을 거쳐도 이미 완료한 할 일의 완료 표시가 풀리지 않는다.
  4) 분류는 목록에서 고르거나 '직접 입력'으로 새로 넣을 수 있다.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database import meeting_store as mt  # noqa: E402


@pytest.fixture
def app_mod(monkeypatch):
    monkeypatch.setenv("SERVICE_PATH", "testkey")
    import importlib

    import service.app as m
    importlib.reload(m)
    return m


# ── 검색 ────────────────────────────────────────────────────────

def test_search_filter_strips_syntax_breakers():
    cond = mt._search_filter("포장, (누락)")
    assert "(" not in cond and ")" not in cond
    # 조건 구분용 쉼표만 남는다 — 각 조각은 'col.ilike.%값%' 꼴
    for part in cond.split(","):
        assert part.count(".ilike.") == 1


def test_search_filter_ignores_spacing():
    cond = mt._search_filter("크림 치즈")
    assert "%크림치즈%" in cond and "%크림 치즈%" in cond


def test_search_filter_looks_at_body_and_decisions():
    cond = mt._search_filter("포장")
    assert "body.ilike" in cond and "decisions.ilike" in cond


# ── 날짜 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,want", [
    ("2026-08-27", "2026-08-27"),
    ("2026-08-27T10:00:00", "2026-08-27"),
    ("", None),
    (None, None),
    ("오늘", None),          # 못 읽는 값은 비운다(엉뚱한 날짜로 저장하지 않는다)
])
def test_date_normalizing(raw, want):
    assert mt._d(raw) == want


# ── 폼 → 할 일 ──────────────────────────────────────────────────

def _post(app_mod, form):
    # 같은 이름이 여러 번 오는 폼(할 일 줄들)이라 MultiDict 로 넘긴다.
    from werkzeug.datastructures import MultiDict
    return app_mod.app.test_request_context(
        "/testkey/meeting/save", method="POST", data=MultiDict(form))


def test_form_tasks_keep_columns_aligned(app_mod):
    """빈 줄이 섞여도 담당·기한이 남의 줄로 밀리지 않는다."""
    form = [
        ("task_id", ""), ("task_done", "0"), ("task_content", "원가 계산"),
        ("task_owner", "사장님"), ("task_due", "2026-08-29"),
        ("task_id", ""), ("task_done", "0"), ("task_content", "  "),
        ("task_owner", "지은"), ("task_due", "2026-08-30"),
        ("task_id", "7"), ("task_done", "1"), ("task_content", "안내문 붙이기"),
        ("task_owner", "태윤"), ("task_due", ""),
    ]
    with _post(app_mod, form):
        rows = app_mod._meeting_form_tasks()
    assert len(rows) == 2
    assert rows[0]["content"] == "원가 계산"
    assert rows[0]["owner"] == "사장님"
    assert rows[0]["due_date"] == "2026-08-29"
    assert rows[1]["content"] == "안내문 붙이기"
    assert rows[1]["owner"] == "태윤"          # 빈 줄의 '지은' 이 밀려오면 안 된다
    assert rows[1]["id"] == "7"


def test_form_keeps_done_flag(app_mod):
    """수정 화면을 거쳐도 완료 표시가 풀리지 않는다."""
    form = [
        ("task_id", "7"), ("task_done", "1"), ("task_content", "안내문"),
        ("task_owner", ""), ("task_due", ""),
        ("task_id", "8"), ("task_done", "0"), ("task_content", "근무표"),
        ("task_owner", ""), ("task_due", ""),
    ]
    with _post(app_mod, form):
        rows = app_mod._meeting_form_tasks()
    assert rows[0]["done"] is True
    assert rows[1]["done"] is False


# ── 분류 ────────────────────────────────────────────────────────

def test_category_picked_from_list(app_mod):
    with _post(app_mod, {"category": "주간회의"}):
        assert app_mod._meeting_category() == "주간회의"


def test_category_direct_input(app_mod):
    with _post(app_mod, {"category": "__new__", "category_new": " 위생점검 "}):
        assert app_mod._meeting_category() == "위생점검"


def test_category_can_be_empty(app_mod):
    with _post(app_mod, {"category": ""}):
        assert app_mod._meeting_category() == ""


# ── 목록 카드 / 상세 표기 ───────────────────────────────────────

def test_card_counts_open_tasks(app_mod):
    row = {"meeting_date": "2026-08-25", "title": "주간회의",
           "decisions": "세트는 영수증 대조\n신메뉴는 단호박"}
    view = app_mod._meeting_card(row, [{"done": True}, {"done": False}])
    assert view["open"] == 1 and view["tasks"] == 2
    assert view["when"] == "8월 25일 (화)"
    assert view["summary"].startswith("세트는 영수증 대조 · 신메뉴는")


def test_card_falls_back_to_body_when_no_decisions(app_mod):
    row = {"meeting_date": "2026-08-25", "title": "메모",
           "body": "포장 누락 이야기"}
    assert app_mod._meeting_card(row, [])["summary"] == "포장 누락 이야기"


def test_card_survives_broken_date(app_mod):
    view = app_mod._meeting_card({"meeting_date": "", "title": "x"}, [])
    assert view["when"] == ""      # 죽지 않는 게 계약


def test_decision_lines_drop_bullets(app_mod):
    assert app_mod._lines("- 첫째\n · 둘째\n\n") == ["첫째", "둘째"]


# ── 기한 D-Day 표기 ─────────────────────────────────────────────
# 사장님 지시 2026-08-28: 화면에는 날짜 대신 D-Day 로만 보여준다.

@pytest.mark.parametrize("due,want", [
    ("2026-08-31", "D-3"),
    ("2026-08-29", "D-1"),
    ("2026-08-28", "D-DAY"),
    ("2026-08-27", "D+1 지남"),
    ("2026-08-26", "D+2 지남"),
    ("", None),
    (None, None),
    ("언젠가", None),
])
def test_dday_label(due, want):
    from datetime import date
    assert mt.dday_label(due, today=date(2026, 8, 28)) == want


def test_dday_uses_store_time_not_server_time():
    """기준일을 안 넘기면 매장 시간(KST) 오늘로 잰다 — 서버는 UTC 라 하루 어긋난다."""
    today = mt.today_kst()
    assert mt.dday_label(str(today)) == "D-DAY"


def test_done_task_is_not_marked_late(app_mod, monkeypatch):
    """끝난 일은 기한이 지났어도 빨갛게 하지 않는다 — 이미 처리된 일이다."""
    from datetime import date
    monkeypatch.setattr(app_mod.mt, "today_kst", lambda: date(2026, 8, 28))
    monkeypatch.setattr(app_mod.mt, "get_tasks", lambda mid: [
        {"id": 1, "content": "끝난 일", "due_date": "2026-08-20", "done": True},
        {"id": 2, "content": "남은 일", "due_date": "2026-08-20", "done": False},
    ])
    rows = app_mod._meeting_tasks_view(1)
    assert rows[0]["dday"] == "D+8 지남" and rows[0]["overdue"] is False
    assert rows[1]["dday"] == "D+8 지남" and rows[1]["overdue"] is True


# ── 업무 메모 (담당자/마감일/업무내용/기타메모) ──────────────────
# 사장님 지시 2026-08-30: 회의에서 나온 업무는 네 항목을 다 받는다.

def test_form_tasks_include_memo(app_mod):
    form = [
        ("task_id", ""), ("task_done", "0"), ("task_content", "원가 계산"),
        ("task_owner", "사장님"), ("task_due", "2026-08-29"),
        ("task_memo", "엑셀에 단가표 있음"),
    ]
    with _post(app_mod, form):
        rows = app_mod._meeting_form_tasks()
    assert rows[0]["memo"] == "엑셀에 단가표 있음"


def test_form_tasks_memo_optional(app_mod):
    """메모 줄이 아예 안 와도(구버전 폼) 죽지 않는다."""
    form = [
        ("task_id", ""), ("task_done", "0"), ("task_content", "근무표"),
        ("task_owner", ""), ("task_due", ""),
    ]
    with _post(app_mod, form):
        rows = app_mod._meeting_form_tasks()
    assert rows[0]["memo"] == ""
