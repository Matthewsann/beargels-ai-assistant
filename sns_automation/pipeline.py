"""파이프라인 오케스트레이션 (폴더 기반 on-demand).

텔레그램에서 주제 폴더명 호출 → 그 폴더 분석 → 캡션 생성
→ (릴스면) 자동 편집 → 승인 요청(편집된 릴스 미리보기)
→ (승인) Supabase 임시 호스팅 → Buffer로 인스타그램 발행(릴스/게시물).
"""

import asyncio
import logging
import os
import tempfile

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from . import db, video_editor
from .buffer_client import BufferClient
from .caption_generator import CaptionGenerator, CaptionResult
from .db import PostRepository
from .drive_monitor import DriveClient
from .media_host import MediaHost
from .templates import Template, get_template

logger = logging.getLogger(__name__)

# 분석에 사용할 대표 이미지 최대 장수 (사진 게시물일 때)
_MAX_ANALYSIS_IMAGES = 3

# 텔레그램 봇 영상 업로드 한도(50MB). 넘으면 미리보기는 썸네일로 대체.
_TELEGRAM_VIDEO_LIMIT = 48 * 1024 * 1024


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


def _edit_choice_keyboard(post_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🤖 AI에게 수정 요청", callback_data=f"aiedit:{post_id}"),
                InlineKeyboardButton("⌨️ 직접 입력", callback_data=f"manedit:{post_id}"),
            ]
        ]
    )


def _render_reel(
    drive: DriveClient,
    videos: list[dict],
    template: Template,
    hook: str,
    menu: str,
) -> bytes:
    """폴더의 영상 클립들을 편집기로 릴스 mp4(bytes)로 만든다. (블로킹)

    hook: 첫 2.5초 상단 자막(AI 제안) / menu: 하단 상시 자막(메뉴명).
    이 함수는 동기(블로킹)라 pipeline에서 asyncio.to_thread로 감싸 호출한다.
    """
    music = os.getenv("REEL_MUSIC_PATH") or None
    if music and not os.path.exists(music):
        logger.warning("REEL_MUSIC_PATH 파일이 없어 음악 없이 편집: %s", music)
        music = None

    with tempfile.TemporaryDirectory() as tmp:
        clip_paths = []
        for i, v in enumerate(videos):
            data = drive.download_file(v["id"])
            ext = ".mov" if "quicktime" in (v.get("mimeType") or "") else ".mp4"
            p = os.path.join(tmp, f"clip{i}{ext}")
            with open(p, "wb") as f:
                f.write(data)
            clip_paths.append(p)

        out = os.path.join(tmp, "reel.mp4")
        video_editor.build_reel(
            clip_paths,
            out,
            target_seconds=template.target_seconds,
            hook=hook or None,
            menu=menu or None,
            watermark="베어글스 송도",
            music_path=music,
        )
        with open(out, "rb") as f:
            return f.read()


def _preview_text(topic: str, result: CaptionResult, is_reel: bool, media_count: int) -> str:
    kind = "릴스(영상)" if is_reel else "사진 게시물"
    lines = [
        f"📸 승인 요청 — {topic}",
        f"형식: {kind} · 미디어 {media_count}개 · 메뉴: {result.menu}",
        "─" * 20,
        "",
        result.caption,
        "",
        " ".join(result.hashtags),
    ]
    if result.overlay_text:
        lines += ["", f"💡 화면 자막 제안: {result.overlay_text}"]
    return "\n".join(lines)


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

    # ── 주제 폴더 목록 ────────────────────────────────────

    async def list_topics(self) -> str:
        try:
            folders = await asyncio.to_thread(self.drive.list_topic_folders)
        except Exception:
            logger.exception("주제 폴더 목록 조회 실패")
            return "⚠️ 폴더 목록을 가져오지 못했습니다."
        if not folders:
            return "콘텐츠 폴더에 주제 폴더가 없습니다. 드라이브에 먼저 만들어주세요."
        names = "\n".join(f"• {f['name']}" for f in folders)
        return f"작업 가능한 주제 폴더:\n{names}\n\n주제명을 그대로 보내면 작업을 시작합니다."

    # ── 주제 폴더 처리 (진입점) ───────────────────────────

    async def process_topic(self, topic: str) -> None:
        """주제명으로 폴더를 찾아 분석 → 캡션 생성 → 승인 요청."""
        try:
            folder = await asyncio.to_thread(self.drive.find_topic_folder, topic)
        except Exception:
            logger.exception("폴더 검색 실패: %s", topic)
            await self.bot.send_message(chat_id=self.chat_id, text="⚠️ 폴더 검색 중 오류가 났어요.")
            return

        if not folder:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=f"'{topic}' 폴더를 못 찾았어요. /목록 으로 폴더 이름을 확인해주세요.",
            )
            return

        try:
            media = await asyncio.to_thread(self.drive.list_media_files, folder["id"])
        except Exception:
            logger.exception("폴더 미디어 목록 실패: %s", topic)
            await self.bot.send_message(chat_id=self.chat_id, text="⚠️ 폴더 내용을 읽지 못했어요.")
            return

        if not media:
            await self.bot.send_message(
                chat_id=self.chat_id, text=f"'{topic}' 폴더에 사진/영상이 없어요."
            )
            return

        videos = [m for m in media if (m.get("mimeType") or "").startswith("video/")]
        is_reel = bool(videos)
        primary = videos[0] if is_reel else media[0]

        post = await asyncio.to_thread(
            self.repo.create_post,
            primary["id"],
            topic,
            primary.get("mimeType") or "image/jpeg",
            folder["id"],
            is_reel,
        )

        try:
            images = await self._fetch_analysis_images(media, is_reel)
            result = await self.generator.generate(
                images=images, topic=topic, is_reel=is_reel, media_count=len(media)
            )
        except Exception as e:
            logger.exception("캡션 생성 실패: %s", topic)
            await asyncio.to_thread(
                self.repo.update_post, post["id"], status=db.STATUS_FAILED, error=str(e)
            )
            await self.bot.send_message(chat_id=self.chat_id, text=f"⚠️ 캡션 생성 실패: {e}")
            return

        await self._finalize_and_request(post["id"], topic, result, media, is_reel, images)

    async def _finalize_and_request(
        self,
        post_id: str,
        topic: str,
        result: CaptionResult,
        media: list[dict],
        is_reel: bool,
        images: list[bytes],
        feedback: str | None = None,
    ) -> None:
        """캡션(+릴스 편집) 결과를 저장하고 승인 요청을 보낸다.

        릴스면 편집기로 실제 릴스를 만들어 Supabase에 올리고, 그 영상을
        미리보기로 함께 보낸다. 편집 실패 시 원본으로 graceful degrade.
        """
        video_bytes: bytes | None = None
        edited_url: str | None = None
        if is_reel:
            videos = [m for m in media if (m.get("mimeType") or "").startswith("video/")]
            template = get_template(None)  # 기본 T1 (추후 주제별 선택 지원)
            try:
                await self.bot.send_message(
                    chat_id=self.chat_id,
                    text="🎬 릴스 자동 편집 중이에요… (영상 길이에 따라 1~2분 걸릴 수 있어요)",
                )
                video_bytes = await asyncio.to_thread(
                    _render_reel,
                    self.drive,
                    videos,
                    template,
                    result.overlay_text or result.menu,  # hook (상단 자막)
                    result.menu,                          # 하단 상시 자막
                )
                edited_url = await asyncio.to_thread(
                    self.media_host.upload_video, post_id, video_bytes
                )
            except Exception as e:
                logger.exception("릴스 편집 실패: %s", topic)
                await self.bot.send_message(
                    chat_id=self.chat_id,
                    text=f"⚠️ 릴스 자동 편집에 실패했어요. 원본 영상으로 진행할게요.\n({e})",
                )

        fields: dict = {
            "status": db.STATUS_AWAITING_APPROVAL,
            "menu": result.menu,
            "caption": result.caption,
            "hashtags": result.hashtags,
            "overlay_text": result.overlay_text,
        }
        if edited_url:
            fields["edited_media_url"] = edited_url
        if feedback is not None:
            fields["feedback"] = feedback
        await asyncio.to_thread(self.repo.update_post, post_id, **fields)

        thumb = images[0] if images else None
        await self._send_approval_request(
            post_id, topic, result, thumb, is_reel, len(media), video_bytes
        )

    async def _fetch_analysis_images(self, media: list[dict], is_reel: bool) -> list[bytes]:
        """분석용 이미지 바이트 목록. 릴스면 영상 썸네일, 사진이면 앞쪽 몇 장."""
        images: list[bytes] = []
        if is_reel:
            video = next(m for m in media if (m.get("mimeType") or "").startswith("video/"))
            thumb = await asyncio.to_thread(self.drive.download_thumbnail, video)
            if thumb:
                images.append(thumb)
        else:
            photos = [m for m in media if (m.get("mimeType") or "").startswith("image/")]
            for m in photos[:_MAX_ANALYSIS_IMAGES]:
                data = await asyncio.to_thread(self.drive.download_file, m["id"])
                images.append(data)
        return images

    # ── 승인 요청 전송 ────────────────────────────────────

    async def _send_approval_request(
        self,
        post_id: str,
        topic: str,
        result: CaptionResult,
        preview_bytes: bytes | None,
        is_reel: bool,
        media_count: int,
        video_bytes: bytes | None = None,
    ) -> None:
        text = _preview_text(topic, result, is_reel, media_count)
        keyboard = _approval_keyboard(post_id)

        # 편집된 릴스가 있으면 영상 자체를 미리보기로 보낸다 (실제 결과 확인용)
        if video_bytes and len(video_bytes) <= _TELEGRAM_VIDEO_LIMIT:
            if len(text) <= 1024:
                await self.bot.send_video(
                    chat_id=self.chat_id,
                    video=video_bytes,
                    caption=text,
                    reply_markup=keyboard,
                )
            else:
                await self.bot.send_video(chat_id=self.chat_id, video=video_bytes)
                await self.bot.send_message(chat_id=self.chat_id, text=text, reply_markup=keyboard)
            return
        if video_bytes:  # 편집은 됐지만 텔레그램 전송 한도 초과
            await self.bot.send_message(
                chat_id=self.chat_id,
                text="(편집된 릴스가 커서 미리보기는 생략했어요. 승인하면 그대로 발행됩니다.)",
            )

        if preview_bytes:
            if len(text) <= 1024:
                await self.bot.send_photo(
                    chat_id=self.chat_id,
                    photo=preview_bytes,
                    caption=text,
                    reply_markup=keyboard,
                )
            else:
                await self.bot.send_photo(chat_id=self.chat_id, photo=preview_bytes)
                await self.bot.send_message(chat_id=self.chat_id, text=text, reply_markup=keyboard)
        else:
            await self.bot.send_message(chat_id=self.chat_id, text=text, reply_markup=keyboard)

    # ── 승인 → 발행 ──────────────────────────────────────

    async def approve(self, post_id: str) -> str:
        post = await asyncio.to_thread(self.repo.get_post, post_id)
        if not post:
            return "게시물을 찾을 수 없습니다."
        if post["status"] == db.STATUS_PUBLISHED:
            return "이미 발행된 게시물입니다."

        full_text = f"{post['caption']}\n\n{' '.join(post['hashtags'] or [])}"
        is_video = (post.get("mime_type") or "").startswith("video/")

        try:
            if is_video:
                # 편집된 릴스가 있으면 그걸 발행, 없으면(편집 실패) 원본 영상
                edited = post.get("edited_media_url")
                if edited:
                    video_url = edited
                else:
                    video_url = await asyncio.to_thread(
                        self.drive.make_public, post["drive_file_id"]
                    )
                bp = await self.buffer.create_post(full_text, video_url=video_url)
            else:
                image_bytes = await asyncio.to_thread(
                    self.drive.download_file, post["drive_file_id"]
                )
                image_url = await asyncio.to_thread(
                    self.media_host.upload_image, post_id, image_bytes
                )
                bp = await self.buffer.create_post(full_text, image_url=image_url)
        except Exception as e:
            logger.exception("발행 실패: post=%s", post_id)
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
        return f"✅ 승인 완료! Buffer 큐에 등록됐습니다.\n(주제: {post['file_name']})"

    # ── 수정: AI 재생성 / 직접 입력 ───────────────────────

    async def request_edit(self, post_id: str, manual: bool = False) -> str:
        await asyncio.to_thread(
            self.repo.update_post, post_id, status=db.STATUS_AWAITING_FEEDBACK
        )
        if manual:
            return "⌨️ 새 캡션 전체를 답장으로 보내주세요. (해시태그까지 원하는 대로 적으면 그대로 발행됩니다)"
        return "✏️ 수정할 내용을 답장으로 보내주세요.\n(예: \"더 짧게\", \"소금빵 강조해줘\")"

    async def regenerate(self, post_id: str, feedback: str) -> None:
        """AI 수정: 피드백 반영 재생성 후 승인 요청 재전송."""
        post = await asyncio.to_thread(self.repo.get_post, post_id)
        if not post:
            await self.bot.send_message(chat_id=self.chat_id, text="게시물을 찾을 수 없습니다.")
            return

        folder_id = post.get("folder_id")
        is_reel = bool(post.get("is_reel"))
        try:
            if folder_id:
                media = await asyncio.to_thread(self.drive.list_media_files, folder_id)
            else:  # 예전 데이터 호환: 단일 파일
                media = [
                    {
                        "id": post["drive_file_id"],
                        "name": post["file_name"],
                        "mimeType": post.get("mime_type") or "image/jpeg",
                    }
                ]
            images = await self._fetch_analysis_images(media, is_reel)
            result = await self.generator.generate(
                images=images,
                topic=post["file_name"],
                is_reel=is_reel,
                media_count=len(media),
                previous_caption=post["caption"],
                feedback=feedback,
            )
        except Exception as e:
            logger.exception("재생성 실패: post=%s", post_id)
            await self.bot.send_message(chat_id=self.chat_id, text=f"⚠️ 재생성 실패: {e}")
            return

        await self._finalize_and_request(
            post_id, post["file_name"], result, media, is_reel, images, feedback=feedback
        )

    async def set_manual_caption(self, post_id: str, text: str) -> None:
        """직접 입력: 사용자가 쓴 텍스트를 그대로 캡션으로 저장."""
        post = await asyncio.to_thread(self.repo.get_post, post_id)
        if not post:
            await self.bot.send_message(chat_id=self.chat_id, text="게시물을 찾을 수 없습니다.")
            return

        result = CaptionResult(
            menu=post.get("menu") or "미상", caption=text.strip(), hashtags=[]
        )
        await asyncio.to_thread(
            self.repo.update_post,
            post_id,
            status=db.STATUS_AWAITING_APPROVAL,
            caption=result.caption,
            hashtags=[],
        )
        is_reel = bool(post.get("is_reel"))
        await self._send_approval_request(post_id, post["file_name"], result, None, is_reel, 1)

    async def cancel(self, post_id: str) -> str:
        await asyncio.to_thread(self.repo.update_post, post_id, status=db.STATUS_CANCELLED)
        return "❌ 취소했습니다. 발행하지 않습니다."
