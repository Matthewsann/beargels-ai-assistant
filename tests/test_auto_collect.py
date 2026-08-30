"""일꾼 자동 수집 판단 로직 회귀 테스트.

직원이 버튼을 안 눌러도 몇 시간마다 알아서 수집하되,
심야엔 쉬고 · 방금 수집했으면 안 하고 · 실패 직후 스팸 재시도도 안 한다.
"""

from datetime import datetime, timedelta

from worker import agent


def _t(hour, minute=0):
    return datetime(2026, 8, 6, hour, minute).astimezone()


def test_due_when_never_collected():
    assert agent.auto_collect_due(_t(10), None) is True


def test_not_due_right_after_a_collect():
    # 직원이 방금 눌렀거나(요청 시각 기준) 자동 수집 직후면 쉰다.
    assert agent.auto_collect_due(_t(10), _t(9, 30)) is False


def test_due_after_interval():
    last = _t(10) - timedelta(hours=agent.AUTO_COLLECT_HOURS)
    assert agent.auto_collect_due(_t(10), last) is True


def test_quiet_hours_block(monkeypatch):
    # 심야(기본 00~07시)엔 아무리 오래됐어도 안 돈다.
    assert agent.auto_collect_due(_t(3), None) is False
    assert agent.auto_collect_due(_t(6, 59), _t(0)) is False
    assert agent.auto_collect_due(_t(7), None) is True     # 07시부터 재개


def test_quiet_hours_wrapping_midnight(monkeypatch):
    monkeypatch.setattr(agent, "QUIET_HOURS", "23-7")
    assert agent._in_quiet_hours(_t(23, 30)) is True
    assert agent._in_quiet_hours(_t(2)) is True
    assert agent._in_quiet_hours(_t(12)) is False


def test_disabled_by_env(monkeypatch):
    monkeypatch.setattr(agent, "AUTO_COLLECT_HOURS", 0)
    assert agent.auto_collect_due(_t(10), None) is False


# --- 자동 답글 등록 슬롯(AUTO_POST_TIMES) ---------------------------------
# 그 경로는 2026-08-29 에 지웠다 — 죽은 코드였고, 되켜면 잡 큐의 중복 방지를
# 우회해 같은 리뷰에 답글이 두 번 달릴 수 있었다. 슬롯 로직 자체(slot_due)는
# 아침 예약·문제리뷰 보고가 계속 쓰므로 test_scheduled_post.py 가 지킨다.

def test_auto_post_path_is_gone():
    assert not hasattr(agent, "run_auto_post")
    assert not hasattr(agent, "AUTO_POST_TIMES")


# --- 답글 기한 지난 옛 리뷰 정리 ---------------------------------------------

def test_too_old_to_reply_by_window():
    # 전체 수집 뒤 옛 리뷰가 직원 화면을 덮던 것 방지(2026-08-13).
    from datetime import date, timedelta
    old = (date.today() - timedelta(days=agent.REPLY_WINDOW_DAYS + 1)).isoformat()
    fresh = (date.today() - timedelta(days=3)).isoformat()
    assert agent._too_old_to_reply({"written_date": old}) is True
    assert agent._too_old_to_reply({"written_date": fresh}) is False


def test_unknown_date_is_kept():
    # 날짜를 모르면 함부로 정리하지 않는다.
    assert agent._too_old_to_reply({}) is False
    assert agent._too_old_to_reply({"written_date": "언젠가"}) is False
