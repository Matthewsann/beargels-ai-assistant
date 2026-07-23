"""텔레그램 승인 인터페이스 (승인/수정/취소 버튼 + 수정 피드백 입력)."""

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

# chat_data 키: 수정 피드백을 기다리는 post_id
PENDING_FEEDBACK_KEY = "sns_pending_feedback_post_id"


def build_application(token: str, allowed_chat_id: str) -> Application:
    """파이프라인은 bot_data['pipeline']에 나중에 주입된다 (main.py 참고)."""
    app = ApplicationBuilder().token(token).build()
    chat_filter = filters.Chat(chat_id=int(allowed_chat_id))

    app.add_handler(CommandHandler("start", _start, filters=chat_filter))
    app.add_handler(CallbackQueryHandler(_on_button, pattern=r"^(approve|edit|cancel):"))
    app.add_handler(MessageHandler(chat_filter & filters.TEXT & ~filters.COMMAND, _on_text))
    return app


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "베어글스 SNS 자동화 봇입니다.\n"
        "구글 드라이브에 사진/영상을 올리면 캡션을 만들어 승인 요청을 보냅니다."
    )


async def _on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    action, post_id = query.data.split(":", 1)
    pipeline = context.bot_data["pipeline"]

    if action == "approve":
        message = await pipeline.approve(post_id)
        await query.message.reply_text(message)
    elif action == "edit":
        context.chat_data[PENDING_FEEDBACK_KEY] = post_id
        message = await pipeline.request_edit(post_id)
        await query.message.reply_text(message)
    elif action == "cancel":
        message = await pipeline.cancel(post_id)
        await query.message.reply_text(message)

    # 버튼 중복 클릭 방지: 처리된 메시지의 버튼 제거
    if action != "edit":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass


async def _on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """수정 버튼을 누른 뒤 들어오는 텍스트는 수정 피드백으로 처리한다."""
    post_id = context.chat_data.pop(PENDING_FEEDBACK_KEY, None)
    if not post_id:
        return  # 승인 대기 중인 수정 요청이 없으면 무시 (기존 비서 봇 몫)

    pipeline = context.bot_data["pipeline"]
    await update.message.reply_text("🔄 수정 반영해서 다시 만들게요...")
    await pipeline.regenerate(post_id, update.message.text)
