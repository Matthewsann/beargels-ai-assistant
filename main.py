"""베어글스 송도 인스타그램 SNS 봇 진입점 (폴더 기반 on-demand).

텔레그램에서 주제 폴더명 호출 → 그 폴더 분석 → Claude 캡션 생성
→ 승인 → Buffer로 인스타그램 발행(릴스/게시물).
"""

import asyncio
import logging

from sns_automation.buffer_client import BufferClient
from sns_automation.caption_generator import CaptionGenerator
from sns_automation.config import load_settings
from sns_automation.db import PostRepository
from sns_automation.drive_monitor import DriveClient
from sns_automation.media_host import MediaHost
from sns_automation.pipeline import Pipeline
from sns_automation.telegram_bot import build_application

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def run() -> None:
    settings = load_settings()

    drive = DriveClient(settings.google_oauth_token_file, settings.google_drive_folder_id)
    repo = PostRepository(settings.supabase_url, settings.supabase_key)
    generator = CaptionGenerator(settings.anthropic_api_key)
    buffer = BufferClient(settings.buffer_access_token, settings.buffer_channel_id)
    media_host = MediaHost(repo.client)

    app = build_application(settings.telegram_bot_token, settings.telegram_chat_id)
    pipeline = Pipeline(
        drive=drive,
        repo=repo,
        generator=generator,
        buffer=buffer,
        media_host=media_host,
        bot=app.bot,
        chat_id=settings.telegram_chat_id,
    )
    app.bot_data["pipeline"] = pipeline

    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        logger.info("봇 시작 — 텔레그램에서 주제명을 보내면 작업을 시작합니다. (/목록 으로 폴더 확인)")

        try:
            await asyncio.Event().wait()  # 종료 시그널까지 대기
        finally:
            await app.updater.stop()
            await app.stop()


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        logger.info("종료합니다.")


if __name__ == "__main__":
    main()
