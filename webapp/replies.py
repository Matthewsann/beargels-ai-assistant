"""리뷰 답글 생성 — 웹앱용 얇은 래퍼.

실제 생성 로직은 assistant/beargels.py 의 generate_review_reply 하나만 쓴다
(텔레그램 봇·크롤러와 완전히 같은 답글 품질·톤). 여기서는 웹 폼에서 온 값을
크롤러가 만드는 review dict 모양으로 바꿔주는 일만 한다.

⚠️ 이 화면은 '초안 생성'만 한다. 실제 게시는 하지 않는다(사장님이 복사해서
   배민/쿠팡에 직접 붙여넣기). 게시 자동화는 crawler/review_reply.py 담당.
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PLATFORMS = [
    ("baemin", "배민", 500),
    ("coupang", "쿠팡이츠", 300),
]


def platform_label(code: str) -> str:
    for c, label, _ in PLATFORMS:
        if c == code:
            return label
    return code


def build_review(form) -> dict:
    """웹 폼(request.form) → 크롤러 review dict 모양으로 변환."""
    def _int(name):
        try:
            return int((form.get(name) or "").strip())
        except (TypeError, ValueError):
            return None

    menus = [m.strip() for m in (form.get("menus") or "").replace("/", ",").split(",")
             if m.strip()]
    return {
        "platform": form.get("platform") or "baemin",
        "review_no": None,
        "author": (form.get("author") or "").strip(),
        "rating": _int("rating"),
        "content": (form.get("content") or "").strip(),
        "menus": menus,
        "order_count": _int("order_count"),
        "reply_status": None,
    }


def generate(review: dict) -> dict:
    """답글 초안 1건을 만든다.

    Returns: {text, kind, escalate, length, max_len, platform_label}
      - kind: escalate | complaint | question | praise_detail | photo_only | neutral
      - escalate=True 면 답글 대신 '직접 대응 필요' 안내가 text 에 담긴다.
    """
    from assistant.beargels import (
        PLATFORM_REPLY, classify_review, generate_review_reply,
    )

    kind = classify_review(review)
    text = generate_review_reply(review)
    cfg = PLATFORM_REPLY.get(review.get("platform"), {"max_len": 300})
    return {
        "text": text,
        "kind": kind,
        "escalate": kind == "escalate",
        "length": len(text or ""),
        "max_len": cfg["max_len"],
        "platform_label": platform_label(review.get("platform")),
    }


KIND_LABEL = {
    "escalate": "🚨 민감 리뷰 (직접 대응)",
    "complaint": "🔸 불만 리뷰",
    "question": "❓ 질문 리뷰",
    "praise_detail": "💚 칭찬 리뷰",
    "photo_only": "📷 사진만",
    "neutral": "🙂 짧은 리뷰",
}
