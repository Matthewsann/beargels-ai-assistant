"""
텔레그램 알림 전송 (경량 유틸)

세션 만료 알림·에러 알림 등 "봇 명령 처리"와 무관한 단발성 알림용.
정식 봇(bot/telegram_bot.py)이 완성되기 전에도 동작하도록 requests 로
직접 sendMessage 를 호출한다.

TELEGRAM_CHAT_ID 가 비어 있으면 실제 전송 대신 로그만 남긴다(개발 편의).
"""

import logging
import os

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
_API = "https://api.telegram.org/bot{token}/sendMessage"


def send_message(text, chat_id=None):
    """텔레그램으로 텍스트 메시지를 보낸다. 성공 여부(bool)를 반환한다.

    토큰/챗ID 가 없으면 전송하지 않고 경고 로그만 남긴 뒤 False 반환.
    """
    chat_id = chat_id or _CHAT_ID
    if not _TOKEN or not chat_id:
        logger.warning(
            "텔레그램 미설정(TELEGRAM_BOT_TOKEN/CHAT_ID) — 알림 생략: %s",
            text.replace("\n", " ")[:120],
        )
        return False
    try:
        resp = requests.post(
            _API.format(token=_TOKEN),
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException:
        logger.exception("텔레그램 전송 실패")
        return False


def send_manual_login_alert(platform):
    """세션 만료 시 수동 재로그인 요청 알림을 보낸다."""
    return send_message(
        f"⚠️ [{platform}] 세션이 만료되었습니다.\n"
        f"scripts/launch_chrome.bat 로 띄운 Chrome 에서 다시 로그인해 주세요."
    )
