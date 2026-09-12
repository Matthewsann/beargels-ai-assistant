"""사장님 알림 — 텔레그램을 걷어낸 자리(2026-08-13, 사장님 지시).

알림 수단을 없앤다고 **정보까지 없애면 안 된다.** 그래서 알림은 두 곳에
남긴다:

  1) 로그 파일(logs/worker.log 등) — 집 PC에서 바로 확인.
  2) Supabase error_log — 직원 웹앱과 새벽 자동 점검이 읽는 곳. 사장님이
     화면에서 볼 수 있고, 처리하면 닫힌다(mark_error_fixed).

⚠️ DB 기록이 실패해도 예외를 올리지 않는다 — 알림 때문에 본 작업(답글 등록
   등)이 멈추면 본말전도다.

Phase 2 (2026-09-11) — notify():
  사람용 알림을 notifications 표(database/schema_v13.sql)에 '문제 하나 = 한 줄'로
  적는다. 발생원은 이 함수만 부르고 표의 생김새는 모른다. 표가 아직 없으면
  (사장님이 SQL 을 실행하기 전) 예전 길 notify_owner → error_log 로 떨어진다.
  지금은 고객 요청 미전파(request.unshared) 한 종류만 이 길을 쓴다.
"""

import logging

logger = logging.getLogger(__name__)

#: 이벤트별 기본값 — 부르는 쪽이 안 주면 여기서 채운다. 아직 1종.
#: request.unshared 가 high 인 이유: 3일 넘게 묵은 고객 요청은 '당일 처리'
#: 감이지 즉시 대표를 깨울 일(critical: 세션 만료·민감 리뷰)은 아니다
#: (설계 보고서 ⑧). 수신자는 people 표가 생기기 전이라 역할 'owner' 고정.
_POLICY = {
    "request.unshared": {"severity": "high", "recipient_type": "role",
                         "recipient_id": "owner", "link": "/review"},
    # 기한이 **지난** 업무 하나 = 알림 한 줄 (Phase 3-B-2, 2026-09-12). 오늘·내일
    # 기한(rank 1·2)은 예전 묶음 잔소리에 그대로 두고, 여기엔 rank 0 만 온다 —
    # 알림 수가 불어나지 않게. 키는 보드가 쓰는 id 그대로 w:<id> / m:<id>.
    "work.overdue": {"severity": "high", "recipient_type": "role",
                     "recipient_id": "owner", "link": "/work"},
}


def notifications_ready() -> bool:
    """notifications 표가 있어서 notify() 가 그 길로 갈 수 있는가."""
    try:
        from database import notification_store as ns
        return bool(ns.available())
    except Exception:  # noqa: BLE001 — 확인 자체가 실패해도 예전 길이 있다
        return False


def notify(event_type, dedupe_key, title, message="", severity=None,
           recipient_type=None, recipient_id=None, source="worker",
           source_ref=None, link=None):
    """문제 하나를 알린다. 같은 dedupe_key 가 열려 있으면 새 줄 대신 횟수만 는다.

    반환: (행, 결과) 또는 폴백으로 갔으면 None. 절대 예외를 올리지 않는다.
    """
    pol = _POLICY.get(event_type, {})
    severity = severity or pol.get("severity", "normal")
    recipient_type = recipient_type or pol.get("recipient_type", "role")
    recipient_id = recipient_id or pol.get("recipient_id", "owner")
    link = link if link is not None else pol.get("link")
    logger.warning("[알림] %s — %s", title, message)
    try:
        from database import notification_store as ns
        if ns.available():
            return ns.record(event_type, dedupe_key, title, message,
                             severity=severity, recipient_type=recipient_type,
                             recipient_id=recipient_id, source=source,
                             source_ref=source_ref, link=link)
    except Exception as e:  # noqa: BLE001 — 저장 실패가 본 작업을 막지 않게
        logger.warning("notifications 저장 실패(예전 길로): %s", str(e)[:120])
    # 폴백 — 표가 없거나 못 썼다. 예전처럼 error_log Notice 로 남겨 알림함에 뜨게.
    notify_owner(f"{title} — {message}" if message else title,
                 kind="Notice", source=source, path=event_type)
    return None


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
