"""베어글스 인스타그램 SNS 자동화 파이프라인 진입점.

구글 드라이브 폴더 모니터링 → Claude 캡션 생성 → 텔레그램 승인 → Buffer 발행
"""

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from sns_automation.buffer_client import BufferClient
from sns_automation.caption_generator import CaptionGenerator
from sns_automation.config import load_settings
from sns_automation.db import PostRepository
from sns_automation.drive_monitor import DriveClient
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
    buffer = BufferClient(settings.buffer_access_token, settings.instagram_profile_id)

    app = build_application(settings.telegram_bot_token, settings.telegram_chat_id)
    pipeline = Pipeline(
        drive=drive,
        repo=repo,
        generator=generator,
        buffer=buffer,
        bot=app.bot,
        chat_id=settings.telegram_chat_id,
    )
    app.bot_data["pipeline"] = pipeline

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        pipeline.poll_drive,
        "interval",
        minutes=settings.poll_interval_minutes,
        max_instances=1,
    )

    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        scheduler.start()
        logger.info(
            "파이프라인 시작 — 드라이브 폴링 주기: %d분", settings.poll_interval_minutes
        )

        # 시작 직후 한 번 즉시 폴링
        await pipeline.poll_drive()

        try:
            await asyncio.Event().wait()  # 종료 시그널까지 대기
        finally:
            scheduler.shutdown(wait=False)
            await app.updater.stop()
            await app.stop()


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        logger.info("종료합니다.")


if __name__ == "__main__":
    main()
