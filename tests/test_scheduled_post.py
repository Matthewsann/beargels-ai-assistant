"""'아침에 등록'(예약) 흐름 — 새벽 답글이 새벽에 나가지 않게 (2026-08-28).

답글을 달면 손님 폰에 푸시가 간다. 새벽 3시 푸시는 반갑지도 않고 주문으로도
이어지지 않는다. 베어글스 주문은 오전 10~12시에 몰리므로(실측 1,039건:
11시 160 · 10시 117 · 12시 115), 예약분은 아침 9시부터 순서대로 올린다.

여기서 지키는 것:
  1) 예약 상태(scheduled)는 자동복구가 즉시 등록해 버리면 안 된다.
  2) 예약해도 카드가 목록에서 사라지지 않는다(확인·취소할 수 있어야 한다).
  3) 아침 슬롯에서만 풀린다.
"""

from datetime import datetime

import pytest


# --- 상태 설계 ------------------------------------------------------------

def test_scheduled_is_pending_not_settled():
    """예약분은 '아직 안 나간 것'이라 답글 화면에 남는다."""
    from database import supabase_client as db
    assert "scheduled" in db._PENDING_STATUSES, "목록에서 사라지면 취소할 길이 없다"
    # 초안칸 자동저장이 예약을 'drafted' 로 되돌리면 안 된다
    assert "scheduled" in db._SETTLED_STATUSES


def test_rescue_only_revives_approved_not_scheduled():
    """자동복구는 approved 만 본다 — scheduled 를 집으면 새벽에 나가 버린다."""
    import inspect

    from worker import agent
    src = inspect.getsource(agent.rescue_stuck_approved)
    assert "get_approved_reviews" in src
    assert "get_scheduled_reviews" not in src


# --- 아침 슬롯 ------------------------------------------------------------

def test_slot_opens_only_in_the_morning_window():
    from worker.agent import slot_due
    at9 = datetime(2026, 8, 28, 9, 0)
    assert slot_due("09:00", at9, None) == "2026-08-28 09:00"
    # 같은 슬롯을 두 번 돌지 않는다
    assert slot_due("09:00", at9, "2026-08-28 09:00") is None
    # 새벽·낮에는 안 열린다
    for h, m in ((3, 0), (8, 30), (9, 20), (14, 0), (23, 59)):
        assert slot_due("09:00", datetime(2026, 8, 28, h, m), None) is None


def test_release_scheduled_queues_normal_post_jobs(monkeypatch):
    """예약분은 '직접 게시'가 아니라 평소의 등록 잡으로 줄 세운다.

    그래야 실패 처리·기한 만료 정리·중복 방지가 버튼 등록과 똑같이 걸린다.
    """
    from worker import agent
    rows = [{"id": 11}, {"id": 22}]
    approved, posted = [], []
    monkeypatch.setattr(agent.db, "get_scheduled_reviews", lambda *a, **k: rows)
    monkeypatch.setattr(agent.db, "mark_approved", lambda rid: approved.append(rid))
    monkeypatch.setattr(agent.db, "request_post",
                        lambda rid, by=None: posted.append((rid, by)))
    monkeypatch.setattr(agent.db, "log_error", lambda *a, **k: None)
    assert agent.release_scheduled() == 2
    assert approved == [11, 22]
    assert [p[0] for p in posted] == [11, 22]


def test_release_scheduled_survives_one_bad_row(monkeypatch):
    """한 건이 실패해도 나머지는 올라간다."""
    from worker import agent
    monkeypatch.setattr(agent.db, "get_scheduled_reviews",
                        lambda *a, **k: [{"id": 1}, {"id": 2}])
    monkeypatch.setattr(agent.db, "mark_approved", lambda rid: None)
    monkeypatch.setattr(agent.db, "log_error", lambda *a, **k: None)

    def flaky(rid, by=None):
        if rid == 1:
            raise RuntimeError("통신 실패")

    monkeypatch.setattr(agent.db, "request_post", flaky)
    assert agent.release_scheduled() == 1


def test_nothing_scheduled_is_a_quiet_noop(monkeypatch):
    from worker import agent
    monkeypatch.setattr(agent.db, "get_scheduled_reviews", lambda *a, **k: [])
    assert agent.release_scheduled() == 0


# --- 화면 문구 ------------------------------------------------------------

@pytest.mark.parametrize("hour,expect", [(3, "오늘"), (8, "오늘"), (10, "내일"),
                                         (23, "내일")])
def test_label_says_today_or_tomorrow(monkeypatch, hour, expect):
    """9시 전에 누르면 '오늘 아침', 지난 뒤면 '내일 아침'."""
    import service.app as app

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 28, hour, 5)

    monkeypatch.setattr(app, "datetime", _Now)
    assert app.scheduled_post_when().startswith(expect)


@pytest.mark.parametrize("hour,night", [(23, True), (2, True), (7, True),
                                        (8, False), (14, False), (21, False)])
def test_night_hours_default_to_scheduling(monkeypatch, hour, night):
    """22시~아침 8시엔 [🌙 아침에 등록]이 기본 버튼이 된다(사장님 확정)."""
    import service.app as app

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 28, hour, 30)

    monkeypatch.setattr(app, "datetime", _Now)
    assert app._is_night() is night
