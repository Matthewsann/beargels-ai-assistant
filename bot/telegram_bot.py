"""
텔레그램 봇 — 베어글스 AI 운영 비서 (대화형)

Matthew 가 텔레그램으로 비서와 대화한다.

명령:
  /start    인사 + chat_id 안내
  /morning  아침 브리핑 (어제 매출 + 오늘 할 일 우선순위)
  /evening  저녁 리뷰 (오늘 완료/미완료 + 매출 + 내일 챙길 것)
  /tasks    오늘 할 일 목록(완료/미완료)
  /done ..  할 일 완료 처리
  /sales    오늘 매출 즉시(DB 조회, 재크롤링 없음) — 플랫폼별+합계
  /report   즉시 일일 리포트(수집→분석)
  /reviews  최근 리뷰 AI 요약
  /drafts   미답변 리뷰 답글 초안 제시 → 버튼 승인으로 게시
            (⚠️ WRITE_DRY_RUN=false 여야 실제 게시. 기본은 미리보기)

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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

from assistant.beargels import (
    compute_order_stats, evening_review, generate_daily_report,
    morning_briefing, summarize_reviews,
)
from crawler.review_reply import ReplyToReviewAction, propose_replies
from crawler.write_guard import _default_dry_run
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
        "• /morning /evening /tasks /sales /report /reviews /drafts\n"
        "• /sales: 오늘 매출 즉시(DB) · /drafts: 리뷰 답글 승인 게시"
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


_PLAT_KO = {"baemin": "배민", "coupang": "쿠팡"}


async def cmd_sales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """오늘 매출을 즉시 보여준다(DB 조회, 재크롤링 없음 → 빠름).

    스케줄러가 2시간마다 수집해 둔 DB 데이터를 읽으므로 최근값 기준이다.
    실시간 최신값이 필요하면 /report(수집+분석)를 쓴다.
    """
    orders = await asyncio.to_thread(db.get_orders_by_date, date.today())
    if not orders:
        await update.message.reply_text(
            "오늘 저장된 주문이 아직 없어요. (스케줄러 수집 전이면 /report 로 즉시 수집)")
        return
    total = compute_order_stats(orders)
    lines = [f"💰 오늘 매출 ({date.today():%m/%d}, DB 기준)",
             f"• 합계: {total['order_count']}건 / {total['revenue']:,}원 "
             f"(건단가 {total['avg_order_value']:,}원)"]
    by_plat = {}
    for o in orders:
        by_plat.setdefault(o.get("platform"), []).append(o)
    for plat, os_ in sorted(by_plat.items()):
        s = compute_order_stats(os_)
        lines.append(f"  – {_PLAT_KO.get(plat, plat)}: "
                     f"{s['order_count']}건 / {s['revenue']:,}원")
    if total["top_menus"]:
        top = ", ".join(f"{m}({c})" for m, c in total["top_menus"][:3])
        lines.append(f"• 인기: {top}")
    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# 리뷰 답글 승인 게이트 (/drafts → 초안 제시 → 버튼 승인 → 게시)
# ---------------------------------------------------------------------------

def _draft_key(review):
    """리뷰를 식별하는 짧은 콜백 키(플랫폼:리뷰번호)."""
    return f"{review.get('platform')}:{review.get('review_no')}"


async def cmd_drafts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """미답변 리뷰를 모아 답글 초안을 하나씩 버튼과 함께 제시한다.

    ⚠️ 게시는 WRITE_DRY_RUN 이 false 여야만 실제로 나간다(기본 true=미리보기).
    에스컬레이션(민감) 리뷰는 게시 버튼 없이 '직접 대응' 안내만 한다.
    """
    dry = _default_dry_run()
    mode = "🧪 dry-run(미리보기)" if dry else "🔴 실게시 모드"
    await update.message.reply_text(
        f"💬 미답변 리뷰 답글 초안 준비 중… ({mode})")
    _, reviews = await asyncio.to_thread(crawl_job)
    proposals = await asyncio.to_thread(propose_replies, reviews, 10)
    if not proposals:
        await update.message.reply_text("미답변 리뷰가 없어요. 👍")
        return

    store = context.bot_data.setdefault("drafts", {})
    for p in proposals:
        review, draft = p["review"], p["draft"]
        key = _draft_key(review)
        store[key] = {"review": review, "draft": draft}
        if p["escalate"]:
            await update.message.reply_text(
                p["preview"] + "\n\n→ 게시 버튼 없음. 사장님이 직접 대응하세요.")
            continue
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ 게시", callback_data=f"rp|post|{key}"),
            InlineKeyboardButton("⏭️ 넘기기", callback_data=f"rp|skip|{key}"),
        ]])
        await update.message.reply_text(p["preview"], reply_markup=kb)


async def on_reply_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """답글 게시/넘기기 버튼 콜백."""
    q = update.callback_query
    await q.answer()
    try:
        _, action, key = q.data.split("|", 2)
    except ValueError:
        return
    store = context.bot_data.get("drafts", {})
    item = store.get(key)
    if not item:
        await q.edit_message_text(q.message.text + "\n\n⚠️ 만료된 초안입니다. /drafts 다시.")
        return

    if action == "skip":
        await q.edit_message_text(q.message.text + "\n\n⏭️ 넘김.")
        return

    # 게시(승인). WRITE_DRY_RUN=true 면 실제로는 미리보기만 반환된다.
    act = ReplyToReviewAction(item["review"], reply_text=item["draft"])
    try:
        res = await asyncio.to_thread(act.run, True)  # confirm=True
    except Exception as e:  # noqa: BLE001
        await q.edit_message_text(q.message.text + f"\n\n❌ 게시 실패: {str(e)[:120]}")
        return
    if res.get("applied"):
        store.pop(key, None)
        await q.edit_message_text(q.message.text + "\n\n✅ 게시 완료.")
    else:
        await q.edit_message_text(
            q.message.text + "\n\n🧪 dry-run 모드라 실제 게시는 안 됐어요. "
            "실게시하려면 .env 의 WRITE_DRY_RUN=false 로 바꾸고 봇 재시작 후 다시 눌러주세요.")


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
    app.add_handler(CommandHandler("sales", cmd_sales))
    app.add_handler(CommandHandler("drafts", cmd_drafts))
    app.add_handler(CallbackQueryHandler(on_reply_action, pattern=r"^rp\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    logger.info("텔레그램 봇 시작(폴링). @beargels_assistant_bot")
    app.run_polling()


if __name__ == "__main__":
    main()
