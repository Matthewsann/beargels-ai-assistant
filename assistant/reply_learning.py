"""리뷰 답글 학습 루프 — 사장님 피드백을 계속 학습해 답글 품질을 고도화한다.

원리(내장 학습):
  1) 답글 생성 → 사장님이 수정/승인해 게시.
  2) 게시된(=승인된) 답글을 '좋은 예시'로 자동 저장(record_example).
  3) 다음 생성 때 ① 누적 규칙(reply_rules.md) + ② 승인된 실제 예시(few-shot)를
     프롬프트에 넣어, 쓸수록 사장님 취향에 가까워진다.
  4) 반복되는 지적은 규칙으로 승격(add_rule).

  5) [diff 학습] 게시 시 'AI 초안'과 '사장님 최종본'을 함께 저장해,
     사장님이 초안을 어떻게 고치는지(수정 패턴)를 다음 생성 프롬프트에 주입.

저장:
  reference/reply_rules.md              — 누적 규칙(사람이 읽고 편집 가능)
  reference/reply_examples_approved.jsonl — 승인된 (리뷰, 답글) 예시(한 줄=한 건)
  reference/reply_drafts.jsonl          — 리뷰별 최신 AI 초안(게시 시 diff 계산용)

⚠️ 말투 학습은 '승인된 예시'로만. 과거 크롤링 답글(딱딱한 톤)은 여기 넣지 않는다.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_REF = Path(__file__).resolve().parent.parent / "reference"
RULES_PATH = _REF / "reply_rules.md"
APPROVED_PATH = _REF / "reply_examples_approved.jsonl"
DRAFTS_PATH = _REF / "reply_drafts.jsonl"


# ---------------------------------------------------------------------------
# 규칙 (reply_rules.md)
# ---------------------------------------------------------------------------

def learned_rules():
    """누적 규칙 텍스트를 반환한다(없으면 빈 문자열)."""
    try:
        return RULES_PATH.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return ""


def add_rule(text):
    """새 규칙 한 줄을 reply_rules.md 에 추가한다(사장님 피드백 승격)."""
    text = (text or "").strip()
    if not text:
        return
    _REF.mkdir(exist_ok=True)
    line = text if text.startswith(("-", "*", "#")) else f"- {text}"
    with RULES_PATH.open("a", encoding="utf-8") as f:
        f.write("\n" + line + "\n")
    logger.info("규칙 추가: %s", text[:60])


# ---------------------------------------------------------------------------
# 승인된 예시 (few-shot 학습 데이터)
# ---------------------------------------------------------------------------

def record_example(review, reply, source="posted", draft=None):
    """승인된 (리뷰, 답글)을 학습 예시로 저장한다.

    Args:
        review: 리뷰 dict.
        reply: 사장님이 승인/게시한 최종 답글 텍스트.
        source: 'posted'(게시) | 'edited'(초안 수정) | 'seed' | 'crawled'.
        draft: 게시 전 AI 초안(있으면 함께 저장해 수정 diff 학습에 쓴다).
    """
    reply = (reply or "").strip()
    if not reply or reply.startswith("⚠️"):
        return
    _REF.mkdir(exist_ok=True)
    draft = (draft or "").strip() or None
    rec = {
        "platform": review.get("platform"),
        "review_no": review.get("review_no"),
        "rating": review.get("rating"),
        "content": review.get("content"),
        "menus": review.get("menus"),
        "order_count": review.get("order_count"),
        "reply": reply,
        "source": source,
        "draft": draft,
        "edited": bool(draft and draft != reply),
    }
    # 같은 리뷰 재게시면 최신으로 교체(중복 제거).
    rows = [r for r in _load_examples()
            if not (r.get("platform") == rec["platform"]
                    and str(r.get("review_no")) == str(rec["review_no"]))]
    rows.append(rec)
    with APPROVED_PATH.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    logger.info("학습 예시 저장(%s) — 총 %d건", source, len(rows))


def _load_examples():
    try:
        return [json.loads(ln) for ln in
                APPROVED_PATH.read_text(encoding="utf-8").splitlines()
                if ln.strip()]
    except Exception:  # noqa: BLE001
        return []


def learned_examples(review, k=3):
    """이 리뷰와 비슷한 승인 예시 k개를 골라 few-shot 용으로 반환한다.

    유사도(간단): 같은 플랫폼 +1, 별점 근접 +, 사진만/텍스트 유형 일치 +,
    메뉴 겹침 +. 최근 것 우선.
    """
    ex = _load_examples()
    if not ex:
        return []
    plat = review.get("platform")
    rating = review.get("rating")
    has_text = bool((review.get("content") or "").strip())
    menus = set(review.get("menus") or [])

    def score(e):
        s = 0.0
        if e.get("platform") == plat:
            s += 1.0
        if isinstance(e.get("rating"), (int, float)) and rating is not None:
            s += max(0, 2 - abs(e["rating"] - rating)) * 0.5
        if bool((e.get("content") or "").strip()) == has_text:
            s += 0.7
        if menus and set(e.get("menus") or []) & menus:
            s += 0.6
        return s

    ranked = sorted(ex, key=score, reverse=True)
    return ranked[:k]


# ---------------------------------------------------------------------------
# AI 초안 기록 (diff 학습용)
# ---------------------------------------------------------------------------

def _review_key(platform, review_no):
    return f"{platform}:{review_no}"


def record_draft(review, draft):
    """생성된 AI 초안을 리뷰별 최신본으로 저장한다(게시 시 diff 계산용)."""
    draft = (draft or "").strip()
    if not draft or draft.startswith("⚠️"):
        return
    _REF.mkdir(exist_ok=True)
    key = _review_key(review.get("platform"), review.get("review_no"))
    rows = {r["key"]: r for r in _load_jsonl(DRAFTS_PATH)}
    rows[key] = {"key": key, "draft": draft}
    with DRAFTS_PATH.open("w", encoding="utf-8") as f:
        for r in rows.values():
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def get_draft(review):
    """이 리뷰에 대해 기억해 둔 최신 AI 초안을 반환한다(없으면 None)."""
    key = _review_key(review.get("platform"), review.get("review_no"))
    for r in _load_jsonl(DRAFTS_PATH):
        if r.get("key") == key:
            return r.get("draft")
    return None


def _load_jsonl(path):
    try:
        return [json.loads(ln) for ln in
                path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except Exception:  # noqa: BLE001
        return []


def edits_block(k=3):
    """사장님이 초안을 고쳐 게시한 최근 사례(초안→최종)를 프롬프트 블록으로.

    수정 자체가 '지적'이다: 초안의 어떤 점이 마음에 안 들었고 어떻게 바꿨는지
    를 그대로 보여줘, 같은 실수를 반복하지 않게 한다. 최근 것 우선 k개.
    """
    edited = [e for e in _load_examples()
              if e.get("edited") and e.get("draft")]
    if not edited:
        return ""
    lines = ["[사장님 수정 패턴 — 초안이 이렇게 고쳐졌다. 처음부터 "
             "'수정 후'에 가깝게 써라]"]
    for e in edited[-k:]:
        lines.append(f"· 수정 전(AI 초안): {e['draft']}\n"
                     f"  수정 후(사장님 최종): {e['reply']}")
    return "\n".join(lines)


def examples_block(review, k=3):
    """few-shot 예시를 프롬프트용 텍스트 블록으로 만든다(없으면 '')."""
    ex = learned_examples(review, k=k)
    if not ex:
        return ""
    lines = ["[사장님이 승인한 실제 답글 예시 — 이 톤·방식을 따라라]"]
    for e in ex:
        rv = (e.get("content") or "(사진/무텍스트)").replace("\n", " ")[:50]
        lines.append(f"· 리뷰(★{e.get('rating')}): \"{rv}\"\n  답글: {e['reply']}")
    return "\n".join(lines)
