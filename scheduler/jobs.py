"""
스케줄러 잡(job)

정해진 시간에 크롤링 → 저장 → 분석 → 알림 파이프라인을 실행한다.

  crawl_job   : 배민(향후 쿠팡) 주문/리뷰 수집 → Supabase 저장
  report_job  : 수집 데이터로 일일 리포트 생성 → 텔레그램 전송
  main        : APScheduler 로 정기 실행 (기본: 2시간마다 수집, 매일 09시 리포트)

각 잡은 예외를 삼켜 로깅한다(한 번 실패해도 스케줄러가 죽지 않도록).
전제: attach 모드로 로그인된 Chrome(launch_chrome.bat)이 켜져 있어야 한다.
"""

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from assistant.beargels import generate_daily_report
from bot.notify import send_message
from crawler.baemin import BaeminCrawler
from crawler.browser import SessionExpiredError
from crawler.coupang import CoupangCrawler
from database import supabase_client

logger = logging.getLogger(__name__)

TIMEZONE = "Asia/Seoul"


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
    """쿠팡 리뷰 수집(주문 API 미구현). 실패해도 삼켜 reviews 반환."""
    try:
        with CoupangCrawler() as c:
            return c.fetch_reviews(days=2)
    except SessionExpiredError:
        logger.warning("쿠팡 세션 만료 — 재로그인 필요(알림 전송됨)")
    except Exception:
        logger.exception("쿠팡 수집 실패")
    return []


def crawl_job():
    """크롤링 → 저장 파이프라인. (orders, reviews) 반환.

    배민·쿠팡을 각각 독립적으로 수집한다(한 플랫폼 실패가 다른 쪽을 막지 않음).
    """
    orders, reviews = _crawl_baemin()
    reviews = list(reviews) + _crawl_coupang()

    # Supabase 저장(테이블 미생성 등 실패해도 수집 결과는 반환)
    try:
        supabase_client.save_orders(orders)
        supabase_client.save_reviews(reviews)
    except Exception:
        logger.exception("Supabase 저장 실패(schema.sql 실행 여부 확인)")

    logger.info("crawl_job 완료: 주문 %d, 리뷰 %d", len(orders), len(reviews))
    return orders, reviews


def report_job():
    """리포트 생성 → 텔레그램 전송 파이프라인."""
    orders, reviews = crawl_job()
    if not orders and not reviews:
        send_message("⚠️ 오늘 수집된 데이터가 없습니다(세션/크롤링 확인).")
        return
    report = generate_daily_report(orders, reviews)
    send_message(report)
    logger.info("report_job 완료: 리포트 전송")


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
    logger.info("스케줄러 시작 (수집 2h, 리포트 매일 09:00 %s)", TIMEZONE)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("스케줄러 종료")


if __name__ == "__main__":
    main()
