"""
베어글스 AI 비서

Supabase 에 저장된 데이터를 분석하고, LLM 을 활용해
매출 리포트/인사이트/리뷰 요약을 생성한다.
"""

# TODO: .env 에서 OPENAI_API_KEY 불러오기 (python-dotenv)
# TODO: database.supabase_client 로 최근 데이터 조회
# TODO: 일일/주간 매출 요약 생성 함수 구현
# TODO: LLM 프롬프트 구성 (매출 추이, 인기 메뉴, 리뷰 감성 분석)
# TODO: 리뷰 요약 및 부정 리뷰 알림 로직 구현
# TODO: 분석 결과를 텔레그램 전송용 텍스트로 포맷팅


def generate_daily_report():
    """일일 매출/리뷰 리포트를 생성해 문자열로 반환한다."""
    # TODO: 구현
    raise NotImplementedError


def summarize_reviews(reviews):
    """리뷰 목록을 LLM 으로 요약한다."""
    # TODO: 구현
    raise NotImplementedError
