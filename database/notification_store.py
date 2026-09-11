"""notifications 표 — 사람용 알림 저장소 (Notification Layer Phase 2, 2026-09-11).

한 줄 = 문제 하나. 같은 문제(dedupe_key)가 다시 감지되면 새 줄을 만들지 않고
occurrences 와 last_seen_at 만 올린다. 그래서 알림함에 "9일째 · 9회"로 보이지,
날짜별로 아홉 줄이 쌓이지 않는다(error_log 시절의 문제).

⚠️ 이 표는 database/schema_v13.sql 로 만들어지며 **사장님이 실행하기 전엔 없다.**
    없으면 available() 이 False 를 돌려주고, 부르는 쪽(alerts.notify)은 예전 길
    (error_log Notice)로 떨어진다 — 기능이 죽지 않는다(프로젝트 관례:
    supabase_client._OFFERS_MISSING 과 같은 방식).

이 모듈은 저장·조회·상태 변경만 한다. 무엇을 알릴지·누구에게·언제 다시 알릴지
(정책)는 alerts.py 몫이고, 발송(Dispatcher)·자동 해소(resolve hook)는 아직 없다.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from database.supabase_client import get_client

logger = logging.getLogger(__name__)

TABLE = "notifications"

#: 이 상태들이 "아직 열려 있다" — 부분 유니크 인덱스(schema_v13)와 같은 집합.
OPEN_STATES = ("open", "sending", "sent", "read")
CLOSED_STATES = ("resolved", "muted", "expired")

_MISSING = ("42P01", "PGRST205", "PGRST200")   # 표 없음(마이그레이션 전)
_UNIQUE = ("23505",)                           # 부분 유니크 인덱스 충돌

# 표 유무는 자주 묻지 않는다 — 없으면 10분, 잠깐 실패면 1분 뒤 다시 본다.
_avail = {"ok": None, "until": 0.0}
_RECHECK_MISSING_SEC = 600
_RECHECK_ERROR_SEC = 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reset_cache() -> None:
    """테스트용 — 표 유무 기억을 지운다."""
    _avail["ok"], _avail["until"] = None, 0.0


def available() -> bool:
    """notifications 표가 있어서 쓸 수 있는가. 없으면 False(예외를 올리지 않는다)."""
    if _avail["ok"] is not None and time.monotonic() < _avail["until"]:
        return _avail["ok"]
    try:
        get_client().table(TABLE).select("id").limit(1).execute()
        _avail["ok"], _avail["until"] = True, float("inf")
    except Exception as e:  # noqa: BLE001
        missing = getattr(e, "code", None) in _MISSING
        _avail["ok"] = False
        _avail["until"] = time.monotonic() + (_RECHECK_MISSING_SEC if missing
                                              else _RECHECK_ERROR_SEC)
        if missing:
            logger.debug("notifications 표 없음 — 마이그레이션 전(schema_v13.sql)")
        else:
            logger.warning("notifications 확인 실패(잠시 예전 길로): %s", str(e)[:120])
    return _avail["ok"]


def _rows_for(dedupe_key: str) -> list[dict]:
    return (get_client().table(TABLE).select("*").eq("dedupe_key", dedupe_key)
            .order("created_at", desc=True).limit(5).execute().data) or []


def _update(row_id: int, patch: dict) -> dict | None:
    patch = dict(patch, updated_at=_now_iso())
    data = (get_client().table(TABLE).update(patch).eq("id", row_id)
            .execute().data) or []
    return data[0] if data else None


def record(event_type: str, dedupe_key: str, title: str, message: str = "",
           severity: str = "normal", recipient_type: str = "role",
           recipient_id: str = "owner", source: str = "worker",
           source_ref: str | None = None, link: str | None = None) -> tuple[dict | None, str]:
    """문제 하나를 적는다. 반환: (행, 결과) — 결과는 created / updated / kept_closed.

    · 같은 키가 **열려** 있으면 → 그 줄의 occurrences+1, last_seen_at, 문구 갱신 (updated)
    · 같은 키가 **닫혀만** 있으면(resolved/muted/expired) → 되살리지 않는다.
      닫힌 줄의 occurrences·last_seen_at 만 올려 "또 보였다"는 기록을 남긴다 (kept_closed).
      왜: 지금은 자동 해소(공유 완료 → resolved)가 아직 없어서, 사장님이 [처리됨]을
      눌렀는데 /review 의 [공유 완료]를 안 눌렀으면 다음 날 같은 요청이 또 잡힌다.
      그때 새 줄을 만들면 error_log 시절의 '매일 반복'이 그대로 돌아온다.
      되살리는 규칙은 Phase 3(자동 해소)과 함께 정한다.
    · 아무 줄도 없으면 → 새 줄 (created)
    """
    now = _now_iso()
    rows = _rows_for(dedupe_key)
    open_row = next((r for r in rows if r.get("status") in OPEN_STATES), None)
    if open_row:
        row = _update(open_row["id"], {
            "occurrences": int(open_row.get("occurrences") or 1) + 1,
            "last_seen_at": now, "title": title, "message": message})
        return row or open_row, "updated"
    if rows:                                   # 닫힌 줄만 있다
        latest = rows[0]
        row = _update(latest["id"], {
            "occurrences": int(latest.get("occurrences") or 1) + 1,
            "last_seen_at": now})
        return row or latest, "kept_closed"
    payload = {
        "event_type": event_type, "dedupe_key": dedupe_key,
        "severity": severity, "title": title, "message": message,
        "link": link, "recipient_type": recipient_type, "recipient_id": recipient_id,
        "status": "open", "occurrences": 1, "last_seen_at": now,
        "source": source, "source_ref": source_ref,
    }
    try:
        data = get_client().table(TABLE).insert(payload).execute().data or []
        return (data[0] if data else payload), "created"
    except Exception as e:  # noqa: BLE001
        if getattr(e, "code", None) not in _UNIQUE:
            raise
        # 같은 순간 다른 프로세스가 먼저 넣었다 — 그 줄을 갱신한다
        rows = _rows_for(dedupe_key)
        open_row = next((r for r in rows if r.get("status") in OPEN_STATES), None)
        if not open_row:
            raise
        row = _update(open_row["id"], {
            "occurrences": int(open_row.get("occurrences") or 1) + 1,
            "last_seen_at": now})
        return row or open_row, "updated"


def inbox(limit: int = 5, recipient_id: str | None = None) -> list[dict]:
    """알림함이 읽는다 — 아직 열린 것(읽은 것 포함)을 최신순으로."""
    q = (get_client().table(TABLE).select("*")
         .in_("status", list(OPEN_STATES)))
    if recipient_id:
        q = q.eq("recipient_id", recipient_id)
    return (q.order("created_at", desc=True).limit(limit).execute().data) or []


def mark_read(row_id: int) -> dict | None:
    """[확인] — 읽었다. 문제는 아직 열려 있다."""
    return _update(row_id, {"status": "read", "read_at": _now_iso()})


def mark_resolved(row_id: int, by: str = "manual", reason: str = "manual") -> dict | None:
    """[처리됨] — 해결. 이후 같은 키가 또 보여도 되살리지 않는다(record 참고)."""
    return _update(row_id, {"status": "resolved", "resolved_at": _now_iso(),
                            "resolved_by": by, "resolve_reason": reason})


def resolve_by_key(dedupe_key: str, by: str = "auto", reason: str = "auto") -> dict | None:
    """문제가 실제로 해소됐을 때 — 열린 줄이 있으면 resolved 로. 없으면 None.

    Phase 3-A(2026-09-11): 고객 요청 [공유 완료]가 부른다. 새 줄을 만들지 않고,
    이미 닫힌 줄은 건드리지 않는다. 표가 없거나 조회가 실패해도 예외를 올리지
    않는다 — 해소 기록 실패가 본 작업(공유 완료 표시)을 막으면 안 된다.
    """
    try:
        if not available():
            return None
        open_row = next((r for r in _rows_for(dedupe_key)
                         if r.get("status") in OPEN_STATES), None)
        if not open_row:
            return None
        return mark_resolved(open_row["id"], by=by, reason=reason)
    except Exception as e:  # noqa: BLE001
        logger.warning("알림 자동 해소 실패(%s): %s", dedupe_key, str(e)[:120])
        return None
