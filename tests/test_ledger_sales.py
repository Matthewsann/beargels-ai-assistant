"""매출 탭 — 사장님이 보고 싶은 것 (2026-09-13).

  실제 매출총액 = 장부 파일 > 매출장부 시트 > 매장_실제_매출총액 (+ 배달)
  폐기율 · 서비스&직원식대 비율 · 단체주문 총액/건수
  주간별 매출 추이 · 요일별 매출 평균 · 매장 vs 배달 비중

계약:
  · '매출장부' 시트는 라벨이 A열, 'N월' 헤더가 2행 — 그 행들을 달별로 읽는다
  · 값이 없는 달은 키를 만들지 않는다(0 으로 채우면 '폐기 0원'으로 읽힌다)
  · 단체주문 건수 행은 선택 — 있으면 읽고 없으면 None
  · 폴더 CSV 를 고를 때 '매출장부' CSV 를 요약으로 착각하지 않는다
  · 포스 20만원 이상 결제로 단체주문을 세지 않는다 — 시트와 안 맞았다
    (6월 262만 vs 시트 274만, 7월 32만 vs 110만; 2026-09-13 실측)
  · 주간 추이는 월요일 시작, partial 날은 매장에서 뺀다
"""
import io
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database import ledger_store as ls  # noqa: E402
from service import dashboard_page as dp  # noqa: E402
from worker import ledger_sheet as lsh  # noqa: E402

# 실제 시트 모양 그대로(2026-09-13 xlsx 덤프에서 옮김). 헤더 2행, 라벨 A열.
SALES_ROWS = [
    ["매출 관리", None, None, None, None, None],
    [None, None, "6월", "7월", "8월", "9월"],
    ["매장_매출총액", None, 16848391, 13895026, 17363069, None],
    ["매장_매출_폐기", None, 10200, None, 119900, None],
    ["매장_매출_서비스&직원식대", None, 275400, 340900, 329500, None],
    ["매장_실제_매출총액", None, 16572991, 13543926, 16913669, 0],
    ["매장_단체주문", None, 1100000, 0, None, None],
    ["배달_매출총액", None, 22052528, 19504687, 17742646, 0],
]


@pytest.fixture(autouse=True)
def _start_ym(monkeypatch):
    # 이 시트 조각은 6월부터 시작한다 — 요약시트 기본(2025-10)과 다르다
    monkeypatch.setattr(ls, "START_YM", "2026-06")


def test_매출장부_행을_달별로_읽는다():
    s = ls.parse_sales_rows(SALES_ROWS)
    assert s["2026-08"] == {"store_waste": 119900, "store_staff_meal": 329500,
                            "store_actual_sales": 16913669}
    assert s["2026-06"]["group_amount"] == 1100000
    assert "group_count" not in s["2026-06"]            # 시트에 행이 없다


def test_값_없는_달은_키가_없다():
    s = ls.parse_sales_rows(SALES_ROWS)
    assert "store_waste" not in s["2026-07"]             # 7월 폐기 빈칸 → 0 아님
    assert s["2026-09"] == {"store_actual_sales": 0}     # 0 은 0 (적힌 값)


def test_단체주문_건수_행이_있으면_읽는다():
    rows = SALES_ROWS + [["매장_단체주문_건수", None, 3, None, None, None]]
    s = ls.parse_sales_rows(rows)
    assert s["2026-06"]["group_count"] == 3 and s["2026-06"]["group_amount"] == 1100000


def test_csv_로도_같다():
    text = "\n".join(",".join("" if c is None else str(c) for c in r) for r in SALES_ROWS)
    assert ls.parse_sales_csv(text)["2026-08"]["store_waste"] == 119900


def test_헤더가_없으면_말한다():
    with pytest.raises(ValueError):
        ls.parse_sales_rows([["아무거나", 1, 2], ["또", 3, 4]])


def test_요약_행에_얹는다():
    rows = [{"ym": "2026-08", "sales_total": 1}, {"ym": "2026-05", "sales_total": 2}]
    ls.merge_sales(rows, ls.parse_sales_rows(SALES_ROWS))
    assert rows[0]["store_actual_sales"] == 16913669
    assert "store_actual_sales" not in rows[1]           # 시트에 없는 달은 그대로


def test_파생_비율과_실제총액():
    r = ls.derive({"ym": "2026-08", "sales_total": 35105715, "store_sales": 17363069,
                   "delivery_sales": 17742646, "store_waste": 119900,
                   "store_staff_meal": 329500, "store_actual_sales": 16913669})
    assert r["waste_rate"] == pytest.approx(119900 / 17363069)
    assert r["staff_rate"] == pytest.approx(329500 / 17363069)
    assert r["actual_total"] == 16913669 + 17742646


def test_시트_항목이_없으면_None():
    r = ls.derive({"ym": "2026-01", "sales_total": 1, "store_sales": 1, "delivery_sales": 1})
    assert r["waste_rate"] is None and r["actual_total"] is None


# ── 폴더 CSV 고르기 ──────────────────────────────────────────────────

def test_매출장부_csv를_요약으로_착각하지_않는다(tmp_path, monkeypatch):
    monkeypatch.setenv("MKT_LEDGER_DIR", str(tmp_path))
    (tmp_path / "베어글스_장부 - 요약.csv").write_text("요약", encoding="utf-8")
    (tmp_path / "베어글스_장부 - 매출장부.csv").write_text("매출", encoding="utf-8")
    assert lsh.find_local_csv().endswith("요약.csv")
    assert lsh.find_local_sales_csv().endswith("매출장부.csv")


# ── 화면 숫자 ────────────────────────────────────────────────────────

def test_상단_4칸():
    R = ls.derive({"ym": "2026-08", "label": "8월", "full": "2026년 8월", "status": "confirmed",
                   "sales_total": 35105715, "store_sales": 17363069, "delivery_sales": 17742646,
                   "store_waste": 119900, "store_staff_meal": 329500, "store_actual_sales": 16913669})
    P = ls.derive({"ym": "2026-07", "sales_total": 33399713, "store_sales": 13895026,
                   "delivery_sales": 19504687, "store_staff_meal": 340900,
                   "store_actual_sales": 13543926, "group_amount": 0})
    k = dp.sales_kpi(R, P)
    assert k["has_sheet"] and k["actual_total"] == 34656315
    assert k["actual_total_pct"] == pytest.approx(34656315 / (13543926 + 19504687) - 1)
    assert k["group_amount"] is None and k["group_count"] is None   # 8월 시트 빈칸
    assert k["waste_rate_prev"] is None                              # 7월 폐기 빈칸


def test_시트_항목이_하나도_없으면_has_sheet_False():
    k = dp.sales_kpi(ls.derive({"ym": "2026-01", "label": "1월", "sales_total": 1}), None)
    assert k["has_sheet"] is False


def day(store, delivery, partial=False):
    return {"store": store, "delivery": delivery, "total": store + delivery, "partial": partial}


def test_주간_추이는_월요일_시작_최근N주():
    end = date(2026, 8, 31)                 # 월요일
    daily = {str(date(2026, 8, d)): day(100, 200) for d in range(1, 32)}
    daily["2026-08-26"] = day(0, 200, partial=True)     # 매장 장부 없는 날
    w = dp.weekly_series(daily, end, weeks=3)
    # 8/31 주는 하루뿐 → 뺀다(막대가 푹 꺼져 급락으로 읽힌다)
    assert w["labels"] == ["8/17", "8/24"]
    assert w["store"] == [700, 600]                      # 8/26 매장 0원은 안 더함
    assert w["delivery"] == [1400, 1400]
    assert w["days"] == [7, 6]


def test_주간_추이_마지막주가_사흘이상이면_남긴다():
    daily = {str(date(2026, 8, d)): day(100, 200) for d in range(17, 27)}   # 8/17~8/26
    w = dp.weekly_series(daily, date(2026, 8, 26), weeks=2)
    assert w["labels"] == ["8/17", "8/24"] and w["days"] == [7, 3]


def test_요일별_평균은_매장_배달_따로():
    daily = {"2026-08-03": day(100, 300), "2026-08-10": day(300, 100),   # 월 두 번
             "2026-08-04": day(50, 50), "2026-08-11": day(0, 0)}         # 화: 휴무 하루 제외
    s = dp.dow_split(daily, date(2026, 8, 1), date(2026, 8, 31))
    assert s["store"][0] == 200 and s["delivery"][0] == 200 and s["total"][0] == 400
    assert s["days"][0] == 2 and s["days"][1] == 1 and s["store"][1] == 50
