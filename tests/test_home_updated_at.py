"""홈의 '마지막 수집' 시각 표기 — 숫자가 언제 기준인지 (2026-08-30).

홈을 열 때마다 배민·쿠팡을 다시 긁을 수는 없다(수 분 걸린다). 그래서 답글
건수·알림은 **마지막 수집 시점**의 것이고, 그 사실을 화면에 적어 주지 않으면
직원이 "방금 들어온 리뷰가 없다"고 오해한다(사장님 지적 2026-08-30).

여기서 지키는 것:
  1) 서버 시계(UTC)가 아니라 매장 시간(KST)으로 보여준다.
  2) 오래 벌어지면 stale 로 표시해 눈에 띈다 — 집 PC 가 꺼져 있었다는 뜻.
  3) 값이 없거나 깨져도 홈은 그냥 뜬다(표기만 빠진다).
"""

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture()
def view():
    from service.app import _updated_view
    return _updated_view


@pytest.fixture()
def kst():
    from service.app import KST
    return KST


# --- 매장 시간으로 보여준다 ------------------------------------------------

def test_utc_기록을_KST_로_바꿔_보여준다(view):
    """서버는 UTC 다. 00:01Z 를 그대로 적으면 9시간 어긋난다."""
    got = view("2026-08-30T00:01:24.940159+00:00")
    assert got["at"].endswith("09:01"), got


def test_시간대_없는_옛_기록도_UTC_로_읽는다(view):
    """옛 행에는 +00:00 이 없다 — 로컬시간으로 읽으면 또 9시간 어긋난다."""
    assert view("2026-08-30T00:01:24")["at"].endswith("09:01")


def test_같은_날이면_시각만_다른_날이면_날짜까지(view, kst):
    now = datetime.now(kst)
    recent = now - timedelta(minutes=5)
    if recent.date() == now.date():   # 자정 직후 5분에 돌 때만 건너뛴다
        # 같은 날 → "09:01" 처럼 시각만(슬래시 없음)
        assert "/" not in view(recent.isoformat())["at"]
    # 다른 날 → "8/29 14:19" 처럼 날짜가 붙는다
    assert "/" in view((now - timedelta(days=1)).isoformat())["at"]


# --- 얼마나 지났는지 -------------------------------------------------------

@pytest.mark.parametrize("delta,expect", [
    (timedelta(seconds=20), "방금"),
    (timedelta(minutes=40), "40분 전"),
    (timedelta(hours=3), "3시간 전"),
    (timedelta(days=2), "2일 전"),
])
def test_지난_시간을_사람_말로(view, kst, delta, expect):
    assert view((datetime.now(kst) - delta).isoformat())["ago"] == expect


# --- 낡음 판정 -------------------------------------------------------------

def test_심야_공백은_낡음이_아니다(view, kst):
    """자동수집은 심야(0-7시)에 쉰다 — 아침 첫 접속의 9시간 공백은 정상이다."""
    assert view((datetime.now(kst) - timedelta(hours=9)).isoformat())["stale"] is False


def test_하루_넘게_안_돌면_낡음(view, kst):
    """집 PC 가 꺼져 있었다는 뜻 — 숫자를 믿으면 안 된다."""
    assert view((datetime.now(kst) - timedelta(days=1)).isoformat())["stale"] is True


# --- 없거나 깨져도 화면은 뜬다 ----------------------------------------------

@pytest.mark.parametrize("bad", [None, "", "nonsense", "2026-13-45"])
def test_값이_없거나_깨지면_표기만_빠진다(view, bad):
    assert view(bad) is None


# --- 성공한 수집만 센다 -----------------------------------------------------

def test_실패한_수집은_기준이_될_수_없다():
    """실패한 잡은 데이터를 갱신하지 못했다 — status='done' 만 본다."""
    import inspect
    from database import supabase_client as db
    src = inspect.getsource(db.last_collect_at)
    assert '"done"' in src, "성공한 수집만 기준으로 삼아야 한다"
    assert "finished_at" in src, "요청 시각이 아니라 끝난 시각이 기준이다"
