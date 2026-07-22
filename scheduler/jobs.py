"""
스케줄러 잡(job)

정해진 시간에 크롤링 -> 저장 -> 분석 -> 알림 파이프라인을 실행한다.
"""

# TODO: APScheduler (BlockingScheduler / BackgroundScheduler) 초기화
# TODO: crawler.baemin, crawler.coupang 크롤링 잡 등록
# TODO: database.supabase_client 저장 연동
# TODO: assistant.beargels 일일 리포트 생성 잡 등록
# TODO: bot.telegram_bot 리포트 전송 연동
# TODO: 크론 스케줄 설정 (예: 매일 오전 9시 리포트)
# TODO: 예외 처리 및 로깅 추가


def crawl_job():
    """크롤링 -> 저장 파이프라인을 실행한다."""
    # TODO: 구현
    raise NotImplementedError


def report_job():
    """리포트 생성 -> 텔레그램 전송 파이프라인을 실행한다."""
    # TODO: 구현
    raise NotImplementedError


def main():
    """스케줄러를 시작한다."""
    # TODO: 구현
    raise NotImplementedError


if __name__ == "__main__":
    main()
