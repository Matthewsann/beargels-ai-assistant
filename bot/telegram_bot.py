"""
텔레그램 봇 — 베어글스 AI 운영 비서 (대화형)

Matthew 가 텔레그램으로 비서와 대화한다.

명령:
  /start    인사 + chat_id 안내
  /morning  아침 브리핑 (어제 매출 + 오늘 할 일 우선순위)
  /evening  저녁 리뷰 (오늘 완료/미완료 + 매출 + 내일 챙길 것)
  /tasks    오늘 할 일 목록(완료/미완료)
  /done ..  할 일 완료 처리
  /report   즉시 일일 리포트(수집→분석)
  /reviews  최근 리뷰 AI 요약

자유 대화(명령 아닌 일반 메시지):
  - "…완료" 포함 → 해당 할 일 완료 처리 (예: "재료 주문 완료")
  - 그 외 → 오늘 할 일로 등록(줄바꿈으로 여러 개) 후 아침 브리핑 응답

주의: 크롤링(Playwright)은 블로킹이라 asyncio.to_thread 로 실행한다.
      attach 모드로 로그인된 Chrome(launch_chrome.bat)이 켜져 있어야 한다.
실행: python -m bot.telegram_bot
"""

import asyncio
import logging
import os
from datetime import date, timedelta

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, filters,
)

from assistant.beargels import (
    evening_review, generate_daily_report, morning_briefing, summarize_reviews,
)
from database import supabase_client as db
from scheduler.jobs import crawl_job

load_dotenv()

logger = logging.getLogger(__name__)
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")


# ---------------------------------------------------------------------------
# 명령 핸들러
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🐻 베어글스 운영 비서입니다.\n"
        f"이 대화 chat_id: {update.effective_chat.id}\n\n"
        "• 아침에 할 일 보내면 우선순위 정리해드려요\n"
        "• \"재료 주문 완료\" 처럼 보내면 체크됩니다\n"
        "• /morning /evening /tasks /report /reviews"
    )


async def cmd_morning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """오늘 등록된 할 일 + 어제 매출로 아침 브리핑."""
    tasks = await asyncio.to_thread(db.get_tasks)
    texts = [t["description"] for t in tasks]
    y_orders = await asyncio.to_thread(
        db.get_orders_by_date, date.today() - timedelta(days=1))
    brief = await asyncio.to_thread(morning_briefing, texts, y_orders)
    await update.message.reply_text(brief)


async def cmd_evening(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """오늘 마감 정리: 신선한 매출/리뷰 수집 + 할 일 완료 현황."""
    await update.message.reply_text("🌙 오늘 정리 중… (수집+분석)")
    orders, reviews = await asyncio.to_thread(crawl_job)
    tasks = await asyncio.to_thread(db.get_tasks)
    done = [t for t in tasks if t["status"] == "done"]
    undone = [t for t in tasks if t["status"] != "done"]
    text = await asyncio.to_thread(
        evening_review, done, undone, orders, reviews)
    await asyncio.to_thread(db.save_summary, "evening", text)
    await update.message.reply_text(text)


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = await asyncio.to_thread(db.get_tasks)
    if not tasks:
        await update.message.reply_text("오늘 등록된 할 일이 없어요.")
        return
    lines = [("✅" if t["status"] == "done" else "⬜") + f" {t['description']}"
             for t in tasks]
    await update.message.reply_text("📋 오늘 할 일\n" + "\n".join(lines))


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("완료할 할 일을 적어주세요. 예: /done 재료 주문")
        return
    await _complete(update, text)


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 리포트 생성 중… (수집+분석)")
    orders, reviews = await asyncio.to_thread(crawl_job)
    if not orders and not reviews:
        await update.message.reply_text("수집된 데이터가 없어요(세션/크롤링 확인).")
        return
    report = await asyncio.to_thread(generate_daily_report, orders, reviews)
    await update.message.reply_text(report)


async def cmd_reviews(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💬 최근 리뷰 요약 중…")
    _, reviews = await asyncio.to_thread(crawl_job)
    summary = await asyncio.to_thread(summarize_reviews, reviews)
    await update.message.reply_text(summary)


# ---------------------------------------------------------------------------
# 자유 대화 (명령이 아닌 일반 메시지)
# ---------------------------------------------------------------------------

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return
    # "…완료" → 완료 처리
    if "완료" in text:
        target = text.replace("완료", "").strip(" .!~")
        await _complete(update, target)
        return
    # 그 외 → 오늘 할 일로 등록(줄바꿈/쉼표로 여러 개) 후 아침 브리핑
    items = [s.strip("-•* \t") for s in text.replace(",", "\n").splitlines()
             if s.strip("-•* \t")]
    if not items:
        return
    await asyncio.to_thread(db.add_tasks, items)
    y_orders = await asyncio.to_thread(
        db.get_orders_by_date, date.today() - timedelta(days=1))
    all_texts = [t["description"] for t in await asyncio.to_thread(db.get_tasks)]
    brief = await asyncio.to_thread(morning_briefing, all_texts, y_orders)
    await update.message.reply_text(brief)


async def _complete(update: Update, target):
    """target 에 해당하는 오늘 할 일을 완료 처리한다."""
    t = await asyncio.to_thread(db.find_pending_task, target)
    if t:
        await asyncio.to_thread(db.complete_task, t["id"])
        await update.message.reply_text(f"✅ 완료: {t['description']}")
    else:
        await update.message.reply_text(
            f"'{target}'에 맞는 할 일을 못 찾았어요. /tasks 로 확인해보세요.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """핸들러에서 난 예외를 잡아 로깅하고, 사용자에게 짧게 알린다."""
    logger.exception("핸들러 오류", exc_info=context.error)
    msg = str(context.error)
    if isinstance(update, Update) and update.effective_message:
        if "row-level security" in msg or "42501" in msg:
            hint = ("DB 저장 권한(RLS 정책)이 아직 안 열렸어요. "
                    "tasks/daily_summaries 정책 SQL 실행이 필요합니다.")
        else:
            hint = "처리 중 오류가 났어요. 잠시 후 다시 시도해주세요."
        try:
            await update.effective_message.reply_text(hint)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not TOKEN:
        raise RuntimeError(".env 에 TELEGRAM_BOT_TOKEN 을 설정하세요.")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("morning", cmd_morning))
    app.add_handler(CommandHandler("evening", cmd_evening))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("reviews", cmd_reviews))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    logger.info("텔레그램 봇 시작(폴링). @beargels_assistant_bot")
    app.run_polling()


if __name__ == "__main__":
    main()
