"""답글 생성 비용 계약 — 프롬프트 캐시 구조와 호출 횟수 상한.

왜 이 테스트가 있나(2026-08-27): Claude API 사용량이 급증했다. 뜯어보니
답글 1회 호출의 입력 11,639토큰 중 8,200이 리뷰가 바뀌어도 똑같은 내용
(참고 사실 + 교훈 노트 + 구성 지침)인데, 캐시가 걸리는 system 이 아니라
user 에 실려 매번 전액 과금되고 있었다. 게다가 한 건에 최대 5회까지 호출이
나갔다(초안 → 넓히기 → 다시쓰기 2 → 금지어 손질).

여기서 지키는 것:
  1) 리뷰마다 달라지는 내용은 system 에 **들어가지 않는다**(캐시가 깨진다).
  2) 늘 똑같은 지시문은 user 에 **남아 있지 않다**(매번 과금된다).
  3) 답글 1건당 호출은 MAX_REPLY_CALLS 를 넘지 않는다.
"""

import assistant.beargels as B
import llm


REVIEW_A = {"platform": "baemin", "review_no": "1", "author": "홍길동",
            "rating": 5, "content": "속이 꽉차있어요 맛있어요",
            "menus": ["[SET] 베이글 샌드위치"], "order_count": 2}
REVIEW_B = {"platform": "coupang", "review_no": "2", "author": "김철수",
            "rating": 5, "content": "플레인 베이글 담백하고 좋아요",
            "menus": ["플레인 베이글"], "order_count": 9}


def _capture(monkeypatch, review, reply="좋은 답글입니다. " * 40):
    """generate_review_reply 를 돌리되 AI 는 부르지 않고 프롬프트만 모은다."""
    seen = []

    def fake(system="", user="", max_tokens=1500, model=None, images=None):
        seen.append({"system": system, "user": user})
        return reply

    monkeypatch.setattr(llm, "complete", fake)
    B.generate_review_reply(review)
    return seen


def test_static_instructions_live_in_the_cached_system_block(monkeypatch):
    """늘 같은 지시문은 system 에 있고 user 에는 없어야 한다."""
    calls = _capture(monkeypatch, REVIEW_A)
    system, user = calls[0]["system"], calls[0]["user"]
    # 구성 흐름의 본문은 system 에만 — user 는 "[구성] 흐름대로" 라고만 가리킨다
    assert "① 이름을 부르고" in system
    assert "① 이름을 부르고" not in user
    lessons = B._reply_lessons()
    if lessons:                     # 교훈 노트가 가장 큰 덩어리(6,500자)
        head = lessons.strip().splitlines()[0]
        assert head in system, "교훈 노트는 캐시 블록에 있어야 한다"
        assert head not in user, "교훈 노트가 user 에 있으면 매번 과금된다"
    # 캐시가 걸리려면 llm.py 의 최소 길이를 넘어야 한다
    assert len(system) >= llm.CACHE_MIN_CHARS


def test_system_block_is_identical_across_reviews_and_calls(monkeypatch):
    """리뷰가 달라도, 손질 호출이어도 system 이 같아야 캐시가 산다."""
    a = _capture(monkeypatch, REVIEW_A, reply="짧다.")   # 손질 경로까지 유발
    b = _capture(monkeypatch, REVIEW_B, reply="짧다.")
    systems = {c["system"] for c in a + b}
    assert len(systems) == 1, "system 이 호출마다 달라지면 캐시가 매번 깨진다"
    # 리뷰 본문은 user 쪽에만
    assert "홍길동" in a[0]["user"] and "홍길동" not in a[0]["system"]


def test_reply_calls_stay_within_budget(monkeypatch):
    """짧고 문제 있는 초안이 나와도 호출은 상한을 넘지 않는다."""
    calls = _capture(monkeypatch, REVIEW_A, reply="속이 꽉차있어요")  # 짧고 따라 씀
    assert len(calls) <= B.MAX_REPLY_CALLS, (
        f"{len(calls)}회 호출 — 상한 {B.MAX_REPLY_CALLS}회를 넘었다")
    assert len(calls) >= 1


def test_budget_is_configurable():
    """품질을 더 태우고 싶을 때 .env 로 올릴 수 있어야 한다."""
    import inspect
    src = inspect.getsource(B)
    assert "BEARGELS_MAX_REPLY_CALLS" in src
    assert B.MAX_REPLY_CALLS >= 1
