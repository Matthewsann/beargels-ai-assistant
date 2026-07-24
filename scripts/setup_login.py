"""
최초 로그인 셋업 / 세션 상태 확인 스크립트 (Playwright)

배민·쿠팡에 로그인된 브라우저 세션을 준비/검증한다.

실행:
  # attach 모드(권장): 먼저 scripts/launch_chrome.bat 로 Chrome 을 띄우고,
  # 열린 탭에서 배민/쿠팡에 로그인한 뒤 실행
  python -m scripts.setup_login

  # profile 모드: 이 스크립트가 직접 Chrome 창을 띄운다.
  BROWSER_MODE=profile python -m scripts.setup_login

두 사이트 모두 로그인 상태가 되면 "준비 완료" 를 출력한다.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawler.browser import BrowserSession, get_mode, is_session_expired  # noqa: E402

SITES = {
    "배민": "https://self.baemin.com",
    "쿠팡이츠": "https://store.coupangeats.com/merchant/management/reviews",
}
POLL_TIMEOUT = 300
POLL_INTERVAL = 5


def main():
    mode = get_mode()
    print(f"[setup] BROWSER_MODE={mode}")
    if mode == "attach":
        print("[setup] launch_chrome.bat 로 띄운 Chrome 에 attach 합니다.")

    with BrowserSession(mode=mode, headless=False) as sess:
        page = sess.page
        print("\n두 사이트 로그인 상태를 확인합니다. 창에서 직접 로그인해 주세요.\n")
        deadline = time.time() + POLL_TIMEOUT
        pending = dict(SITES)
        while pending and time.time() < deadline:
            done = []
            for name, url in pending.items():
                page.goto(url, wait_until="domcontentloaded")
                time.sleep(2)
                if not is_session_expired(page):
                    print(f"  ✅ {name} 로그인 확인")
                    done.append(name)
                else:
                    print(f"  … {name} 로그인 대기 중")
            for name in done:
                pending.pop(name)
            if pending:
                time.sleep(POLL_INTERVAL)

        if pending:
            print(f"\n[미완료] 아직 로그인 안 된 사이트: {list(pending)}")
            sys.exit(1)
        print("\n🎉 준비 완료 — 모든 사이트 로그인 세션이 유효합니다.")


if __name__ == "__main__":
    main()
