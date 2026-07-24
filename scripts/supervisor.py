"""봇/스케줄러 24h 자동 재시작 감시자(supervisor).

크래시가 나도 자동으로 다시 띄우고, 재시작을 로그로 남긴다. 붙을 Chrome
(launch_chrome.bat, --remote-debugging-port=9222)이 죽어 있으면 먼저 되살린다.

사용:
    python -m scripts.supervisor                # 텔레그램 봇 감시(기본)
    python -m scripts.supervisor bot.telegram_bot
    python -m scripts.supervisor scheduler.jobs # 스케줄러 감시

동작:
  1) 대상 파이썬 모듈을 자식 프로세스로 실행(`python -m <module>`).
  2) 프로세스가 종료되면 사유(exit code)를 로그에 남기고 백오프 후 재시작.
  3) 시작 전마다 CDP 포트(9222)를 확인 — 죽어 있으면 전용 프로필 Chrome 을
     원격 디버깅 모드로 되살린다(attach 크롤링 전제 유지).
  4) 짧은 시간 반복 크래시면 백오프를 늘려(최대 60s) 크래시 루프를 완화한다.

Ctrl+C 로 감시자와 자식 프로세스를 함께 종료한다.
로그: logs/supervisor.log (+ 콘솔).
"""

import logging
import os
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
PROFILE_DIR = PROJECT_ROOT / ".browser_profile"
CDP_PORT = int(os.getenv("CDP_PORT", "9222"))
KST = timezone(timedelta(hours=9))

DEFAULT_TARGET = "bot.telegram_bot"

# 재시작 백오프(초): 짧게 죽으면 점점 늘림, 오래 살면 리셋.
BACKOFF_START = 3
BACKOFF_MAX = 60
HEALTHY_RUNTIME = 60  # 이 시간 이상 살아있었으면 정상 → 백오프 리셋

_CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


def _setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    logger = logging.getLogger("supervisor")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s: %(message)s")
    fh = RotatingFileHandler(
        LOG_DIR / "supervisor.log", maxBytes=2_000_000, backupCount=5,
        encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


log = _setup_logging()


def _cdp_alive():
    """CDP 원격 디버깅 포트가 응답하는지 확인."""
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=3) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def _find_chrome():
    for p in _CHROME_CANDIDATES:
        if Path(p).exists():
            return p
    return None


def ensure_chrome():
    """CDP 포트가 죽어 있으면 전용 프로필 Chrome 을 원격 디버깅으로 되살린다."""
    if _cdp_alive():
        return True
    chrome = _find_chrome()
    if not chrome:
        log.error("Chrome 실행파일을 찾지 못함 — attach 크롤링 불가")
        return False
    log.warning("CDP 포트(%s) 응답 없음 — Chrome 재실행", CDP_PORT)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [chrome, f"--remote-debugging-port={CDP_PORT}",
         f"--user-data-dir={PROFILE_DIR}",
         "https://self.baemin.com", "https://store.coupangeats.com"],
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
    )
    # 포트가 올라올 때까지 최대 ~30s 대기
    for _ in range(15):
        time.sleep(2)
        if _cdp_alive():
            log.info("Chrome CDP 포트 복구됨")
            return True
    log.warning("Chrome CDP 포트가 시간 내 올라오지 않음(로그인 필요할 수 있음)")
    return False


def run_forever(target):
    log.info("supervisor 시작 — 대상 `%s`, CDP 포트 %s", target, CDP_PORT)
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    backoff = BACKOFF_START
    child = None

    def _terminate(*_):
        log.info("종료 신호 수신 — 자식 프로세스 정리 후 종료")
        if child and child.poll() is None:
            child.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, _terminate)
    signal.signal(signal.SIGTERM, _terminate)

    while True:
        ensure_chrome()
        started = time.time()
        log.info("자식 프로세스 시작: %s -m %s", sys.executable, target)
        child = subprocess.Popen([sys.executable, "-m", target],
                                 cwd=str(PROJECT_ROOT), env=env)
        code = child.wait()
        ran = time.time() - started
        log.warning("자식 프로세스 종료 (exit=%s, 실행 %.0fs)", code, ran)

        if ran >= HEALTHY_RUNTIME:
            backoff = BACKOFF_START  # 정상 운영하다 죽음 → 즉시성 회복
        else:
            backoff = min(backoff * 2, BACKOFF_MAX)  # 빠른 반복 크래시 완화
        log.info("%ds 후 재시작…", backoff)
        time.sleep(backoff)


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TARGET
    try:
        run_forever(target)
    except SystemExit:
        raise
    except Exception:
        log.exception("supervisor 치명적 오류 — 종료")
        raise


if __name__ == "__main__":
    main()
