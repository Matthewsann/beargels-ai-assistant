"""화면 JS 문법 회귀 테스트 — 버튼이 통째로 먹통되던 사고(2026-08-27) 재발 방지.

무슨 일이 있었나:
    답글 실패 안내 문구를 고치면서 JS 문자열 안에 **진짜 줄바꿈**이 들어갔다.

        ? '
        브라우저 연결이 잠깐 끊긴 거예요. …'

    자바스크립트에서 따옴표 문자열은 줄을 넘을 수 없어 SyntaxError 가 나고,
    그러면 그 <script> 블록이 통째로 실행되지 않는다. 즉 함수 하나가 아니라
    **그 화면의 모든 버튼**(등록·AI 재생성·복사·넘김)이 한꺼번에 죽는다.
    서버는 200 을 주고 화면도 멀쩡히 그려지니 겉으론 멀쩡해 보인다 —
    사장님이 "ai 생성 버튼 먹통임" 이라고 알려주기 전까지 아무도 몰랐다.

여기서 확인하는 것:
    각 화면을 실제로 렌더한 뒤 <script> 블록을 꺼내 문법을 검사한다.
    node 가 있으면 `node --check`, 없으면 최소한 '따옴표 문자열 안 줄바꿈'
    만이라도 잡는다(그게 이번 사고의 형태다).
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "service"))

SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)

# 검사할 화면 — 버튼이 있는 곳은 전부.
PAGES = ["", "todo", "care", "history", "reviews", "review", "menu",
         "meeting", "meeting/new", "instagram"]


def _client():
    try:
        import app as service_app
    except Exception as e:  # noqa: BLE001 — .env 없는 환경에선 건너뛴다
        pytest.skip(f"service app 을 불러올 수 없음: {str(e)[:80]}")
    if not service_app.SERVICE_PATH:
        pytest.skip("SERVICE_PATH 가 없어 화면을 열 수 없음")
    return service_app.app.test_client(), service_app.SERVICE_PATH


def _unterminated_string_line(js: str):
    """따옴표 문자열이 줄 끝에서 안 닫힌 첫 줄 번호(없으면 None).

    node 가 없을 때 쓰는 최소 검사. 완벽한 파서가 아니라 이번 사고 형태
    ('...' 안에서 줄바꿈)를 잡는 것이 목적이다.
    """
    for n, line in enumerate(js.split("\n"), 1):
        stripped = line.split("//")[0]
        for quote in ("'", '"'):
            # 이스케이프되지 않은 따옴표 개수가 홀수면 그 줄에서 안 닫혔다.
            count = len(re.findall(r"(?<!\\)" + quote, stripped))
            if count % 2 == 1:
                return n, line.strip()[:60]
    return None


@pytest.mark.parametrize("page", PAGES)
def test_screen_javascript_parses(page, tmp_path):
    client, key = _client()
    resp = client.get(f"/{key}/{page}")
    assert resp.status_code == 200, f"/{page} 가 {resp.status_code}"
    html = resp.get_data(as_text=True)

    blocks = SCRIPT.findall(html)
    assert blocks, f"/{page} 에 <script> 블록이 없다"

    node = shutil.which("node")
    for i, js in enumerate(blocks):
        if not js.strip():
            continue
        if node:
            # 화면 이름에 / 가 있으면(meeting/new) 파일 이름으로 못 쓴다
            safe = (page or "home").replace("/", "_")
            f = tmp_path / f"{safe}_{i}.js"
            f.write_text(js, encoding="utf-8")
            r = subprocess.run([node, "--check", str(f)],
                               capture_output=True, text=True, timeout=30)
            assert r.returncode == 0, (
                f"/{page} script[{i}] 문법 오류 — 이 화면의 버튼이 전부 먹통이 된다\n"
                f"{r.stderr[:400]}")
        else:
            bad = _unterminated_string_line(js)
            assert bad is None, (
                f"/{page} script[{i}] {bad[0]}번째 줄에서 따옴표 문자열이 "
                f"안 닫혔다(줄바꿈이 들어간 듯): {bad[1]}")


def test_meeting_detail_javascript_parses(tmp_path):
    """meeting_detail.html 은 id 가 있어야 열리는 화면이라 PAGES 목록에 못
    넣는다 — 실제 회의 1건을 만들어 렌더한 뒤 같은 검사를 하고 지운다."""
    client, key = _client()
    from database import meeting_store as mt
    mid = mt.create_meeting("JS 문법 검사용 임시 회의", body="본문")
    try:
        resp = client.get(f"/{key}/meeting/{mid}")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        blocks = [b for b in SCRIPT.findall(html) if b.strip()]
        assert blocks, "meeting_detail.html 에 <script> 블록이 없다"
        node = shutil.which("node")
        for i, js in enumerate(blocks):
            if node:
                f = tmp_path / f"meeting_detail_{i}.js"
                f.write_text(js, encoding="utf-8")
                r = subprocess.run([node, "--check", str(f)],
                                   capture_output=True, text=True, timeout=30)
                assert r.returncode == 0, (
                    f"meeting_detail script[{i}] 문법 오류\n{r.stderr[:400]}")
            else:
                bad = _unterminated_string_line(js)
                assert bad is None, f"meeting_detail {bad[0]}번째 줄: {bad[1]}"
    finally:
        mt.delete_meeting(mid)


def test_the_2026_08_27_bug_shape_is_detected():
    """검사기가 실제 사고 코드를 잡는지 — 테스트가 헛돌지 않게."""
    broken = "\n".join([
        "function f() {",
        "  alert('앞부분",
        "  뒷부분');",
        "}",
    ])
    assert _unterminated_string_line(broken) is not None
    ok = "function f() {\n  alert('한 줄짜리');\n}"
    assert _unterminated_string_line(ok) is None
