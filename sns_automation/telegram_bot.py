"""텔레그램 인터페이스.

- 주제명(폴더명)을 보내면 그 폴더를 분석해 승인 요청을 만든다.
- /목록  : 작업 가능한 주제 폴더 목록
- 승인 요청에는 [승인 / 수정 / 취소] 버튼. 수정은 'AI 수정' / '직접 입력' 선택.
"""

import logging

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

# chat_data 키: 수정 입력을 기다리는 상태 {"post_id": str, "mode": "ai"|"manual"}
PENDING_KEY = "sns_pending_edit"


def build_application(token: str, allowed_chat_id: str) -> Application:
    """파이프라인은 bot_data['pipeline']에 나중에 주입된다 (main.py 참고)."""
    app = ApplicationBuilder().token(token).build()
    chat_filter = filters.Chat(chat_id=int(allowed_chat_id))

    app.add_handler(CommandHandler("start", _start, filters=chat_filter))
    app.add_handler(CommandHandler(["목록", "list"], _list, filters=chat_filter))
    app.add_handler(
        CallbackQueryHandler(_on_button, pattern=r"^(approve|edit|aiedit|manedit|cancel):")
    )
    app.add_handler(MessageHandler(chat_filter & filters.TEXT & ~filters.COMMAND, _on_text))
    return app


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "베어글스 송도 SNS 봇입니다.\n"
        "1) 드라이브 '콘텐츠 생성' 아래 주제 폴더에 사진/영상을 정리하세요.\n"
        "2) 여기에 그 주제명을 보내면 릴스/게시물 캡션을 만들어 드립니다.\n"
        "3) /목록 으로 폴더 이름을 확인할 수 있어요."
    )


async def _list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pipeline = context.bot_data["pipeline"]
    message = await pipeline.list_topics()
    await update.message.reply_text(message)


async def _on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    action, post_id = query.data.split(":", 1)
    pipeline = context.bot_data["pipeline"]

    if action == "approve":
        await query.edit_message_reply_markup(reply_markup=None)
        message = await pipeline.approve(post_id)
        await query.message.reply_text(message)
    elif action == "edit":
        from .pipeline import _edit_choice_keyboard

        await query.message.reply_text(
            "어떻게 수정할까요?", reply_markup=_edit_choice_keyboard(post_id)
        )
    elif action == "aiedit":
        context.chat_data[PENDING_KEY] = {"post_id": post_id, "mode": "ai"}
        message = await pipeline.request_edit(post_id, manual=False)
        await query.message.reply_text(message)
    elif action == "manedit":
        context.chat_data[PENDING_KEY] = {"post_id": post_id, "mode": "manual"}
        message = await pipeline.request_edit(post_id, manual=True)
        await query.message.reply_text(message)
    elif action == "cancel":
        await query.edit_message_reply_markup(reply_markup=None)
        message = await pipeline.cancel(post_id)
        await query.message.reply_text(message)


async def _on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """수정 대기 중이면 수정 처리, 아니면 주제명으로 폴더 작업 시작."""
    pipeline = context.bot_data["pipeline"]
    text = update.message.text.strip()

    pending = context.chat_data.pop(PENDING_KEY, None)
    if pending:
        if pending["mode"] == "manual":
            await update.message.reply_text("⌨️ 입력한 캡션으로 미리보기를 만들게요...")
            await pipeline.set_manual_caption(pending["post_id"], text)
        else:
            await update.message.reply_text("🔄 수정 반영해서 다시 만들게요...")
            await pipeline.regenerate(pending["post_id"], text)
        return

    # 일반 텍스트 = 주제명 → 폴더 작업
    await update.message.reply_text(f"🔎 '{text}' 폴더 분석 중이에요...")
    await pipeline.process_topic(text)
