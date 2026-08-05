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
