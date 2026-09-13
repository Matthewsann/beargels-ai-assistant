"""장부 시트가 밀리면 화면이 말해준다 (2026-09-09).

왜: 일꾼이 매일 시트를 읽다가 token.json 이 없어 **6일 연속 실패**했는데
(2026-09-04~09 실측) 실패가 error_log 에만 쌓여 아무도 못 봤다. 그동안
8월 장부가 '예상치'인 채로 남아 대시보드는 조용히 7월을 진단하고 있었다.
사장님이 "8월 데이터 폴더에 있을 텐데 업데이트 안됨" 이라고 직접 알려준 뒤에야
드러났다. 그 침묵을 없애는 규칙이다.

계약:
  · 지난달이 confirmed 이고 반영도 잘 돌면 → 아무 말 안 한다
  · 지난달이 confirmed 여도 반영이 막혀 있으면 → 지금 알린다(다음 달까지 안 기다림)
  · 지난달이 estimate 이거나 아예 없으면 → 무엇이 없는지 + 무엇을 하면 되는지
  · 원인을 아는 경우(구글 로그인 풀림)엔 그 원인을 사장님 말로 말한다
"""
from datetime import date

from service import dashboard_page as dp

TODAY = date(2026, 9, 9)          # 지난달 = 2026-08
ERR = {"at": "2026-09-09T01:20:39+00:00",
       "cause": "구글 로그인이 풀렸어요",
       "fix": "집 PC에서 3_google_login.bat 을 한 번 실행해 주세요."}


def months(**status):
    return [{"ym": ym, "status": st} for ym, st in status.items()]


def test_최신이고_잘_돌면_조용하다():
    m = months(**{"2026-07": "confirmed", "2026-08": "confirmed"})
    assert dp.ledger_alert(m, TODAY, "2026-09-09", None) is None


def test_최신이어도_반영이_막혔으면_지금_알린다():
    m = months(**{"2026-07": "confirmed", "2026-08": "confirmed"})
    a = dp.ledger_alert(m, TODAY, "2026-09-09", ERR)
    assert a is not None and a["blocked"] is True
    assert "자동 반영이 막혀" in a["why"]
    assert "3_google_login.bat" in a["how"]


def test_지난달이_예상치면_마감하라고_한다():
    m = months(**{"2026-07": "confirmed", "2026-08": "estimate"})
    a = dp.ledger_alert(m, TODAY, "2026-09-03", None)
    assert "8월" in a["why"] and "예상치" in a["why"]
    assert "마감" in a["how"] and a["blocked"] is False


def test_지난달이_아예_없으면_채우라고_한다():
    m = months(**{"2026-07": "confirmed"})
    a = dp.ledger_alert(m, TODAY, "2026-09-03", None)
    assert "8월" in a["why"] and "안 들어왔어요" in a["why"]
    assert "베어글스_장부" in a["how"]


def test_원인을_알면_원인을_먼저_말한다():
    m = months(**{"2026-07": "confirmed"})
    a = dp.ledger_alert(m, TODAY, "2026-09-03", ERR)
    assert a["how"].startswith("구글 로그인이 풀렸어요")
    assert a["blocked"] is True


def test_해가_바뀌어도_지난달을_맞게_고른다():
    m = months(**{"2025-12": "confirmed"})
    assert dp.ledger_alert(m, date(2026, 1, 5), None, None) is None
    m2 = months(**{"2025-11": "confirmed"})
    a = dp.ledger_alert(m2, date(2026, 1, 5), None, None)
    assert "12월" in a["why"]


def test_반영_날짜를_같이_알려준다():
    m = months(**{"2026-07": "confirmed"})
    a = dp.ledger_alert(m, TODAY, "2026-09-03T10:00:00+00:00", None)
    assert a["synced"] == "2026-09-03"


def test_실패_뒤에_성공했으면_실패를_잊는다():
    """로그인을 풀고 바로 반영됐는데 '막혀 있어요'가 사흘 더 떠 있으면 안 된다."""
    m = months(**{"2026-07": "confirmed", "2026-08": "confirmed"})
    old_err = dict(ERR, at="2026-09-13T04:04:00+00:00")
    assert dp.ledger_alert(m, TODAY, "2026-09-13T04:07:00+00:00", old_err) is None


def test_성공_뒤에_또_실패하면_그건_알린다():
    m = months(**{"2026-07": "confirmed", "2026-08": "confirmed"})
    new_err = dict(ERR, at="2026-09-13T05:00:00+00:00")
    a = dp.ledger_alert(m, TODAY, "2026-09-13T04:07:00+00:00", new_err)
    assert a and a["blocked"]
