"""구글 시트 '베어글스_장부' 요약시트 → ledger_monthly (집 PC 일꾼 전용).

사장님이 매달 정리하는 장부는 구글 시트다(드라이브 동기화 폴더에는 .gsheet
바로가기만 내려온다). 그래서 Drive API 로 시트를 CSV 로 내보내 읽는다 —
files.export 는 **첫 번째 시트**(= 요약)만 내보내므로 요약시트가 맨 앞에
있어야 한다(지금 그렇다).

인증: 릴스 파이프라인과 같은 OAuth 토큰(token.json, scope drive). 이 PC 에
token.json 이 없으면 `3_google_login.bat` 를 한 번 실행해 로그인한다.

일꾼의 하루 1회 매출 반영(maybe_pos_import)과 웹 [장부 지금 반영] 버튼에서
sync() 를 부른다. 실패해도 포스 장부 반영은 막지 않는다.
"""

from __future__ import annotations

import logging
import os
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database import ledger_store  # noqa: E402

logger = logging.getLogger(__name__)

# 시트 ID — 드라이브에서 '베어글스_장부' (사장님 계정). 바뀌면 .env 로.
DEFAULT_SHEET_ID = "1kPh9GkWP4g9qFLDlPKs0LbASx3Aj3dHkq9i8g2EHRe4"


def sheet_id() -> str:
    return os.getenv("LEDGER_SHEET_ID", DEFAULT_SHEET_ID)


def _token_file() -> str:
    p = os.getenv("GOOGLE_OAUTH_TOKEN_FILE", "token.json")
    return p if os.path.isabs(p) else str(ROOT / p)


def fetch_summary_csv():
    """(csv_text, modified_at: datetime|None). Drive API 두 번 왕복."""
    from googleapiclient.discovery import build
    from sns_automation.drive_monitor import load_oauth_credentials

    creds = load_oauth_credentials(_token_file())
    svc = build("drive", "v3", credentials=creds, cache_discovery=False)
    fid = sheet_id()
    meta = svc.files().get(fileId=fid, fields="modifiedTime,name").execute()
    raw = svc.files().export(fileId=fid, mimeType="text/csv").execute()
    text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else str(raw)
    mod = None
    if meta.get("modifiedTime"):
        mod = datetime.fromisoformat(meta["modifiedTime"].replace("Z", "+00:00"))
    return text, mod


def sync(text=None, modified_at=None) -> dict:
    """시트 → ledger_monthly + 목표(menu_settings). 요약 dict 반환.

    text 를 주면(테스트·수동 시드) Drive 를 안 부른다.
    """
    if text is None:
        text, modified_at = fetch_summary_csv()
    rows, targets = ledger_store.parse_summary_csv(text, modified_at)
    n = ledger_store.upsert_ledger(rows)
    if targets:
        ledger_store.set_ledger_targets(targets)
    est = [r["ym"] for r in rows if r.get("status") == "estimate"]
    note = (f"{n}개월 반영" + (f" (예상치 {', '.join(est)})" if est else "")
            + (f", 시트 수정 {modified_at.astimezone(timezone.utc).date()}" if modified_at else ""))
    logger.info("장부 시트 반영: %s", note)
    return {"ok": True, "months": n, "estimate": est, "targets": bool(targets),
            "note": note}
