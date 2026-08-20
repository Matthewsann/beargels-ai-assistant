"""
브라우저 세션 관리 (Playwright) — 배민·쿠팡 크롤러 공용 계층

배민/쿠팡 사장님 사이트는 **로그인 플로우에서 봇 탐지가 가장 강하다**. 매 실행마다
로그인 폼을 거치는 대신 **저장된 세션을 재사용**해 로그인 폼 자체를 건너뛴다.

두 모드(.env BROWSER_MODE):

  attach  (권장)
      사용자가 scripts/launch_chrome.bat 로 --remote-debugging-port 를 열어
      Chrome 을 띄우고 배민·쿠팡에 로그인해 둔다. 크롤러는 그 실제 브라우저에
      Playwright 의 connect_over_cdp 로 붙어 새 탭(page)에서 작업한다. 사람이
      켠 진짜 창이라 탐지 위험이 가장 낮다.

  profile
      크롤러가 launch_persistent_context 로 .browser_profile/ 를 써서 시스템
      Chrome(channel="chrome")을 직접 띄운다. 무인 서버용. 최초 1회
      scripts/setup_login.py 로 수동 로그인해 세션을 저장한다.

⚠️ 그냥 켜둔 Chrome 에는 attach 불가 — 반드시 launch_chrome.bat 경유.
   그 프로필은 평소 쓰는 Chrome 과 분리된 전용 프로필이다.

사용:
    with BrowserSession() as sess:
        sess.page.goto(...)
        html = sess.page.content()
"""

import logging
import os
import random
import time
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE_DIR = PROJECT_ROOT / ".browser_profile"

# 크롤러가 **실수로 연 탭**의 주소 — 우리는 이런 페이지를 열 일이 없다.
# 배민 리뷰 목록의 '더보기'를 누르다 도움말 쪽 버튼이 걸리면 ceo.baemin.com/qna
# 가 **새 탭**으로 뜬다. 같은 탭 이동이 아니라 기존 URL 가드에 안 걸려 조용히
# 쌓였고, 사장님 Chrome 에 탭이 76개까지 열렸다(2026-08-21).
STRAY_TAB_URLS = ("ceo.baemin.com/qna", "ceo.baemin.com/faq",
                  "ceo.baemin.com/help")

_stray_closed = 0


def stray_tabs_closed():
    """지금까지 자동으로 닫은 '잘못 열린 탭' 수 — 크롤러가 오클릭 감지에 쓴다."""
    return _stray_closed


def is_stray_url(url):
    return any(p in (url or "") for p in STRAY_TAB_URLS)


def close_stray_tabs(context, log=True):
    """열려 있는 잘못된 탭을 정리한다(닫은 수 반환).

    ⚠️ **우리가 낸 쓰레기 탭만** 닫는다(STRAY_TAB_URLS). 사장님이 직접 열어둔
       탭을 건드리면 안 되므로 주소를 정확히 맞춰 보고, 마지막 한 탭은 남긴다
       (탭이 0개가 되면 Chrome 창이 닫힌다).
    """
    global _stray_closed
    n = 0
    try:
        pages = list(context.pages)
    except Exception:  # noqa: BLE001
        return 0
    for pg in pages:
        if len(context.pages) - n <= 1:
            break
        try:
            if is_stray_url(pg.url):
                pg.close()
                n += 1
        except Exception:  # noqa: BLE001 — 이미 닫힌 탭 등
            continue
    _stray_closed += n
    if n and log:
        logger.warning("잘못 열린 탭 %d개를 닫았습니다(도움말 페이지).", n)
    return n


class SessionExpiredError(RuntimeError):
    """저장된 로그인 세션이 만료되어 수동 재로그인이 필요할 때 발생."""


# ---------------------------------------------------------------------------
# 설정 헬퍼
# ---------------------------------------------------------------------------

def get_mode():
    return os.getenv("BROWSER_MODE", "attach").strip().lower()


def get_cdp_port():
    return int(os.getenv("CDP_PORT", "9222"))


def get_profile_dir():
    p = os.getenv("BROWSER_PROFILE_DIR", "").strip()
    return Path(p) if p else DEFAULT_PROFILE_DIR


# ---------------------------------------------------------------------------
# 세션 (컨텍스트 매니저)
# ---------------------------------------------------------------------------

class BrowserSession:
    """모드에 맞는 Playwright 페이지를 준비/정리하는 컨텍스트 매니저.

    - attach : 실행 중인 Chrome 에 붙어 새 page 를 연다. 종료 시 그 page 만
               닫고 CDP 연결만 끊는다(사용자 브라우저는 유지).
    - profile: 전용 프로필로 Chrome 을 직접 실행. 종료 시 컨텍스트를 닫는다.
    """

    def __init__(self, mode=None, headless=False):
        self.mode = (mode or get_mode()).lower()
        self.headless = headless
        self._pw = None
        self._browser = None
        self._context = None
        self.page = None

    def __enter__(self):
        self._pw = sync_playwright().start()
        if self.mode == "attach":
            port = get_cdp_port()
            try:
                # 기본 3분은 너무 길다 — Chrome 이 먹통이면 수집 한 번이
                # 6분씩 헛돌았다(2026-08-21). 빨리 실패하고 복구에 넘긴다.
                self._browser = self._pw.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{port}",
                    timeout=float(os.getenv("CDP_CONNECT_TIMEOUT_MS", "45000")))
            except Exception as e:  # noqa: BLE001
                self._pw.stop()
                raise RuntimeError(
                    f"CDP attach 실패(127.0.0.1:{port}). "
                    f"scripts/launch_chrome.bat 로 Chrome 을 먼저 실행했는지 "
                    f"확인하세요. 원인: {e}") from e
            # 기존 컨텍스트(사용자 세션) 재사용
            self._context = (self._browser.contexts[0]
                             if self._browser.contexts
                             else self._browser.new_context())
            self.page = self._context.new_page()
            logger.info("기존 Chrome 에 attach 완료 (port %s)", port)
        elif self.mode == "profile":
            profile_dir = get_profile_dir()
            profile_dir.mkdir(parents=True, exist_ok=True)
            self._context = self._pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                channel="chrome",           # 시스템 Chrome 사용(다운로드 불필요)
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled",
                      "--lang=ko-KR"],
            )
            self.page = (self._context.pages[0] if self._context.pages
                         else self._context.new_page())
            logger.info("profile 모드로 Chrome 실행 (%s)", profile_dir)
        else:
            self._pw.stop()
            raise ValueError(f"알 수 없는 BROWSER_MODE: {self.mode!r} (attach|profile)")
        self.page.set_default_timeout(15000)
        # 크롤러는 새 탭을 열 일이 없다 — 열리면 즉시 닫는다. 배민 '더보기'
        # 오클릭이 새 탭으로 떠서 URL 가드를 피해 갔다(2026-08-21).
        self.page.on("popup", self._close_popup)
        try:
            self._context.on("page", self._close_popup)   # 팝업 외 경로 대비
        except Exception:  # noqa: BLE001
            pass
        close_stray_tabs(self._context)      # 지난 실행이 남긴 탭도 치운다
        return self

    @staticmethod
    def _close_popup(popup):
        """새로 열린 탭 처리 — 도움말 등 '우리 것이 아닌' 탭이면 닫는다."""
        global _stray_closed
        try:
            url = popup.url or ""
            if url in ("", "about:blank"):        # 주소가 아직이면 잠깐 기다린다
                try:
                    popup.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:  # noqa: BLE001
                    pass
                url = popup.url or ""
            if is_stray_url(url):
                popup.close()
                _stray_closed += 1
                logger.warning("잘못 열린 탭을 닫았습니다: %s", url[:80])
        except Exception:  # noqa: BLE001 — 탭 정리가 크롤링을 막으면 안 된다
            logger.debug("팝업 정리 실패(무시)", exc_info=True)

    def __exit__(self, *exc):
        try:
            if self._context:
                close_stray_tabs(self._context)   # 남은 쓰레기 탭 정리
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.mode == "attach":
                if self.page:
                    self.page.close()          # 우리가 연 탭만 닫음
                if self._browser:
                    self._browser.close()      # CDP 연결 해제(브라우저 유지)
            else:
                if self._context:
                    self._context.close()      # 우리가 띄운 브라우저 종료
        finally:
            if self._pw:
                self._pw.stop()


# ---------------------------------------------------------------------------
# 세션 상태 / 페이싱
# ---------------------------------------------------------------------------

def is_session_expired(page):
    """현재 페이지가 로그아웃(세션 만료) 상태인지 판별한다.

    로그아웃 시 로그인 폼(비밀번호 입력칸)이 노출되거나 URL 에 login 이
    포함되므로 이를 신호로 사용한다.
    """
    if "login" in (page.url or "").lower():
        return True
    return page.query_selector(
        'input[type="password"], input[name="password"]') is not None


def human_pause(min_s=1.5, max_s=4.0):
    """사람 같은 랜덤 지연 — 서버 부하·탐지 완화."""
    time.sleep(random.uniform(min_s, max_s))
