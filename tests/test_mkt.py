"""마케팅 캘린더 순수 로직 회귀 테스트 (DB·브라우저 불필요).

핵심 계약:
  · totals_by_date — 매장은 출처 합산(IMU+TOS), 배달은 우선순위 출처만(이중계상 방지)
  · weekday_baseline / day_signal — 같은 요일 4주 평균, 휴무(0원) 제외
  · campaign_effect — 요일 보정 기대치 대비 증분, 짧은 기간 '참고용' 플래그
  · extract_targets — 제목에서 메뉴 이름 자동 인식
  · pos_import 셀 파싱 유틸
"""
from datetime import date, timedelta

from database import mkt_store
from worker import pos_import


# ---------------------------------------------------------------------------
# pos_import 유틸
# ---------------------------------------------------------------------------

def test_to_date_formats():
    assert pos_import._to_date("2026-07-31") == date(2026, 7, 31)
    assert pos_import._to_date("2026-07-31 22:48:35") == date(2026, 7, 31)
    assert pos_import._to_date("20251001") == date(2025, 10, 1)
    assert pos_import._to_date("합계") is None
    assert pos_import._to_date(None) is None


def test_to_int_formats():
    assert pos_import._to_int("1,232,000") == 1232000
    assert pos_import._to_int(" 5,800원") == 5800
    assert pos_import._to_int(14000.0) == 14000
    assert pos_import._to_int("-4,300") == -4300
    assert pos_import._to_int("") == 0


# ---------------------------------------------------------------------------
# totals_by_date — 출처 병합 규칙
# ---------------------------------------------------------------------------

def test_store_sums_across_sources():
    """매장 = IMU(키오스크) + TOS(포스) 합산 — 1월 장부 원 단위 일치 검증의 규칙."""
    rows = [
        {"sale_date": "2026-01-10", "channel": "store", "amount": 100, "source": "imu"},
        {"sale_date": "2026-01-10", "channel": "store", "amount": 200, "source": "tos"},
    ]
    daily = mkt_store.totals_by_date(rows)
    assert daily["2026-01-10"]["store"] == 300
    assert daily["2026-01-10"]["total"] == 300


def test_delivery_prefers_tos_over_other_sources():
    """배달 채널은 같은 날 tos 와 정산엑셀/크롤러가 겹치면 tos 만 (이중계상 방지)."""
    rows = [
        {"sale_date": "2026-01-10", "channel": "baemin", "amount": 500, "source": "tos"},
        {"sale_date": "2026-01-10", "channel": "baemin", "amount": 480, "source": "baemin_xls"},
        {"sale_date": "2026-01-10", "channel": "coupang", "amount": 300, "source": "crawler"},
    ]
    daily = mkt_store.totals_by_date(rows)
    assert daily["2026-01-10"]["baemin"] == 500          # tos 만
    assert daily["2026-01-10"]["coupang"] == 300         # 크롤러뿐이면 그대로
    assert daily["2026-01-10"]["delivery"] == 800


# ---------------------------------------------------------------------------
# 요일 베이스라인 / 신호
# ---------------------------------------------------------------------------

def _make_daily(base_amount=1000000, weeks=5, start=date(2026, 7, 1)):
    """월요일 휴무(0원 없음 = 행 자체 없음)인 5주치 가짜 매출."""
    rows = []
    for i in range(weeks * 7):
        d = start + timedelta(days=i)
        if d.weekday() == 0:      # 월요일 휴무
            continue
        rows.append({"sale_date": str(d), "channel": "store",
                     "amount": base_amount, "source": "tos"})
    return rows


def test_weekday_baseline_excludes_closed_days():
    daily = mkt_store.totals_by_date(_make_daily())
    target = date(2026, 7, 29)    # 수요일
    base = mkt_store.weekday_baseline(daily, target)
    assert base == 1000000
    # 휴무일(월)은 표본이 없으니 None
    assert mkt_store.weekday_baseline(daily, date(2026, 7, 27)) is None




# ---------------------------------------------------------------------------
# 캠페인 효과
# ---------------------------------------------------------------------------









# ---------------------------------------------------------------------------
# 타겟 자동 인식
# ---------------------------------------------------------------------------













# ---------------------------------------------------------------------------
# 화면 조립 — 리마인드가 허위로 뜨지 않는가 (DB 는 monkeypatch 로 대체)
# ---------------------------------------------------------------------------



def _ledger_plus_provisional():
    """장부는 7/31까지 매장+배달, 8월은 배달만(크롤러 잠정치) — 실제 운영 모양."""
    rows = []
    d = date(2026, 6, 1)
    while d <= date(2026, 7, 31):
        if d.weekday() != 0:                  # 월요일 휴무
            rows.append({"sale_date": str(d), "channel": "store",
                         "amount": 500000, "source": "tos"})
            rows.append({"sale_date": str(d), "channel": "baemin",
                         "amount": 700000, "source": "tos"})
        d += timedelta(days=1)
    d = date(2026, 8, 1)
    while d <= date(2026, 8, 29):
        if d.weekday() != 0:
            rows.append({"sale_date": str(d), "channel": "baemin",
                         "amount": 700000, "source": "crawler"})
        d += timedelta(days=1)
    return rows




def test_partial_day_excluded_from_baseline():
    """매장 장부 없이 배달만 잡힌 날(partial)은 표본이 안 된다.
    실사고: 2025-12 가 통째로 배달-only 라 2026-01 캘린더가 ▲27개 도배."""
    rows = []
    # 4주간 정상(매장+배달 = 120만), 그 다음 주 같은 요일은 배달만(70만)
    for w in range(4):
        d = date(2026, 6, 3) + timedelta(days=7 * w)   # 수요일들
        rows.append({"sale_date": str(d), "channel": "store",
                     "amount": 500000, "source": "tos"})
        rows.append({"sale_date": str(d), "channel": "baemin",
                     "amount": 700000, "source": "tos"})
    partial_day = date(2026, 7, 1)
    rows.append({"sale_date": str(partial_day), "channel": "baemin",
                 "amount": 700000, "source": "baemin_xls"})
    daily = mkt_store.totals_by_date(rows)
    assert daily[str(partial_day)]["partial"] is True
    # partial 날은 다음 주의 표본에서도 빠진다 (기준선 오염 방지)
    next_wed = partial_day + timedelta(days=7)
    assert mkt_store.weekday_baseline(daily, next_wed) == 1200000


