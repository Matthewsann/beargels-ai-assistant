"""직원용 웹(PythonAnywhere)에 코드를 반영한다 — git pull + Reload.

그동안은 사장님이 PA 에 로그인해 Bash 창에서 pull 하고 Web 탭에서 Reload 를
눌러야 했다. API 토큰만 있으면 그 두 단계를 여기서 대신 한다.

준비 (한 번만):
    1. https://www.pythonanywhere.com/user/beargels/account/#api_token
       에서 "Create a new API token" (이미 있으면 그 값을 복사)
    2. 저장소 루트 .env 에 아래 한 줄 추가 — 채팅에 붙여넣지 말 것
           PA_API_TOKEN=발급받은토큰
       (.env 는 .gitignore 라 GitHub 에 안 올라간다)

쓰는 법:
    python scripts/deploy_pa.py              # push 된 최신 코드를 반영
    python scripts/deploy_pa.py --no-pull    # Reload 만

콘솔에 대해: PA API 로 새로 만든 콘솔은 브라우저에서 한 번 열어줘야 깨어난다.
그래서 **이미 만들어 둔 Bash 콘솔**을 찾아 재사용한다. 하나도 없으면 만들되,
사장님께 한 번 열어달라고 안내한다.
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

PULL_CMD = f"cd {REPO} && git fetch origin && git reset --hard origin/main && git log --oneline -1"


def api(method: str, path: str, body: dict | None = None):
    url = f"{HOST}/api/v0/user/{USER}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Token {TOKEN}",
                 "Content-Type": "application/json"})
    # PA API 가 가끔 느리다(특히 Reload). 한 번 늦었다고 배포가 죽으면
    # 실제로는 반영됐는데 실패로 보여 사람을 헷갈리게 한다 — 두어 번 더 기다린다.
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                raw = r.read().decode("utf-8", "replace")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise SystemExit(f"API 실패 {e.code} {method} {path}\n  {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            if attempt < 2:
                time.sleep(5)
    raise SystemExit(f"API 응답 없음 {method} {path} — {last}")


def pick_console() -> dict | None:
    """살아 있는 Bash 콘솔 하나. 없으면 None."""
    consoles = api("GET", "/consoles/")
    bash = [c for c in consoles if (c.get("executable") or "").endswith("bash")]
    return bash[0] if bash else None


ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
DONE = "___DEPLOY_DONE___"


def run_pull(console: dict) -> tuple[str, bool]:
    """콘솔에서 pull 을 돌리고 (내 명령의 출력, 성공여부) 를 준다.

    get_latest_output 은 콘솔에 남아 있던 **이전 출력까지 통째로** 준다.
    그래서 거기서 'error' 를 찾으면 지난번 실패가 이번 실패로 둔갑한다
    (실제로 그랬다). 끝에 표시를 찍고 그 앞뒤로만 잘라서 본다.
    """
    cid = console["id"]
    api("POST", f"/consoles/{cid}/send_input/",
        {"input": f"{PULL_CMD}; echo {DONE}$?\n"})
    out = ""
    for _ in range(12):
        time.sleep(3)
        raw = api("GET", f"/consoles/{cid}/get_latest_output/").get("output", "")
        out = ANSI.sub("", raw)
        # 표시가 '명령을 되울린 줄'이 아니라 '실행 결과'로 찍혔는지 본다.
        if len(re.findall(rf"{DONE}(\d+)", out)) >= 1:
            break
    codes = re.findall(rf"{DONE}(\d+)", out)
    if not codes:
        return out[-800:], False
    mine = out.rsplit(DONE, 1)[0]           # 마지막 표시 앞까지가 이번 실행분
    start = mine.rfind("git fetch origin")   # 내가 보낸 명령이 되울린 자리
    body = mine[start:] if start >= 0 else mine[-800:]
    return body.strip(), codes[-1] == "0"


def main() -> int:
    ap = argparse.ArgumentParser(description="PythonAnywhere 배포")
    ap.add_argument("--no-pull", action="store_true", help="Reload 만 한다")
    args = ap.parse_args()

    if not TOKEN:
        print("PA_API_TOKEN 이 없습니다. 아래를 한 번만 해주세요:\n"
              f"  1. {HOST}/user/{USER}/account/#api_token 에서 토큰 발급\n"
              "  2. 저장소 .env 에  PA_API_TOKEN=발급받은토큰  한 줄 추가\n"
              "     (채팅에 붙여넣지 마세요 — .env 는 GitHub 에 안 올라갑니다)")
        return 1

    if not args.no_pull:
        console = pick_console()
        if console is None:
            made = api("POST", "/consoles/",
                       {"executable": "bash", "arguments": "", "working_directory": REPO})
            print("Bash 콘솔을 새로 만들었습니다. PA 웹에서 한 번 열어 깨운 뒤 "
                  f"다시 실행해 주세요:\n  {HOST}/user/{USER}/consoles/{made.get('id')}/")
            return 1
        print(f"콘솔 {console['id']} 에서 최신 코드 받는 중…")
        out, ok = run_pull(console)
        print("\n".join(out.splitlines()[-6:]) or "(출력 없음)")
        if not ok:
            print("\n⚠ pull 이 실패했습니다 — Reload 하지 않고 멈춥니다.")
            return 1

    print("웹앱 Reload 중…")
    r = api("POST", f"/webapps/{DOMAIN}/reload/")
    print(f"Reload {r.get('status', 'OK')} → https://{DOMAIN}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
