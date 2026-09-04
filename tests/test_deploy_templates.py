"""배포 누락 회귀 테스트 — /menu 500 TemplateNotFound(2026-07-29) 재발 방지.

app.py 에 새 화면을 만들면서 .github/workflows/deploy.yml 의 업로드 목록에
템플릿을 추가하지 않으면, 로컬에선 잘 돌지만 PythonAnywhere 에는 파일이 안 올라가
운영에서만 500(TemplateNotFound)이 난다. 실제로 menu.html 이 그랬다(cf8e8f7).

그래서 여기서 확인한다:
  1) app.py 가 렌더하는 템플릿이 service/templates/ 에 실제로 있는가
  2) 그 템플릿(그리고 extends/include 로 끌어쓰는 부모)이 deploy.yml 업로드
     목록에 들어 있는가
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "service" / "app.py"
TEMPLATES = ROOT / "service" / "templates"
DEPLOY = ROOT / ".github" / "workflows" / "deploy.yml"

RENDERED = re.compile(r'render_template\(\s*["\']([\w./-]+\.html)["\']')
INHERITS = re.compile(r'{%-?\s*(?:extends|include)\s+["\']([\w./-]+\.html)["\']')


def _rendered_templates():
    names = set(RENDERED.findall(APP.read_text(encoding="utf-8")))
    # extends/include 로 딸려 올라가야 하는 부모 템플릿까지 펼친다
    seen, queue = set(), list(names)
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        path = TEMPLATES / name
        if path.exists():
            queue.extend(INHERITS.findall(path.read_text(encoding="utf-8")))
    return sorted(seen)


def _deploy_uploads():
    text = DEPLOY.read_text(encoding="utf-8")
    return set(re.findall(r"^\s*upload\s+(\S+)", text, re.MULTILINE))


def _deploy_trigger_paths():
    """on.push.paths 목록 — 이게 안 맞으면 배포가 조용히 안 돈다."""
    text = DEPLOY.read_text(encoding="utf-8")
    block = text.split("paths:", 1)[1].split("workflow_dispatch", 1)[0]
    return set(re.findall(r"^\s*-\s*'([^']+)'", block, re.MULTILINE))


def test_uploaded_non_service_files_also_trigger_deploy():
    """service/ 밖의 파일을 올린다면 그 파일이 바뀔 때 배포도 돌아야 한다.

    실제로 있었던 일(2026-09-04): sns_automation/briefs.py 를 업로드 목록에는
    넣었는데 트리거 경로에는 안 넣어, 그 파일만 고친 커밋은 배포가 안 돌았다.
    운영에는 옛 파일이 남고 아무 에러도 안 난다.
    """
    triggers = _deploy_trigger_paths()
    missing = []
    for up in _deploy_uploads():
        if up.startswith("service/"):
            continue                      # 'service/**' 가 통째로 받는다
        if up in triggers:
            continue
        if any(t.endswith("/**") and up.startswith(t[:-2]) for t in triggers):
            continue
        missing.append(up)
    assert not missing, f"업로드하지만 배포 트리거에 없는 파일: {sorted(missing)}"


def test_app_renders_at_least_one_template():
    # 정규식이 헛돌아 빈 목록으로 무조건 통과하는 걸 막는 안전장치
    assert _rendered_templates(), "app.py 에서 render_template 을 못 찾았다"


@pytest.mark.parametrize("name", _rendered_templates())
def test_template_file_exists(name):
    assert (TEMPLATES / name).exists(), f"service/templates/{name} 가 없다"


@pytest.mark.parametrize("name", _rendered_templates())
def test_template_is_in_deploy_list(name):
    uploads = _deploy_uploads()
    rel = f"service/templates/{name}"
    assert rel in uploads, (
        f"deploy.yml 업로드 목록에 '{rel}' 이 빠졌다. "
        f"이러면 운영(PythonAnywhere)에서만 500 TemplateNotFound 가 난다."
    )
