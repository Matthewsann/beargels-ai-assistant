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


# ---------------------------------------------------------------------------
# 작성기 열기 — 클릭이 가로채이거나 입력창이 카드 밖에 열려도 찾아야 한다
# (사장님 제보 2026-08-16: '답글 입력창(textarea)이 나타나지 않았습니다')
# ---------------------------------------------------------------------------

class _Btn:
    def __init__(self, fail_normal_click=False):
        self.fail = fail_normal_click
        self.clicked_via = None

    def scroll_into_view_if_needed(self, timeout=None):
        pass

    def click(self, timeout=None):
        if self.fail:
            raise RuntimeError("element is covered by header")
        self.clicked_via = "click"

    def evaluate(self, js):
        self.clicked_via = "dom"


def test_click_falls_back_to_dom_when_intercepted():
    b = _Btn(fail_normal_click=True)
    rr._baemin_click(b, "'등록' 버튼")
    assert b.clicked_via == "dom", "가려지면 DOM click 으로 눌러야 한다"


def test_click_uses_normal_click_when_possible():
    b = _Btn()
    rr._baemin_click(b, "버튼")
    assert b.clicked_via == "click"


def test_click_failure_raises_readable_error():
    class Dead(_Btn):
        def evaluate(self, js):
            raise RuntimeError("detached")

    try:
        rr._baemin_click(Dead(fail_normal_click=True), "'등록' 버튼")
        raise AssertionError("에러가 나야 한다")
    except rr.ReplyPostError as e:
        assert "누르지 못했습니다" in str(e)


class _Sel:
    """셀렉터별 개수를 흉내내는 가짜 Locator."""

    def __init__(self, n=0):
        self._n = n

    def count(self):
        return self._n

    @property
    def first(self):
        return self


class _ScopeWith:
    """지정한 셀렉터에만 입력칸이 있는 가짜 범위."""

    def __init__(self, exists=True, sel_with=None):
        self._exists, self._sel = exists, sel_with

    def count(self):
        return 1 if self._exists else 0

    def locator(self, sel):
        return _Sel(1 if sel == self._sel else 0)


class _CardOutsideEditor:
    """카드 안엔 입력칸이 없고, 다음 형제(작성기)에 열리는 배민 실제 구조."""

    def __init__(self, sel_with="textarea"):
        self._sel = sel_with

    def count(self):
        return 1

    def locator(self, sel):
        if sel.startswith("xpath="):
            return _ScopeWith(True, self._sel)
        return _Sel(0)                       # 카드 안엔 없다


class _PageEmpty:
    def locator(self, sel):
        return _Sel(0)


def test_finds_textarea_outside_the_card():
    ed, kind = rr._baemin_find_editor(_PageEmpty(), _CardOutsideEditor(),
                                      timeout_ms=500)
    assert ed is not None and kind == "textarea"


def test_finds_contenteditable_editor():
    """입력칸이 textarea 가 아니어도 찾아야 한다(2026-08-16 반복 실패)."""
    card = _CardOutsideEditor(sel_with='[contenteditable="true"]')
    ed, kind = rr._baemin_find_editor(_PageEmpty(), card, timeout_ms=500)
    assert ed is not None and kind == '[contenteditable="true"]'


def test_returns_none_when_editor_never_opens():
    class _EmptyCard:
        def count(self):
            return 1

        def locator(self, sel):
            return _Sel(0) if not sel.startswith("xpath=") else _ScopeWith(False)

    ed, kind = rr._baemin_find_editor(_PageEmpty(), _EmptyCard(),
                                      timeout_ms=300)
    assert ed is None and kind is None


def test_editor_report_lists_all_candidates():
    class _P:
        def locator(self, sel):
            return _Sel(2 if sel == "textarea" else 0)

    rep = rr._baemin_editor_report(_P())
    assert "textarea=2개" in rep and "contenteditable" in rep




def test_fill_contenteditable_sets_text_and_fires_input():
    """contenteditable 은 fill 이 안 먹어 값 주입 + input 이벤트가 필요하다."""
    seen = {}

    class _Ed:
        def click(self):
            seen["clicked"] = True

        def evaluate(self, js, text=None):
            seen["js"] = js
            seen["text"] = text

        def inner_text(self):
            return seen.get("text", "")

    rr._baemin_fill_editor(_Ed(), '[contenteditable="true"]', "답글입니다")
    assert seen["clicked"] and seen["text"] == "답글입니다"
    assert "InputEvent" in seen["js"]      # 리액트 상태까지 갱신되게


def test_fill_rejects_mismatch():
    class _Ed:
        def fill(self, t):
            pass

        def input_value(self):
            return "다른 내용"

    try:
        rr._baemin_fill_editor(_Ed(), "textarea", "답글입니다")
        raise AssertionError("불일치면 막아야 한다")
    except rr.ReplyPostError as e:
        assert "불일치" in str(e)
