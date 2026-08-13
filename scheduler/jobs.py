"""
스케줄러 잡(job)

정해진 시간에 크롤링 → 저장 → 분석 → 기록 파이프라인을 실행한다.
(알림 수단은 텔레그램에서 '로그 + 화면 오류기록 + reports/ 파일'로
 바뀌었다 — 사장님 지시 2026-08-13.)

  crawl_job   : 배민(향후 쿠팡) 주문/리뷰 수집 → Supabase 저장
  report_job  : 수집 데이터로 일일 리포트 생성 → reports/ 저장
  main        : APScheduler 로 정기 실행 (기본: 2시간마다 수집, 매일 09시 리포트)

각 잡은 예외를 삼켜 로깅한다(한 번 실패해도 스케줄러가 죽지 않도록).
전제: attach 모드로 로그인된 Chrome(launch_chrome.bat)이 켜져 있어야 한다.
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler

from assistant.beargels import (
    format_complaint_report, generate_daily_report, is_serious_review,
)
from alerts import notify_owner
from crawler.baemin import BaeminCrawler
from crawler.browser import SessionExpiredError
from crawler.coupang import CoupangCrawler
from database import supabase_client

logger = logging.getLogger(__name__)

TIMEZONE = "Asia/Seoul"
REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"


def save_report(name, text):
    """리포트를 reports/ 에 파일로 남긴다(텔레그램 대체). 저장 경로 반환.

    쓰기에 실패해도 예외를 올리지 않는다 — 저장 실패로 스케줄러가 죽으면
    다음 수집까지 통째로 멈춘다.
    """
    try:
        REPORTS_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        path = REPORTS_DIR / f"{name}-{stamp}.md"
        path.write_text(text, encoding="utf-8")
        (REPORTS_DIR / f"latest-{name}.md").write_text(text, encoding="utf-8")
        return path
    except Exception:  # noqa: BLE001
        logger.exception("리포트 저장 실패(%s)", name)
        return None


def _crawl_baemin():
    """배민 주문/리뷰 수집. 실패해도 예외를 삼켜 (orders, reviews) 반환."""
    try:
        with BaeminCrawler() as c:
            return c.fetch_orders(), c.fetch_reviews()
    except SessionExpiredError:
        logger.warning("배민 세션 만료 — 재로그인 필요(알림 전송됨)")
    except Exception:
        logger.exception("배민 수집 실패")
    return [], []


def _crawl_coupang():
    """쿠팡 주문·리뷰 수집. 실패해도 예외를 삼켜 (orders, reviews) 반환.

    한 세션에서 주문·리뷰를 함께 긁는다(주문 먼저). 주문 수집이 실패해도
    리뷰는 이어서 시도한다.
    """
    orders, reviews = [], []
    try:
        with CoupangCrawler() as c:
            try:
                orders = c.fetch_orders(days=1)
            except SessionExpiredError:
                raise
            except Exception:
                logger.exception("쿠팡 주문 수집 실패")
            reviews = c.fetch_reviews(days=2)
    except SessionExpiredError:
        logger.warning("쿠팡 세션 만료 — 재로그인 필요(알림 전송됨)")
    except Exception:
        logger.exception("쿠팡 수집 실패")
    return orders, reviews


def crawl_job():
    """크롤링 → 저장 파이프라인. (orders, reviews) 반환.

    배민·쿠팡을 각각 독립적으로 수집한다(한 플랫폼 실패가 다른 쪽을 막지 않음).
    """
    orders, reviews = _crawl_baemin()
    cp_orders, cp_reviews = _crawl_coupang()
    orders = list(orders) + cp_orders
    reviews = list(reviews) + cp_reviews

    # Supabase 저장(테이블 미생성 등 실패해도 수집 결과는 반환)
    try:
        supabase_client.save_orders(orders)
        supabase_client.save_reviews(reviews)
    except Exception:
        logger.exception("Supabase 저장 실패(schema.sql 실행 여부 확인)")

    logger.info("crawl_job 완료: 주문 %d, 리뷰 %d", len(orders), len(reviews))
    return orders, reviews


def report_job():
    """리포트 생성 → reports/ 파일 저장(텔레그램 제거, 2026-08-13)."""
    orders, reviews = crawl_job()
    if not orders and not reviews:
        notify_owner("오늘 수집된 데이터가 없습니다(세션·크롤링 확인 필요).",
                     kind="NoDataToday", source="scheduler", path="report_job")
        return
    report = generate_daily_report(orders, reviews)
    path = save_report("daily", report)
    logger.info("report_job 완료: 리포트 저장 %s", path)


def check_complaint_reviews(label=""):
    """심각(불만) 리뷰를 수집·필터해 텔레그램으로 보고한다.

    같은 날 이미 보고한 리뷰는 제외(daily_summaries 에 review_no 로그).
    신규가 없으면 '이상 없음'을 짧게 보낸다.
    """
    _, reviews = crawl_job()
    serious = [r for r in reviews if is_serious_review(r)]

    logrow = supabase_client.get_summary("complaint_alert_log")
    try:
        alerted = set(json.loads(logrow["content"])) if logrow else set()
    except Exception:  # noqa: BLE001
        alerted = set()
    new = [r for r in serious
           if r.get("review_no") and r["review_no"] not in alerted]

    if not new:
        logger.info("심각 리뷰 체크%s: 신규 불만 리뷰 없음",
                    (" — " + label) if label else "")
        return
    # 불만 리뷰는 사장님이 바로 알아야 하므로 화면(오류기록)에 올린다.
    report = format_complaint_report(new, label)
    save_report("complaint", report)
    notify_owner(f"불만 리뷰 {len(new)}건 — 확인이 필요합니다"
                 f"{(' (' + label + ')') if label else ''}. "
                 f"자세한 내용은 reports/ 최신 complaint 파일 참고.",
                 kind="SeriousReview", source="scheduler",
                 path="check_complaint_reviews")
    alerted |= {r["review_no"] for r in new if r.get("review_no")}
    try:
        supabase_client.save_summary("complaint_alert_log", json.dumps(list(alerted)))
    except Exception:
        logger.exception("심각 리뷰 로그 저장 실패")
    logger.info("심각 리뷰 보고: 신규 %d건", len(new))


_PLAT_KO = {"coupang": "쿠팡", "baemin": "배민"}


def _format_news(items, heading, label, total=None):
    """공지 목록을 텔레그램 보고 텍스트로 포맷한다."""
    head = f"📢 {heading}{(' — ' + label) if label else ''}"
    if total is not None:
        head += f" (현재 총 {total}건)"
    lines = [head, "─────────"]
    for it in items:
        date = f" ({it['date']})" if it.get("date") else ""
        title = re.sub(r"\s*\d+일 전 업데이트", "", it["title"]).strip()
        lines.append(
            f"[{_PLAT_KO.get(it['platform'], it['platform'])}·"
            f"{it['category']}] {title}{date}\n  {it['url']}")
    return "\n".join(lines)


def check_platform_news(label=""):
    """쿠팡·배민 공지·정책·혜택 소식을 점검해 신규만 텔레그램으로 보고한다.

    글 URL 기준으로 dedup(daily_summaries 'platform_news_seen'). 첫 실행은 과거
    전체를 스팸하지 않도록 현재 목록을 시드하고 최근 몇 건만 요약해 알린다.
    매월 1·15일 스케줄로 실행(정책 개정·이벤트·혜택 놓치지 않게).
    """
    from crawler.platform_news import fetch_all
    items = fetch_all()
    if not items:
        logger.warning("플랫폼 공지 수집 0건 — 건너뜀")
        return

    logrow = supabase_client.get_summary("platform_news_seen")
    first_run = logrow is None
    try:
        seen = set(json.loads(logrow["content"])) if logrow else set()
    except Exception:  # noqa: BLE001
        seen = set()
    new = [it for it in items if it["url"] not in seen]

    if first_run:
        save_report("platform_news", _format_news(
            items[:8], "플랫폼 소식 점검 시작(현재 공지)", label, total=len(items)))
    elif new:
        save_report("platform_news", _format_news(new, "신규 공지/정책/혜택", label))
        notify_owner(f"배민·쿠팡 신규 공지 {len(new)}건 — reports/ 최신 "
                     f"platform_news 파일을 확인해 주세요.",
                     kind="PlatformNews", source="scheduler",
                     path="check_platform_news")
    else:
        logger.info("플랫폼 공지 점검%s: 신규 없음",
                    (" — " + label) if label else "")

    seen |= {it["url"] for it in items}
    try:
        supabase_client.save_summary("platform_news_seen", json.dumps(list(seen)))
    except Exception:
        logger.exception("플랫폼 공지 로그 저장 실패")
    logger.info("플랫폼 공지 점검: 신규 %d건(첫실행=%s)", len(new), first_run)


def main():
    """스케줄러를 시작한다(blocking)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    sched = BlockingScheduler(timezone=TIMEZONE)
    # 정기 수집: 2시간마다
    sched.add_job(crawl_job, "interval", hours=2, id="crawl")
    # 일일 리포트: 매일 09:00
    sched.add_job(report_job, "cron", hour=9, minute=0, id="daily_report")
    # 심각 리뷰 보고: 매일 14:00, 22:00
    sched.add_job(lambda: check_complaint_reviews("오후 2시"),
                  "cron", hour=14, minute=0, id="complaint_2pm")
    sched.add_job(lambda: check_complaint_reviews("저녁 10시"),
                  "cron", hour=22, minute=0, id="complaint_10pm")
    # 플랫폼 공지·정책·혜택 점검: 매월 1일·15일 10:00
    sched.add_job(lambda: check_platform_news("정기점검"),
                  "cron", day="1,15", hour=10, minute=0, id="platform_news")
    logger.info(
        "스케줄러 시작 (수집 2h, 리포트 09:00, 심각리뷰 14·22시, "
        "플랫폼공지 매월 1·15일 10시 %s)", TIMEZONE)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("스케줄러 종료")


if __name__ == "__main__":
    main()
