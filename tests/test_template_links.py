"""템플릿이 없는 화면을 가리키고 있지 않은지 (2026-09-02).

실제로 터진 사고: 인스타 화면을 새로 만들면서 라우트 이름이
`instagram_info` → `instagram_page` 로 바뀌었는데, **홈 템플릿만 옛 이름을
부르고 있었다.** 그 줄은 홈을 열어야만 실행되는 한 줄이라 아무 테스트도
걸리지 않았고, 운영 홈이 500 으로 죽은 채 이틀 가까이 방치됐다.

배포 워크플로의 헬스체크는 이걸 세 번이나 잡아 냈지만(deploy 3회 연속
failure), 파일 업로드가 헬스체크보다 **먼저** 끝나서 운영은 이미 깨진 뒤였고
빨간 배포를 아무도 보지 않았다. 그래서 **푸시 전에** 걸리게 여기서 막는다.

`url_for('없는이름')` 은 Jinja 가 렌더링할 때서야 BuildError 를 던진다 —
화면을 열어보지 않으면 알 수 없다. 그래서 템플릿을 글자로 훑어서 검사한다.
DB 도 네트워크도 쓰지 않으므로 빠르고 언제나 같은 결과가 나온다.
"""

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "service" / "templates"

# url_for('이름' ...) / url_for("이름" ...) 의 첫 인자만 뽑는다.
URL_FOR = re.compile(r"""url_for\(\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]""")


def _template_files():
    return sorted(TEMPLATES.glob("*.html"))


@pytest.fixture(scope="module")
def endpoints():
    """앱이 실제로 가진 화면 이름들."""
    import sys
    sys.path.insert(0, str(ROOT / "service"))
    from service.app import app
    return set(app.view_functions)


def test_템플릿이_하나는_있다():
    """glob 이 빈손이면 아래 검사가 조용히 통과해 버린다."""
    assert len(_template_files()) > 5


@pytest.mark.parametrize("path", _template_files(), ids=lambda p: p.name)
def test_없는_화면을_가리키지_않는다(path, endpoints):
    """템플릿의 모든 url_for 가 실제 라우트를 가리켜야 한다."""
    used = set(URL_FOR.findall(path.read_text(encoding="utf-8")))
    missing = sorted(used - endpoints)
    assert not missing, (
        f"{path.name} 이 없는 화면을 가리킵니다: {missing}\n"
        f"라우트 이름을 바꿨다면 이 템플릿도 같이 고쳐야 합니다."
    )


def test_홈이_인스타_화면을_제대로_가리킨다(endpoints):
    """사고 재발 방지 — 이름이 또 바뀌면 여기서 바로 드러난다."""
    home = (TEMPLATES / "home.html").read_text(encoding="utf-8")
    assert "instagram_info" not in home, "옛 이름(instagram_info)이 되살아났다"
    assert "instagram_page" in endpoints
