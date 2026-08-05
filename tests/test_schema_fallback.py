"""schema_v5 미적용 폴백 회귀 테스트.

Supabase 에 ai_draft/kind/posted_at 컬럼이 아직 없어도(마이그레이션 전)
초안 저장(save_ai_draft)과 '답글 등록함'(mark_replied)이 죽지 않고
v5 컬럼만 빼고 저장해야 한다 — 안 그러면 새 리뷰 초안이 안 만들어지고
등록한 리뷰가 목록에서 안 내려간다(2026-08-06 실장애).
"""

import pytest

from database import supabase_client as db


class _FakeQuery:
    """update(...).eq(...).execute() 체인을 흉내내며 payload 를 기록한다."""

    def __init__(self, store, fail_codes):
        self._store = store
        self._fail_codes = fail_codes
        self._payload = None

    def table(self, _name):
        return self

    def update(self, payload):
        self._payload = payload
        return self

    def eq(self, _col, _val):
        return self

    def execute(self):
        if any(k in self._payload for k in db._V5_COLS) and self._fail_codes:
            err = Exception("column missing")
            err.code = self._fail_codes[0]
            raise err
        self._store.append(dict(self._payload))


@pytest.mark.parametrize("code", ["PGRST204", "42703"])
def test_mark_replied_survives_missing_column(monkeypatch, code):
    saved = []
    monkeypatch.setattr(db, "get_client",
                        lambda: _FakeQuery(saved, [code]))
    db.mark_replied(1)                      # 예외 없이 통과해야 한다
    assert saved and saved[0]["reply_status"] == "posted"
    assert "posted_at" not in saved[0]      # v5 컬럼은 빼고 저장


def test_save_ai_draft_survives_missing_column(monkeypatch):
    saved = []
    monkeypatch.setattr(db, "get_client",
                        lambda: _FakeQuery(saved, ["PGRST204"]))
    db.save_ai_draft(1, "초안", kind="neutral")
    assert saved and saved[0]["reply_draft"] == "초안"
    assert "ai_draft" not in saved[0] and "kind" not in saved[0]


def test_save_ai_draft_full_after_migration(monkeypatch):
    # 마이그레이션 후에는 v5 컬럼까지 전부 저장돼야 한다.
    saved = []
    monkeypatch.setattr(db, "get_client",
                        lambda: _FakeQuery(saved, []))
    db.save_ai_draft(1, "초안", kind="neutral")
    assert saved[0]["ai_draft"] == "초안" and saved[0]["kind"] == "neutral"


def test_other_errors_still_raise(monkeypatch):
    # 다른 에러(권한 등)는 삼키면 안 된다.
    saved = []
    monkeypatch.setattr(db, "get_client",
                        lambda: _FakeQuery(saved, ["42501"]))
    with pytest.raises(Exception):
        db.mark_replied(1)
