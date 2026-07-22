"""
Supabase 클라이언트

크롤링한 주문/매출/리뷰 데이터를 Supabase 에 저장하고 조회한다.
"""

# TODO: .env 에서 SUPABASE_URL, SUPABASE_KEY 불러오기 (python-dotenv)
# TODO: supabase-py 로 클라이언트 생성 (create_client)
# TODO: 테이블 스키마 정의 (orders, sales, reviews 등)
# TODO: 데이터 저장(insert/upsert) 함수 구현
# TODO: 데이터 조회(select) 함수 구현 (날짜/플랫폼 필터)
# TODO: 중복 저장 방지 로직 추가


def get_client():
    """Supabase 클라이언트 인스턴스를 반환한다."""
    # TODO: 구현
    raise NotImplementedError


def save_orders(orders):
    """주문 데이터를 Supabase 에 저장한다."""
    # TODO: 구현
    raise NotImplementedError


def save_reviews(reviews):
    """리뷰 데이터를 Supabase 에 저장한다."""
    # TODO: 구현
    raise NotImplementedError
