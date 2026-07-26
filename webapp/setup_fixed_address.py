"""고정 주소 만들기 (Tailscale) — 집 PC에서 한 번만 실행.

하는 일:
  1. Tailscale 설치·로그인 확인
  2. 웹앱 비밀번호(.env BEARGELS_WEB_PASSWORD) 없으면 만들어 저장
  3. `tailscale serve` 로 https://<PC>.<tailnet>.ts.net → localhost:5050 고정 연결
     (재부팅해도 유지됨)
  4. (선택) PC 켤 때 웹앱 자동 시작 등록(작업 스케줄러)
  5. 최종 고정 주소 출력

⚠️ 인터넷에 공개하지 않는다. Tailscale 로 로그인한 내 기기끼리만 연결된다.
   (공개 주소가 필요하면 start_public.bat = Cloudflare 임시 주소를 쓴다.)

bat 대신 파이썬으로 둔 이유: 배치파일은 한글·따옴표 조합에서 인자가 깨진다.
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
TASK_NAME = "BeargelsAssistant"


def line(msg=""):
    print(msg, flush=True)


def ask(prompt: str) -> str:
    """입력 받기. 입력을 못 받는 환경이면 빈 문자열(건너뜀)."""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def run(args, **kw):
    """외부 명령 실행 — 리스트로 넘겨 따옴표 문제를 원천 차단."""
    return subprocess.run(args, text=True, encoding="utf-8",
                          errors="replace", **kw)


def find_tailscale() -> str | None:
    exe = shutil.which("tailscale")
    if exe:
        return exe
    for p in (r"C:\Program Files\Tailscale\tailscale.exe",
              r"C:\Program Files (x86)\Tailscale\tailscale.exe"):
        if pathlib.Path(p).exists():
            return p
    return None


def ensure_password() -> None:
    text = ENVFILE.read_text(encoding="utf-8") if ENVFILE.exists() else ""
    if any(l.startswith("BEARGELS_WEB_PASSWORD=") for l in text.splitlines()):
        line("[2/5] 웹앱 비밀번호 이미 있음 — 그대로 씁니다.")
        return
    line("[2/5] 웹앱 비밀번호가 아직 없어요. 하나 정해주세요(12자 이상 추천).")
    pw = ask("      새 비밀번호: ")
    if not pw:
        line("      건너뜀 — 잠금 없이 열립니다(Tailscale 안에서만 접속 가능).")
        return
    with ENVFILE.open("a", encoding="utf-8") as f:
        if text and not text.endswith("\n"):
            f.write("\n")
        f.write(f"BEARGELS_WEB_PASSWORD={pw}\n")
    line("      저장했어요(.env). 이 비밀번호로 로그인합니다.")


def register_autostart(pythonw: str) -> None:
    """PC 로그온 시 웹앱 자동 시작(작업 스케줄러). 창 없이 pythonw 로 띄운다."""
    r = run(["schtasks", "/create", "/f", "/tn", TASK_NAME,
             "/sc", "onlogon", "/rl", "limited",
             "/tr", f'"{pythonw}" "{APP}"'],
            capture_output=True)
    if r.returncode != 0:
        line("      [!] 자동 시작 등록 실패 — run.bat 으로 직접 켜면 됩니다.")
        line(f"      ({(r.stderr or r.stdout or '').strip()[:200]})")
        return
    line("      등록 완료. 지금 바로 한 번 켭니다.")
    subprocess.Popen([pythonw, str(APP)],
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))


def main() -> int:
    line("=" * 60)
    line(" 베어글스 AI 비서 — 고정 주소 만들기 (한 번만)")
    line("=" * 60)
    line()

    ts = find_tailscale()
    if not ts:
        line("[!] Tailscale 이 없어요. 명령 프롬프트에서 아래를 한 번만 실행하세요:")
        line()
        line("      winget install --id tailscale.tailscale")
        line()
        line("설치가 끝나면 이 파일을 다시 실행해주세요.")
        return 1

    line("[1/5] Tailscale 로그인 확인...")
    if run([ts, "status"], capture_output=True).returncode != 0:
        line("      로그인이 안 돼 있어요. 브라우저가 열리면 구글 계정으로 로그인해주세요.")
        if run([ts, "up"]).returncode != 0:
            line("[!] 로그인이 안 됐어요. 다시 실행해주세요.")
            return 1
    line("      로그인 OK")

    ensure_password()

    line(f"[3/5] 고정 주소 연결 (→ localhost:{PORT})...")
    r = run([ts, "serve", "--bg", "--https=443", f"http://localhost:{PORT}"],
            capture_output=True)
    if r.returncode != 0:
        line("[!] 고정 주소 연결 실패. 보통 원인은 둘 중 하나입니다:")
        line("    1) MagicDNS / HTTPS 를 켜야 함 →")
        line("       https://login.tailscale.com/admin/dns 에서 두 개 모두 Enable")
        line("    2) 로그인 계정이 다름")
        line()
        line((r.stderr or r.stdout or "").strip()[:600])
        return 1
    line("      연결 완료 (재부팅해도 유지됩니다)")

    line("[4/5] 이 PC를 켜면 비서가 자동으로 켜지게 등록할까요?")
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    if not pathlib.Path(pythonw).exists():
        pythonw = sys.executable
    if ask("      등록하려면 y, 건너뛰려면 엔터: ").lower() == "y":
        register_autostart(pythonw)
    else:
        line("      건너뜀 — 쓸 때 run.bat 을 켜면 됩니다.")

    line("[5/5] 끝났어요. 앞으로 계속 쓰는 고정 주소입니다:")
    line()
    st = run([ts, "serve", "status"], capture_output=True)
    line((st.stdout or "").strip() or "(주소 확인: tailscale serve status)")
    line()
    line("  * 휴대폰/다른 컴퓨터에도 Tailscale 앱을 깔고 같은 계정으로 로그인하면")
    line("    위 https:// 주소가 바로 열립니다.")
    line("  * 주소는 다시 바뀌지 않아요. 즐겨찾기에 넣어두세요.")
    line("  * 집 PC가 켜져 있어야 접속됩니다.")
    return 0


if __name__ == "__main__":
    os.chdir(HERE)
    sys.exit(main())
