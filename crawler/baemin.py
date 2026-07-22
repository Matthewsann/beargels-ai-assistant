"""
배달의민족(배민) 크롤러

배민 사장님 페이지에서 주문/매출/리뷰 데이터를 수집한다.
"""

# TODO: .env 에서 BAEMIN_ID, BAEMIN_PASSWORD 불러오기 (python-dotenv)
# TODO: Selenium WebDriver 초기화 (webdriver-manager 로 드라이버 자동 설치)
# TODO: 배민 사장님 사이트 로그인 함수 구현
# TODO: 주문 내역 크롤링 함수 구현 (날짜 범위 파라미터)
# TODO: 매출 데이터 크롤링 함수 구현
# TODO: 신규 리뷰 크롤링 함수 구현
# TODO: 수집 결과를 dict/list 로 정규화하여 반환
# TODO: 예외 처리 및 재시도 로직 추가


def fetch_orders():
    """배민 주문 내역을 크롤링해 반환한다."""
    # TODO: 구현
    raise NotImplementedError


def fetch_reviews():
    """배민 신규 리뷰를 크롤링해 반환한다."""
    # TODO: 구현
    raise NotImplementedError


if __name__ == "__main__":
    # TODO: 단독 실행 시 테스트용 크롤링 수행
    pass
