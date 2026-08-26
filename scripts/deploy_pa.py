"""직원용 웹(PythonAnywhere)에 코드를 반영한다 — 파일 업로드 + Reload.

그동안은 사장님이 PA 에 로그인해 Bash 창에서 pull 하고 Web 탭에서 Reload 를
눌러야 했다. API 토큰만 있으면 그 두 단계를 여기서 대신 한다.

왜 git pull 이 아니라 파일 업로드인가 (2026-08-27 전환):
    처음엔 콘솔 API(send_input)로 git pull 을 보냈는데, 콘솔이 브라우저로
    한 번 열려 '시작된' 상태가 아니면 412 로 실패했다. GitHub Actions 배포
    (deploy.yml)와 똑같이, 콘솔 상태에 의존하지 않는 Files API 로 파일을
    직접 올린다. **올릴 파일 목록은 deploy.yml 의 upload 줄에서 읽는다** —
    목록이 두 곳으로 갈라지면 반드시 한쪽이 늦는다.

⚠️ 업로드는 로컬 파일 기준이다. 커밋 안 한 수정분도 그대로 올라가니,
   배포 전에 commit+push 를 먼저 해서 GitHub 과 서버를 맞춰 둘 것.

준비 (한 번만):
    1. https://www.pythonanywhere.com/user/beargels/account/#api_token
       에서 "Create a new API token" (이미 있으면 그 값을 복사)
    2. 저장소 루트 .env 에 아래 한 줄 추가 — 채팅에 붙여넣지 말 것
           PA_API_TOKEN=발급받은토큰
       (.env 는 .gitignore 라 GitHub 에 안 올라간다)

쓰는 법:
    python scripts/deploy_pa.py              # 파일 올리고 Reload + 확인
    python scripts/deploy_pa.py --reload-only    # Reload 만
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

HOST = os.getenv("PA_HOST", "https://www.pythonanywhere.com")
USER = os.getenv("PA_USER", "beargels")
DOMAIN = os.getenv("PA_DOMAIN", f"{USER}.pythonanywhere.com")
TOKEN = (os.getenv("PA_API_TOKEN") or "").strip()
REPO = os.getenv("PA_REPO", f"/home/{USER}/beargels-ai-assistant")
SERVICE_PATH = (os.getenv("SERVICE_PATH") or "").strip().strip("/")

WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"


def deploy_files() -> list[str]:
    """deploy.yml 의 `upload 경로` 줄들 — 배포 파일 목록의 단일 원천."""
    text = WORKFLOW.read_text(encoding="utf-8")
    files = re.findall(r"^\s*upload (\S+)$", text, re.MULTILINE)
    if not files:
        raise SystemExit("deploy.yml 에서 upload 목록을 못 찾았습니다.")
    return files


def request(method: str, path: str, data: bytes | None = None,
            content_type: str | None = None, tries: int = 3):
    """PA API 호출. 일시 오류(타임아웃·5xx)는 몇 번 더 시도한다."""
    url = f"{HOST}/api/v0/user/{USER}{path}"
    headers = {"Authorization": f"Token {TOKEN}"}
    if content_type:
        headers["Content-Type"] = content_type
    last = None
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            if e.code >= 500 and attempt < tries - 1:
                last = f"HTTP {e.code} {body}"
                time.sleep(5)
                continue
            raise SystemExit(f"API 실패 {e.code} {method} {path}\n  {body}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            if attempt < tries - 1:
                time.sleep(5)
    raise SystemExit(f"API 응답 없음 {method} {path} — {last}")


def upload(rel: str) -> None:
    """파일 하나를 서버의 같은 경로에 올린다 (multipart/form-data)."""
    local = ROOT / rel
    if not local.exists():
        raise SystemExit(f"로컬에 없는 파일: {rel}")
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="content"; filename="{local.name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + local.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    request("POST", f"/files/path{REPO}/{rel}", data=body,
            content_type=f"multipart/form-data; boundary={boundary}")
    print(f"  올림: {rel}")


def health_check() -> bool:
    """배포 뒤 직원 화면이 실제로 뜨는지(200) 확인."""
    if not SERVICE_PATH:
        print("(SERVICE_PATH 가 없어 화면 확인은 건너뜁니다)")
        return True
    url = f"https://{DOMAIN}/{SERVICE_PATH}/"
    time.sleep(8)   # Reload 직후엔 앱이 아직 뜨는 중일 수 있다
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                print(f"직원 화면 응답: {r.status}")
                return r.status == 200
        except Exception as e:  # noqa: BLE001
            if attempt < 2:
                time.sleep(8)
                continue
            print(f"⚠ 화면 확인 실패: {e}")
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="PythonAnywhere 배포")
    ap.add_argument("--reload-only", "--no-pull", dest="reload_only",
                    action="store_true", help="파일은 안 올리고 Reload 만")
    args = ap.parse_args()

    if not TOKEN:
        print("PA_API_TOKEN 이 없습니다. 아래를 한 번만 해주세요:\n"
              f"  1. {HOST}/user/{USER}/account/#api_token 에서 토큰 발급\n"
              "  2. 저장소 .env 에  PA_API_TOKEN=발급받은토큰  한 줄 추가\n"
              "     (채팅에 붙여넣지 마세요 — .env 는 GitHub 에 안 올라갑니다)")
        return 1

    if not args.reload_only:
        files = deploy_files()
        print(f"파일 {len(files)}개 올리는 중… (목록: deploy.yml)")
        for rel in files:
            upload(rel)

    print("웹앱 Reload 중…")
    status, raw = request("POST", f"/webapps/{DOMAIN}/reload/")
    try:
        msg = json.loads(raw).get("status", status)
    except ValueError:
        msg = status
    print(f"Reload {msg} → https://{DOMAIN}/")

    return 0 if health_check() else 1


if __name__ == "__main__":
    raise SystemExit(main())
