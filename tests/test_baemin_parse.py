"""배민 리뷰 파싱 회귀 테스트 — 닉네임 추출(날짜 오파싱 방지)."""

from bs4 import BeautifulSoup

from crawler.baemin import BaeminCrawler


def _mk(leaves):
    inner = "".join(f"<span>{x}</span>" for x in leaves)
    html = f'<div class="ReviewContent-module__x">{inner}</div>'
    return BeautifulSoup(html, "html.parser").select_one(
        '[class*="ReviewContent-module__"]')


def test_nickname_with_delivery_badge():
    # 잎: [배달유형, 닉네임, 날짜, ...] — 닉네임은 날짜 바로 앞.
    rev = BaeminCrawler._parse_review_item(_mk([
        "알뜰배달", "낙지호롱구이", "2026년 7월 24일",
        "리뷰번호 2026072402117595", "1회 주문 고객", "너무 맛있어요!!!"]))
    assert rev["author"] == "낙지호롱구이"
    assert rev["delivery_type"] == "알뜰배달"


def test_nickname_without_delivery_badge():
    # 배달유형 badge 가 없으면 닉네임이 맨 앞 — 그래도 날짜가 작성자로 잡히면 안 됨.
    rev = BaeminCrawler._parse_review_item(_mk([
        "먹는게남는거다", "2026년 7월 20일", "리뷰번호 123",
        "2회 주문 고객", "맛있어요"]))
    assert rev["author"] == "먹는게남는거다"
    assert rev["author"] != "2026년 7월 20일"


def test_review_no_extracted():
    rev = BaeminCrawler._parse_review_item(_mk([
        "한집배달", "빵순이", "2026년 7월 1일", "리뷰번호 999888", "굿"]))
    assert rev["review_no"] == "999888"
    assert rev["author"] == "빵순이"
