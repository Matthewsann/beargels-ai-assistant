"""시간차로 이미 답글이 달린 리뷰에 '등록'을 눌렀을 때의 동작 회귀 테스트.

상황: 웹 화면엔 미답변인데, 그 사이 앱이나 다른 경로에서 답글이 먼저 달렸다.
예전에는 신규 등록이 거절돼(쿠팡 50001 / 배민 '등록하기' 버튼 없음) 그냥
실패했다 — 직원이 쓴 최종본이 반영되지 않고 계속 재시도만 하게 된다.

지금 계약: **'수정'으로 자동 전환해 직원이 등록한 내용으로 맞춘다.**
그리고 덮어썼다는 사실을 replaced 로 알려 사장님께 보고한다.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crawler.review_reply import _coupang_already_replied, _coupang_ok  # noqa: E402


def test_detects_already_replied_by_code():
    assert _coupang_already_replied(50001, None)
    assert _coupang_already_replied("50001", {"message": "x"})


def test_detects_already_replied_by_message():
    assert _coupang_already_replied("ERROR", {"message": "이미 답글이 등록된 리뷰"})


def test_normal_failures_are_not_treated_as_already_replied():
    assert not _coupang_already_replied("SUCCESS", None)
    assert not _coupang_already_replied(40003, {"message": "권한이 없습니다"})
    assert not _coupang_already_replied(None, None)


def test_reject_code_without_error_field_is_not_success():
    # error 필드가 비어도 거절 코드면 '게시 완료'로 보면 안 된다.
    assert not _coupang_ok(200, 50001, None)
    assert _coupang_ok(200, "SUCCESS", None)
    assert _coupang_ok(200, None, None)
    assert not _coupang_ok(500, "SUCCESS", None)


def test_coupang_switches_to_modify_and_flags_replaced(monkeypatch):
    """신규 등록이 50001 로 거절되면 modify 로 재시도하고 replaced=True."""
    from crawler import review_reply as rr

    act = rr.ReplyToReviewAction({"platform": "coupang", "review_no": "77",
                                  "raw": '{"replies":[{"orderReviewReplyId":9}]}'},
                                 reply_text="최종 답글")
    calls = []

    def fake_fetch(self, page, url, payload):
        calls.append((url, dict(payload)))
        if url == rr.COUPANG_REPLY_API:
            return 200, 50001, {"message": "이미 답글이 있습니다"}
        return 200, "SUCCESS", None

    monkeypatch.setattr(rr.ReplyToReviewAction, "_coupang_fetch", fake_fetch)
    monkeypatch.setattr(rr, "is_session_expired", lambda page: False)
    monkeypatch.setattr(rr, "human_pause", lambda *a, **k: None)
    monkeypatch.setattr(rr, "STORE_ID", "1")

    class FakePage:
        def goto(self, *a, **k):
            pass

    res = act._apply_coupang(FakePage(), "최종 답글")

    assert res["replaced"] is True
    assert [c[0] for c in calls] == [rr.COUPANG_REPLY_API,
                                     rr.COUPANG_REPLY_MODIFY_API]
    # 수정 호출엔 대상 답글 id 가 실려야 한다.
    assert calls[1][1]["orderReviewReplyId"] == 9
    assert calls[1][1]["comment"] == "최종 답글"


def test_coupang_without_reply_id_explains_recollect(monkeypatch):
    """기존 답글 id 를 모르면(재수집 전) 덮어쓰지 않고 사유를 알린다."""
    from crawler import review_reply as rr

    act = rr.ReplyToReviewAction({"platform": "coupang", "review_no": "77",
                                  "raw": None}, reply_text="최종 답글")
    monkeypatch.setattr(rr.ReplyToReviewAction, "_coupang_fetch",
                        lambda self, page, url, payload: (200, 50001, None))
    monkeypatch.setattr(rr, "is_session_expired", lambda page: False)
    monkeypatch.setattr(rr, "human_pause", lambda *a, **k: None)
    monkeypatch.setattr(rr, "STORE_ID", "1")

    class FakePage:
        def goto(self, *a, **k):
            pass

    try:
        act._apply_coupang(FakePage(), "최종 답글")
        raise AssertionError("에러가 나야 한다")
    except rr.ReplyPostError as e:
        assert "이미 답글" in str(e) and "수집" in str(e)


# --- 배민: 이미 답글이 있으면 하나 더 붙이지 않는다 (2026-08-27) -----------
# 배민이 '사장님 댓글 추가하기'를 주기 시작하면서, 답글이 있어도 등록 버튼이
# 보인다. 예전 로직('버튼 없음 = 이미 답글 있음')이 무력화돼 손님 리뷰에
# 답글이 두 개 달렸다(재윤님 리뷰: 8/16·8/27).

def test_baemin_refuses_to_add_second_reply():
    import inspect
    from crawler import review_reply as rr
    src = inspect.getsource(rr.ReplyToReviewAction._apply_baemin)
    assert "ReviewCommentBox-module__" in src
    assert "답글이 두 개" in src, "중복 등록을 막는 안내가 있어야 한다"
    # allow_edit(=수정 등록) 일 때는 막지 않아야 한다
    assert "not self.allow_edit and existing.count()" in src
