"""임시 공개 주소로 열기 (Cloudflare quick tunnel) — 쓸 때마다 실행.

Tailscale 앱을 못 깔는 기기에서 급히 써야 할 때만 쓴다.
주소가 매번 바뀌고 **인터넷에 공개**되므로 비밀번호가 반드시 있어야 한다.
평소에는 고정 주소(setup_fixed_address.py = Tailscale) 를 쓴다.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
ENVFILE = ROOT / ".env"
APP = HERE / "app.py"
PORT = 5050


def ask(prompt: str) -> str:
    """입력 받기. 창 없이 실행돼 입력을 못 받으면 빈 문자열."""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def has_password() -> bool:
    if not ENVFILE.exists():
        return False
    return any(l.startswith("BEARGELS_WEB_PASSWORD=")
               for l in ENVFILE.read_text(encoding="utf-8").splitlines())


def ask_password() -> bool:
    print("인터넷에 공개하는 주소라서 비밀번호가 반드시 있어야 해요.")
    print("다른 사람은 못 맞출 만한 걸로, 12자 이상 추천.")
    pw = ask("새 비밀번호: ")
    if not pw:
        print("[!] 비밀번호 없이는 열 수 없어요. 다시 실행해주세요.")
        return False
    text = ENVFILE.read_text(encoding="utf-8") if ENVFILE.exists() else ""
    with ENVFILE.open("a", encoding="utf-8") as f:
        if text and not text.endswith("\n"):
            f.write("\n")
        f.write(f"BEARGELS_WEB_PASSWORD={pw}\n")
    print("저장했어요(.env). 다음부터는 안 물어봐요.\n")
    return True


def find_cloudflared() -> str | None:
    exe = shutil.which("cloudflared")
    if exe:
        return exe
    p = pathlib.Path(r"C:\Program Files (x86)\cloudflared\cloudflared.exe")
    return str(p) if p.exists() else None


def main() -> int:
    print("=" * 60)
    print(" 베어글스 AI 비서 — 밖에서 쓰기 (임시 공개 주소)")
    print("=" * 60)
    print()

    if not has_password() and not ask_password():
        return 1

    cf = find_cloudflared()
    if not cf:
        print("[!] 터널 프로그램(cloudflared)이 없어요. 아래를 한 번만 실행하세요:")
        print()
        print("      winget install --id Cloudflare.cloudflared")
        print()
        print("설치가 끝나면 이 파일을 다시 실행해주세요.")
        return 1

    print(f"[1/2] 웹앱 시작 (localhost:{PORT})...")
    subprocess.Popen([sys.executable, str(APP)], cwd=str(HERE),
                     creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))

    print("[2/2] 외부 접속 주소 만드는 중...")
    print()
    print("  조금 뒤 아래에 https://... .trycloudflare.com 주소가 나와요.")
    print("  그 주소를 휴대폰/다른 컴퓨터 브라우저에 입력하면 됩니다.")
    print("  (이 창을 닫으면 외부 접속이 끊깁니다)")
    print()
    try:
        subprocess.run([cf, "tunnel", "--url", f"http://localhost:{PORT}"])
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    os.chdir(HERE)
    sys.exit(main())
