"""장부를 로그인 없이도 읽는다 — 장부관리 폴더의 CSV (2026-09-09).

왜: 이 가게 OAuth 앱(beargels-sns)이 **조직 전용**으로 잠겨 있어 개인 지메일로
로그인이 안 된다(403 org_internal — myeonggu96·beargelssongdo 둘 다 차단).
그래서 구글 로그인이 영영 안 풀려도 장부가 최신으로 유지되는 길을 둔다:
사장님이 시트를 CSV 로 내려받아 장부관리 폴더(TOS 엑셀 올리는 그 폴더)에
넣으면 일꾼이 그걸 읽는다.

계약:
  · 폴더에 '장부' 들어간 CSV 가 있으면 그걸 쓴다(구글 시트를 안 부른다)
  · 여러 개면 가장 최근 것
  · 확정/예상은 그 파일의 수정 시각으로 가른다
  · 폴더에 없을 때만 구글 시트를 부른다
  · 실패 메시지는 사장님이 할 수 있는 말로 바뀐다
"""
import io
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database import ledger_store as ls  # noqa: E402
from worker import ledger_sheet as lsh  # noqa: E402

CSV = """요약,,,,,,,,,,,,,,,,,
,목표,최저,누적,평균,10월,11월,12월,1월,2월,3월,4월,5월,6월,7월,8월,,,11월
매출총액,"40,000,000",,,,"20,877,490","28,772,434","32,972,850","26,654,706","31,404,344","31,499,341","35,670,114","45,674,996","38,900,919","33,399,713","35,105,715",,,
매장매출,"18,000,000",,,,"12,770,245","14,223,800","14,795,105","11,268,945","17,322,740","13,583,665","16,378,127","18,498,543","16,848,391","13,895,026","17,363,069",,,
배달매출,"22,000,000",,,,"8,107,245","14,548,634","18,177,745","15,385,761","14,609,304","17,915,676","19,291,987","27,176,453","22,052,528","19,504,687","17,742,646",,,
정산총액(실입금금액),"30,660,000",,,,"16,000,516","19,763,568","24,071,673","19,837,512","24,607,961","22,901,314","27,129,226","31,147,993","28,551,795","25,659,250","26,830,008",,,
매입원가 총액,"14,000,000",,,,"9,131,893","11,350,004","14,168,110","12,691,739","12,437,571","12,092,940","12,186,020","17,549,321","15,275,440","14,174,530","12,287,000",,,
고정비_총액,"13,850,000",,,,"8,368,080","9,418,560","9,852,980","8,375,440","9,597,035","9,547,505","10,676,348","10,554,240","12,593,270","12,145,030","10,766,784",,,
영업이익,"1,210,000",,,,"-1,499,457","-1,004,996","50,583","-1,229,667","2,573,355","1,260,869","4,266,858","3,044,432","683,085","-660,310","3,776,223",,,
"""


@pytest.fixture()
def folder(tmp_path, monkeypatch):
    """가짜 장부관리 폴더 — 진짜 드라이브를 건드리지 않는다."""
    monkeypatch.setenv("MKT_LEDGER_DIR", str(tmp_path))
    return tmp_path


def _put(folder, name, text=CSV, encoding="utf-8-sig", when=None):
    p = folder / name
    p.parent.mkdir(parents=True, exist_ok=True)
    io.open(p, "w", encoding=encoding, newline="").write(text)
    if when:
        os.utime(p, (when, when))
    return p


# ---------------------------------------------------------------------------
# 폴더에서 찾기
# ---------------------------------------------------------------------------

def test_이름에_장부가_든_csv를_찾는다(folder):
    _put(folder, "베어글스_장부 - 요약.csv")
    assert lsh.find_local_csv() == str(folder / "베어글스_장부 - 요약.csv")


def test_하위_월폴더에_넣어도_찾는다(folder):
    _put(folder, "2026년 8월/베어글스_장부.csv")
    assert lsh.find_local_csv().endswith("베어글스_장부.csv")


def test_상관없는_csv는_안_집는다(folder):
    _put(folder, "카드매출내역.csv")
    assert lsh.find_local_csv() is None


def test_여러개면_가장_최근것(folder):
    old = time.time() - 86400
    _put(folder, "장부_예전.csv", when=old)
    _put(folder, "장부_최신.csv")
    assert lsh.find_local_csv().endswith("장부_최신.csv")


def test_폴더가_없어도_안_죽는다(tmp_path, monkeypatch):
    monkeypatch.setenv("MKT_LEDGER_DIR", str(tmp_path / "없는폴더"))
    assert lsh.find_local_csv() is None


# ---------------------------------------------------------------------------
# 읽기
# ---------------------------------------------------------------------------

def test_BOM붙은_UTF8을_읽는다(folder):
    p = _put(folder, "장부.csv")
    text, mod = lsh.read_local_csv(str(p))
    assert text.startswith("요약") and mod is not None


def test_엑셀이_저장한_cp949도_읽는다(folder):
    p = _put(folder, "장부.csv", encoding="cp949")
    text, _ = lsh.read_local_csv(str(p))
    assert "매출총액" in text


def test_파일_수정시각이_확정_예상을_가른다(folder):
    """9월에 내려받은 파일이면 8월은 확정이다."""
    p = _put(folder, "장부.csv",
             when=datetime(2026, 9, 9, 12, 0).timestamp())
    text, mod = lsh.read_local_csv(str(p))
    rows, _ = ls.parse_summary_csv(text, mod)
    st = {r["ym"]: r["status"] for r in rows}
    assert st["2026-08"] == "confirmed"


def test_달이_끝나기_전_파일이면_그_달은_예상(folder):
    p = _put(folder, "장부.csv",
             when=datetime(2026, 8, 20, 12, 0).timestamp())
    text, mod = lsh.read_local_csv(str(p))
    rows, _ = ls.parse_summary_csv(text, mod)
    st = {r["ym"]: r["status"] for r in rows}
    assert st["2026-08"] == "estimate" and st["2026-07"] == "confirmed"


# ---------------------------------------------------------------------------
# 어느 길로 갈지
# ---------------------------------------------------------------------------

def test_폴더에_있으면_구글을_안_부른다(folder, monkeypatch):
    _put(folder, "장부.csv")
    monkeypatch.setattr(lsh, "fetch_summary_csv",
                        lambda: pytest.fail("폴더 CSV 가 있는데 구글을 불렀다"))
    text, mod, source = lsh.load_summary()
    assert "폴더 CSV" in source and "매출총액" in text


def test_폴더에_없으면_구글을_부른다(folder, monkeypatch):
    monkeypatch.setattr(lsh, "fetch_summary_csv",
                        lambda: (CSV, datetime(2026, 9, 9, tzinfo=timezone.utc)))
    _, _, source = lsh.load_summary()
    assert source == "구글 시트"


def test_반영결과에_어디서_읽었는지_남는다(folder, monkeypatch):
    _put(folder, "장부.csv")
    seen = {}
    monkeypatch.setattr(ls, "upsert_ledger", lambda rows: seen.setdefault("rows", rows) and len(rows) or len(rows))
    monkeypatch.setattr(ls, "set_ledger_targets", lambda t: None)
    r = lsh.sync()
    assert r["ok"] and "폴더 CSV" in r["source"] and "폴더 CSV" in r["note"]
    assert {x["ym"] for x in seen["rows"]} >= {"2026-07", "2026-08"}


# ---------------------------------------------------------------------------
# 실패 안내 — 사장님이 할 수 있는 말로
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("msg", [
    "인증 파일이 없습니다: C:\\...\\token.json",
    "403 오류: org_internal",
    "구글 로그인을 완료하세요",
])
def test_로그인이_막힌_경우엔_CSV_길을_안내한다(msg):
    cause, fix = ls.explain_sync_error(msg)
    assert "로그인" in cause
    assert "다운로드" in fix and "장부관리 폴더" in fix


def test_모르는_오류도_할_일을_준다():
    cause, fix = ls.explain_sync_error("HTTP 500 무슨무슨 오류")
    assert cause and "장부관리 폴더" in fix
