"""사장님 알림 — 텔레그램을 걷어낸 자리(2026-08-13, 사장님 지시).

알림 수단을 없앤다고 **정보까지 없애면 안 된다.** 그래서 알림은 두 곳에
남긴다:

  1) 로그 파일(logs/worker.log 등) — 집 PC에서 바로 확인.
  2) Supabase error_log — 직원 웹앱과 새벽 자동 점검이 읽는 곳. 사장님이
     화면에서 볼 수 있고, 처리하면 닫힌다(mark_error_fixed).

⚠️ DB 기록이 실패해도 예외를 올리지 않는다 — 알림 때문에 본 작업(답글 등록
   등)이 멈추면 본말전도다.
"""

import logging

logger = logging.getLogger(__name__)


def notify_owner(text, kind="Notice", source="worker", path=None):
    """사장님이 알아야 할 일을 로그와 화면(error_log)에 남긴다.

    Args:
        text: 사람이 읽을 한 줄 요약(원인과 할 일이 드러나게).
        kind: 분류 태그(예: ReplyReplaced, SessionExpired).
        source: 남긴 주체(worker/service/scheduler).
        path: 코드 위치(선택) — 새벽 점검이 원인 추적에 쓴다.
    """
    logger.warning("[알림] %s", text)
    try:
        from database import supabase_client as db
        db.log_error(source, text, kind=kind, path=path)
    except Exception:  # noqa: BLE001 — 기록 실패가 본 작업을 막지 않게
        logger.debug("알림 기록 실패(무시): %s", text[:80])


def session_expired(platform):
    """배민·쿠팡 로그인 세션 만료 — 사장님이 직접 재로그인해야 한다."""
    notify_owner(
        f"[{platform}] 로그인 세션이 만료됐습니다. 집 PC에서 "
        f"scripts/launch_chrome.bat 으로 띄운 Chrome 에서 다시 로그인해 주세요.",
        kind="SessionExpired", source="crawler", path=f"{platform}/login")
