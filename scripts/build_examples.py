# -*- coding: utf-8 -*-
"""사장님이 실제로 쓴 답글 → 유형별 '예시 창고' 파일로 굽는다.

왜 필요한가:
    무료·저가 AI 는 추상적인 규칙("문어체 쓰지 마라")을 잘 못 지킨다. 대신
    **실제 예시를 보여주면 놀랄 만큼 잘 따라한다.** 사장님 답글이 1,000건
    넘게 쌓여 있으므로, 이걸 유형별로 정리해 두었다가 답글을 만들 때
    비슷한 예시 몇 개를 프롬프트에 함께 넣는다(사장님 제안 2026-08-18).

    → 비싼 모델을 안 써도 사장님 문체가 나온다. API 비용은 그대로 무료.

만드는 것: reference/reply_examples_by_kind.json
    {유형: [{rating, content, menus, order_count, reply}, ...], ...}

실행: python scripts/build_examples.py   (재료가 늘면 다시 돌리면 된다)
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from assistant.beargels import (  # noqa: E402
    _REPLY_BANNED, classify_review, order_count_of,
)
from database import supabase_client as db  # noqa: E402

OUT = ROOT / "reference" / "reply_examples_by_kind.json"
OUT_REF = ROOT / "reference" / "reply_examples_reference.json"
# 유형당 이만큼만 보관한다 — 너무 많으면 파일만 커지고 고르는 데 도움이 안 된다.
PER_KIND = 60
# 참고용(말투 불일치) 예시는 유형당 이만큼만 — 많이 두면 말투가 오염된다.
REF_PER_KIND = 12
MIN_LEN, MAX_LEN = 40, 400


def _clean(reply: str) -> str:
    return " ".join((reply or "").split())


# 지금 말투 규칙에 어긋나는 옛 답글은 예시로 쓰면 안 된다 — 약한 모델은
# 예시를 그대로 베끼므로, 격식체 예시를 주면 격식체 답글이 나온다
# (실제로 '감사드립니다·준비하겠습니다' 예시가 뽑혔다, 2026-08-18).
# 불만·민감 답글만 정중한 격식체가 규칙이라 예외.
_FORMAL_ENDINGS = ("습니다", "입니다", "드립니다", "됩니다")
# 금지 표현의 '변형'까지 막는다(정확히 일치하지 않아 걸러지지 않던 것들).
_SOFT_BANNED = ("보답", "정성을 다해", "정성으로 준비", "큰 힘을",
                "찾아뵙겠", "맞이하겠", "바라며", "기원합니다")


def _tone_ok(reply: str, kind: str) -> bool:
    if kind in ("complaint", "escalate"):
        return True                     # 이 유형은 격식체가 규칙
    if any(e in reply for e in _FORMAL_ENDINGS):
        return False
    return not any(b in reply for b in _SOFT_BANNED)


def _fetch_all_replies(page_size=1000, max_rows=5000):
    """플랫폼에 실제 등록된 사장님 답글을 **전부** 가져온다.

    ⚠️ 한 번에 아무리 크게 요청해도 서버가 1,000건에서 끊는다 — limit(2000)
       으로 적어 두고 다 읽는 줄 알았지만 실제로는 최신 1,000건만 보고 있었다
       (2026-08-23 실측: 실답글 1,592건 중 592건은 아예 후보에도 못 들었다).
       그래서 범위를 옮겨 가며 여러 번 읽는다.
    """
    out, offset = [], 0
    while offset < max_rows:
        chunk = (db.get_client().table("reviews")
                 .select("platform,rating,content,menus,platform_reply,raw,"
                         "written_date")
                 .not_.is_("platform_reply", "null")
                 .order("written_date", desc=True)
                 .range(offset, offset + page_size - 1).execute().data)
        out += chunk
        if len(chunk) < page_size:
            break
        offset += page_size
    return out


def main() -> int:
    rows = _fetch_all_replies()
    print(f"사장님 답글 후보 {len(rows)}건")

    banks: dict[str, list] = {}
    # 지금 말투 규칙엔 안 맞지만(옛 격식체) 금지 표현은 없는 답글 —
    # 예시가 거의 없는 유형(질문·민감 등)에서 '내용은 어떻게 짚었는지'만
    # 참고하도록 따로 보관한다. 말투를 베끼면 안 되므로 창고를 분리한다.
    refs: dict[str, list] = {}
    skipped_banned = skipped_tone = 0
    for r in rows:
        reply = _clean(r.get("platform_reply"))
        if not (MIN_LEN <= len(reply) <= MAX_LEN):
            continue
        # 지금 규칙에 어긋나는 옛 답글은 예시로 쓰지 않는다 — 나쁜 걸 배운다.
        if any(b in reply for b in _REPLY_BANNED):
            skipped_banned += 1
            continue
        kind = classify_review(r)
        if not _tone_ok(reply, kind):
            skipped_tone += 1
            ref = refs.setdefault(kind, [])
            if len(ref) < REF_PER_KIND:
                ref.append({
                    "rating": r.get("rating"),
                    "content": (r.get("content") or "").strip()[:120],
                    "menus": (r.get("menus") or [])[:3],
                    "order_count": order_count_of(r),
                    "reply": reply,
                })
            continue
        bank = banks.setdefault(kind, [])
        if len(bank) >= PER_KIND:
            continue
        bank.append({
            "rating": r.get("rating"),
            "content": (r.get("content") or "").strip()[:120],
            "menus": (r.get("menus") or [])[:3],
            "order_count": order_count_of(r),
            "reply": reply,
        })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(banks, ensure_ascii=False, indent=1), encoding="utf-8")
    OUT_REF.write_text(json.dumps(refs, ensure_ascii=False, indent=1),
                       encoding="utf-8")
    print(f"금지어 제외 {skipped_banned}건 · 옛 말투(격식체 등) 제외 {skipped_tone}건")
    for k, v in sorted(banks.items(), key=lambda x: -len(x[1])):
        print(f"  {k}: {len(v)}건")
    print("저장:", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
