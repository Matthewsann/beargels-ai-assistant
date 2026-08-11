"""자동 등록이 안 될 때 원인을 진단하고 고친다 (집 PC에서 더블클릭 실행).

증상: 화면에 '자동 등록 대기 N건'만 쌓이고 11·17·22시가 지나도 등록이 안 됨.

가장 흔한 원인은 **연습 모드**(.env 의 WRITE_DRY_RUN=true)다. 이 상태면 일꾼이
정해진 시각에 정상적으로 돌고 크롬까지 켜지만 실제 등록만 하지 않고 대기로
남긴다. 사장님이 메모장으로 .env 를 찾아 고치는 대신, 이 스크립트가
① 현재 설정을 읽어 알려주고 ② 실게시로 바꿔준다(원본은 .env.bak 로 백업).

⚠️ 실게시로 바꾸면 직원이 '수정 완료'한 답글이 실제 고객에게 등록된다.
   그래서 되돌리는 법(--off)도 함께 제공한다.

사용:
    python scripts/fix_auto_post.py          # 진단 + 실게시로 켜기
    python scripts/fix_auto_post.py --off    # 다시 연습 모드로
    python scripts/fix_auto_post.py --check  # 진단만(변경 없음)
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
KEY = "WRITE_DRY_RUN"


def read_env_lines():
    if not ENV_PATH.exists():
        return None
    return ENV_PATH.read_text(encoding="utf-8").splitlines()


def current_value(lines):
    """.env 에서 WRITE_DRY_RUN 값을 읽는다(주석·미설정이면 None)."""
    for ln in lines:
        s = ln.strip()
        if s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        if k.strip() == KEY:
            return v.strip().strip('"').strip("'")
    return None


def is_dry(value):
    """미설정이면 기본값이 연습 모드(true)다 — worker/agent.py 와 동일 규칙."""
    if value is None:
        return True
    return value.strip().lower() not in ("false", "0", "no")


def set_value(lines, new_value):
    """WRITE_DRY_RUN 줄을 바꾸거나, 없으면 끝에 추가한다."""
    out, found = [], False
    for ln in lines:
        s = ln.strip()
        if not s.startswith("#") and "=" in s and s.split("=", 1)[0].strip() == KEY:
            out.append(f"{KEY}={new_value}")
            found = True
        else:
            out.append(ln)
    if not found:
        out.append(f"{KEY}={new_value}")
    return out


def restart_worker():
    """일꾼(worker/agent.py)을 껐다 켠다 — .env 변경은 재시작해야 적용된다.

    ⚠️ 답글 일꾼은 4_run_bot.bat(인스타 SNS 봇)이 아니라 worker/run_agent.bat
       이고, 창 없이(hidden) 돌기 때문에 사장님이 직접 끄기 어렵다. 그래서
       여기서 기존 프로세스를 종료하고 바로 다시 띄운다. 혹시 실패해도
       5분마다 도는 작업 스케줄러(BeargelsWatchdog)가 살려준다.
    """
    print("\n[일꾼 재시작]")
    if os.name != "nt":
        print("  · 윈도우가 아니라 건너뜁니다(집 PC에서 실행해 주세요).")
        return

    kill_ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -match 'worker[\\\\/]agent\\.py' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", kill_ps],
                       timeout=60, capture_output=True)
        print("  · 돌고 있던 일꾼을 껐습니다.")
    except Exception as e:  # noqa: BLE001 — 못 꺼도 감시자가 5분 안에 처리
        print(f"  · 종료 시도 실패({e}) — 감시자가 곧 정리합니다.")

    agent_bat = ROOT / "worker" / "run_agent.bat"
    if not agent_bat.exists():
        print(f"  · 실행 파일을 찾지 못했습니다: {agent_bat}")
        print("    5분 안에 자동 감시자가 일꾼을 다시 켭니다.")
        return
    try:
        subprocess.Popen([str(agent_bat)], cwd=str(agent_bat.parent),
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        print("  · 새 설정으로 일꾼을 다시 켰습니다. ✅")
        print("    (창 없이 도는 게 정상입니다 — 상태는 답글 화면에서 확인)")
    except Exception as e:  # noqa: BLE001
        print(f"  · 재시작 실패({e}) — 5분 안에 감시자가 자동으로 켭니다.")


def main():
    args = [a.lower() for a in sys.argv[1:]]
    check_only = "--check" in args
    turn_off = "--off" in args

    print("=" * 58)
    print(" 자동 등록 진단 도구")
    print("=" * 58)

    lines = read_env_lines()
    if lines is None:
        print(f"\n[!] .env 파일이 없습니다: {ENV_PATH}")
        print("    1_setup.bat 을 먼저 실행해 설정 파일을 만들어 주세요.")
        return 1

    value = current_value(lines)
    dry = is_dry(value)
    shown = value if value is not None else "(설정 없음 → 기본값 true)"
    print(f"\n현재 설정: {KEY}={shown}")
    if dry:
        print("→ ⚠️ '연습 모드'입니다. 일꾼이 시간 맞춰 돌아도 실제 등록은")
        print("     하지 않고 대기로만 남깁니다. (대기 건수가 쌓이는 원인)")
    else:
        print("→ ✅ '실게시 모드'입니다. 설정은 정상이에요.")

    if check_only:
        return 0

    if turn_off:
        if dry:
            print("\n이미 연습 모드입니다. 바꿀 것이 없습니다.")
            return 0
        new_value, done_msg = "true", "연습 모드로 되돌렸습니다."
    else:
        if not dry:
            print("\n설정은 이미 정상입니다. 그래도 등록이 안 된다면 일꾼이")
            print("옛 설정으로 돌고 있을 수 있어, 다시 시작해 보겠습니다.")
            restart_worker()
            return 0
        new_value, done_msg = "false", "실게시 모드로 바꿨습니다."

    backup = ENV_PATH.parent / ".env.bak"
    shutil.copyfile(ENV_PATH, backup)
    ENV_PATH.write_text("\n".join(set_value(lines, new_value)) + "\n",
                        encoding="utf-8")

    print(f"\n✅ {done_msg} (원본은 {backup.name} 로 백업)")
    restart_worker()
    print("\n이제 11시·17시·22시 중 가장 가까운 시각에 대기 중인 답글이")
    print("배민·쿠팡에 등록되고, 결과가 텔레그램으로 옵니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
