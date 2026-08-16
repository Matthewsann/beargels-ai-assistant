"""배민 답글 등록 시 리뷰 카드 찾기 회귀 테스트.

증상: 8/8 자 배민 리뷰에 답글 등록을 눌러도 '배민·쿠팡 화면에서 이 리뷰를
못 찾았어요'만 반복(사장님 제보 2026-08-16).

원인: 배민 리뷰 목록은 **스크롤만으로는 다음 묶음이 안 나온다** — 목록 아래
'더보기' 버튼을 눌러야 한다(수집기는 그렇게 하는데 등록 쪽은 스크롤만 했다).
그래서 며칠 지난 리뷰는 카드가 DOM 에 아예 없어 매칭이 불가능했다.

계약: 카드를 못 찾으면 스크롤 + '더보기'를 눌러 목록을 넓혀가며 다시 찾는다.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crawler import review_reply as rr  # noqa: E402


class FakeLocator:
    def __init__(self, n):
        self._n = n

    def count(self):
        return self._n


class FakePage:
    """'더보기'를 눌러야만 카드가 늘어나는 배민 목록 흉내."""

    def __init__(self, need_rounds):
        self.need = need_rounds      # 몇 번 더보기를 눌러야 대상이 나오는가
        self.clicks = 0
        self.scrolls = 0
        self.cards = 10

    def goto(self, *a, **k):
        pass

    def locator(self, sel):
        return FakeLocator(self.cards)

    def evaluate(self, js, *a):
        if "scrollTo" in js:
            self.scrolls += 1
            return None
        # 더보기 클릭 — 누를 때마다 한 묶음(10건)이 붙는다.
        if self.clicks >= self.need + 3:
            return False              # 더 나올 게 없음
        self.clicks += 1
        self.cards += 10
        return True


def _run(monkeypatch, need_rounds, found_after):
    """need_rounds 번 더보기 후에 카드가 발견되는 상황을 만든다."""
    page = FakePage(need_rounds)
    act = rr.ReplyToReviewAction(
        {"platform": "baemin", "review_no": "2026080802903778",
         "author": "여왕쥐", "content": ""}, reply_text="답글")

    calls = {"n": 0}

    def fake_find(self, p):
        calls["n"] += 1
        return "CARD" if calls["n"] > found_after else None

    monkeypatch.setattr(rr.ReplyToReviewAction, "_find_baemin_card", fake_find)
    monkeypatch.setattr(rr, "is_session_expired", lambda p: False)
    monkeypatch.setattr(rr, "human_pause", lambda *a, **k: None)
    return page, act


def test_clicks_more_button_to_reach_old_review(monkeypatch):
    """첫 화면에 없던 리뷰도 '더보기'를 눌러 찾아낸다."""
    page, act = _run(monkeypatch, need_rounds=3, found_after=3)
    # 카드를 찾은 뒤엔 게시 단계로 넘어가므로, 거기서 멈추게 한다.
    monkeypatch.setattr(rr.ReplyToReviewAction, "_baemin_open_and_submit",
                        lambda self, page, card, reply: {"ok": True},
                        raising=False)
    try:
        act._apply_baemin(page, "답글")
    except Exception:
        pass                        # 이후 DOM 조작은 이 테스트 범위 밖
    assert page.clicks >= 3, "더보기를 눌러 목록을 넓혀야 한다"
    assert page.scrolls >= 3


def test_gives_up_with_helpful_message(monkeypatch):
    """끝까지 못 찾으면 무엇이 문제인지 알 수 있게 알린다."""
    page, act = _run(monkeypatch, need_rounds=1, found_after=10_000)
    try:
        act._apply_baemin(page, "답글")
        raise AssertionError("에러가 나야 한다")
    except rr.ReplyPostError as e:
        msg = str(e)
        assert "2026080802903778" in msg      # 어떤 리뷰인지
        assert "훑어본 카드" in msg            # 얼마나 찾아봤는지


def test_more_click_failure_is_not_fatal(monkeypatch):
    """'더보기' 평가가 실패해도 예외로 터지지 않는다."""

    class Boom:
        def evaluate(self, *a, **k):
            raise RuntimeError("페이지 접근 실패")

    assert rr._click_baemin_more(Boom()) is False


# ---------------------------------------------------------------------------
# 리뷰번호 매칭은 공백에 흔들리면 안 된다 (2026-08-16 반복 실패 원인 후보)
# ---------------------------------------------------------------------------

class _Card:
    def __init__(self, text):
        self._t = text

    def inner_text(self):
        return self._t


class _Cards:
    def __init__(self, cards):
        self._c = cards

    def count(self):
        return len(self._c)

    def nth(self, i):
        return self._c[i]

    def filter(self, has_text=None):
        hit = [c for c in self._c if has_text and has_text in c.inner_text()]
        return _Cards(hit)

    @property
    def first(self):
        return self._c[0]


class _Page:
    def __init__(self, cards):
        self._cards = _Cards(cards)

    def locator(self, sel):
        return self._cards


def _find(cards, review):
    act = rr.ReplyToReviewAction(review, reply_text="x")
    return act._find_baemin_card(_Page(cards))


RID = "2026080802903778"


def test_matches_number_split_across_lines():
    """'리뷰번호'와 숫자가 줄바꿈으로 갈려도 찾아야 한다."""
    card = _Card(f"여왕쥐\n★★★★★\n리뷰번호\n{RID}\n사장님 댓글 등록하기")
    assert _find([card], {"platform": "baemin", "review_no": RID,
                          "author": "여왕쥐", "content": ""}) is card


def test_matches_number_with_nbsp_and_double_space():
    card = _Card(f"리뷰번호  {RID}")
    assert _find([card], {"platform": "baemin", "review_no": RID,
                          "author": "여왕쥐", "content": ""}) is card


def test_does_not_match_other_review():
    other = _Card("리뷰번호 2026070100000001")
    assert _find([other], {"platform": "baemin", "review_no": RID,
                           "author": "여왕쥐", "content": ""}) is None


def test_seen_numbers_helper_is_safe_on_error():
    class Boom:
        def evaluate(self, *a, **k):
            raise RuntimeError("page gone")

    assert rr._baemin_seen_review_nos(Boom()) == []
