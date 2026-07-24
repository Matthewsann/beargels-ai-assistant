"""파이프라인 오케스트레이션.

드라이브 폴링 → 캡션 생성 → 텔레그램 승인 요청 → (승인 시) Buffer로 인스타그램 발행
"""

import asyncio
import logging

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from . import db
from .buffer_client import BufferClient
from .caption_generator import CaptionGenerator, CaptionResult
from .db import PostRepository
from .drive_monitor import DriveClient
from .media_host import MediaHost

logger = logging.getLogger(__name__)


def _approval_keyboard(post_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ 승인", callback_data=f"approve:{post_id}"),
                InlineKeyboardButton("✏️ 수정", callback_data=f"edit:{post_id}"),
                InlineKeyboardButton("❌ 취소", callback_data=f"cancel:{post_id}"),
            ]
        ]
    )


def _preview_text(file_name: str, result: CaptionResult) -> str:
    return (
        f"📸 새 게시물 승인 요청\n"
        f"파일: {file_name}\n"
        f"메뉴: {result.menu}\n"
        f"{'─' * 20}\n\n"
        f"{result.caption}\n\n"
        f"{' '.join(result.hashtags)}"
    )


class Pipeline:
    def __init__(
        self,
        drive: DriveClient,
        repo: PostRepository,
        generator: CaptionGenerator,
        buffer: BufferClient,
        media_host: MediaHost,
        bot: Bot,
        chat_id: str,
    ):
        self.drive = drive
        self.repo = repo
        self.generator = generator
        self.buffer = buffer
        self.media_host = media_host
        self.bot = bot
        self.chat_id = chat_id
        self._poll_lock = asyncio.Lock()

    # ── 1단계: 드라이브 폴링 ──────────────────────────────

    async def poll_drive(self) -> None:
        """APScheduler가 주기적으로 호출. 새 파일을 감지해 처리한다."""
        if self._poll_lock.locked():
            logger.info("이전 폴링이 아직 진행 중 — 이번 회차 건너뜀")
            return
        async with self._poll_lock:
            try:
                files = await asyncio.to_thread(self.drive.list_media_files)
            except Exception:
                logger.exception("드라이브 파일 목록 조회 실패")
                return

            for file in files:
                try:
                    processed = await asyncio.to_thread(self.repo.is_processed, file["id"])
                    if processed:
                        continue
                    logger.info("새 파일 감지: %s (%s)", file["name"], file["mimeType"])
                    await self._process_new_file(file)
                except Exception:
                    logger.exception("파일 처리 실패: %s", file.get("name"))

    async def _process_new_file(self, file: dict) -> None:
        post = await asyncio.to_thread(
            self.repo.create_post, file["id"], file["name"], file["mimeType"]
        )
        try:
            result, image_bytes = await self._generate_for_file(file)
            await self._send_approval_request(post["id"], file["name"], result, image_bytes)
            await asyncio.to_thread(
                self.repo.update_post,
                post["id"],
                status=db.STATUS_AWAITING_APPROVAL,
                menu=result.menu,
                caption=result.caption,
                hashtags=result.hashtags,
            )
        except Exception as e:
            await asyncio.to_thread(
                self.repo.update_post, post["id"], status=db.STATUS_FAILED, error=str(e)
            )
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=f"⚠️ 파일 처리 실패: {file['name']}\n{e}",
            )
            raise

    # ── 2단계: 캡션 생성 ─────────────────────────────────

    async def _generate_for_file(
        self,
        file: dict,
        previous_caption: str | None = None,
        feedback: str | None = None,
    ) -> tuple[CaptionResult, bytes | None]:
        is_video = file["mimeType"].startswith("video/")
        if is_video:
            image_bytes = await asyncio.to_thread(self.drive.download_thumbnail, file)
        else:
            image_bytes = await asyncio.to_thread(self.drive.download_file, file["id"])

        result = await self.generator.generate(
            image_bytes=image_bytes,
            file_name=file["name"],
            is_video=is_video,
            previous_caption=previous_caption,
            feedback=feedback,
        )
        return result, image_bytes

    # ── 3단계: 텔레그램 승인 요청 ─────────────────────────

    async def _send_approval_request(
        self,
        post_id: str,
        file_name: str,
        result: CaptionResult,
        image_bytes: bytes | None,
    ) -> None:
        text = _preview_text(file_name, result)
        keyboard = _approval_keyboard(post_id)
        if image_bytes:
            # 텔레그램 사진 캡션은 1024자 제한 → 넘으면 텍스트로 분리 전송
            if len(text) <= 1024:
                await self.bot.send_photo(
                    chat_id=self.chat_id, photo=image_bytes, caption=text, reply_markup=keyboard
                )
            else:
                await self.bot.send_photo(chat_id=self.chat_id, photo=image_bytes)
                await self.bot.send_message(chat_id=self.chat_id, text=text, reply_markup=keyboard)
        else:
            await self.bot.send_message(chat_id=self.chat_id, text=text, reply_markup=keyboard)

    # ── 4단계: 승인/수정/취소 처리 (telegram_bot.py에서 호출) ──

    async def approve(self, post_id: str) -> str:
        """승인 → 인스타그램 발행. 결과 메시지를 반환."""
        post = await asyncio.to_thread(self.repo.get_post, post_id)
        if not post:
            return "게시물을 찾을 수 없습니다."
        if post["status"] == db.STATUS_PUBLISHED:
            return "이미 발행된 게시물입니다."

        full_text = f"{post['caption']}\n\n{' '.join(post['hashtags'] or [])}"
        is_video = (post.get("mime_type") or "").startswith("video/")

        try:
            if is_video:
                # 영상: 드라이브 파일을 링크 공개로 전환해 Buffer에 전달
                video_url = await asyncio.to_thread(
                    self.drive.make_public, post["drive_file_id"]
                )
                bp = await self.buffer.create_post(full_text, video_url=video_url)
            else:
                # 사진: JPEG 변환 후 Supabase 공개 버킷에 잠깐 올려서 전달
                image_bytes = await asyncio.to_thread(
                    self.drive.download_file, post["drive_file_id"]
                )
                image_url = await asyncio.to_thread(
                    self.media_host.upload_image, post_id, image_bytes
                )
                bp = await self.buffer.create_post(full_text, image_url=image_url)
        except Exception as e:
            logger.exception("Buffer 발행 실패: post=%s", post_id)
            await asyncio.to_thread(
                self.repo.update_post, post_id, status=db.STATUS_FAILED, error=str(e)
            )
            return f"⚠️ 발행 실패: {e}"

        await asyncio.to_thread(
            self.repo.update_post,
            post_id,
            status=db.STATUS_PUBLISHED,
            buffer_update_id=bp["id"],
        )
        return f"✅ 승인 완료! Buffer 큐에 등록됐습니다.\n({post['file_name']})"

    async def request_edit(self, post_id: str) -> str:
        await asyncio.to_thread(
            self.repo.update_post, post_id, status=db.STATUS_AWAITING_FEEDBACK
        )
        return "✏️ 수정할 내용을 답장으로 보내주세요.\n(예: \"더 짧게\", \"소금빵 베이글이라고 해줘\")"

    async def regenerate(self, post_id: str, feedback: str) -> None:
        """수정 피드백 반영 재생성 후 승인 요청 재전송."""
        post = await asyncio.to_thread(self.repo.get_post, post_id)
        if not post:
            await self.bot.send_message(chat_id=self.chat_id, text="게시물을 찾을 수 없습니다.")
            return

        try:
            # 영상 썸네일 링크 등 최신 메타데이터를 다시 가져온다
            file = await asyncio.to_thread(self.drive.get_file, post["drive_file_id"])
        except Exception:
            file = {
                "id": post["drive_file_id"],
                "name": post["file_name"],
                "mimeType": post.get("mime_type") or "image/jpeg",
            }
        try:
            result, image_bytes = await self._generate_for_file(
                file, previous_caption=post["caption"], feedback=feedback
            )
        except Exception as e:
            logger.exception("캡션 재생성 실패: post=%s", post_id)
            await self.bot.send_message(chat_id=self.chat_id, text=f"⚠️ 재생성 실패: {e}")
            return

        await asyncio.to_thread(
            self.repo.update_post,
            post_id,
            status=db.STATUS_AWAITING_APPROVAL,
            menu=result.menu,
            caption=result.caption,
            hashtags=result.hashtags,
            feedback=feedback,
        )
        await self._send_approval_request(post_id, post["file_name"], result, image_bytes)

    async def cancel(self, post_id: str) -> str:
        await asyncio.to_thread(self.repo.update_post, post_id, status=db.STATUS_CANCELLED)
        return "❌ 취소했습니다. 이 파일은 발행되지 않습니다."
