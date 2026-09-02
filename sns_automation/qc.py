"""출하 전 검수(QC) — 릴스가 나가기 전에 마지막으로 본다.

공장 설계 4단계. 검사는 두 겹이다:

  ① 결정적 검사(무료·코드): 허위 표현·금지어·격식체·길이 — 채점표가
     명확한 것은 AI 를 부르지 않는다. 릴스-출하기준.md 의 치명 불량 목록.
  ② AI 검수(유료 소액): 완성본에서 프레임을 다시 뽑아 "첫 프레임이 훅으로
     충분한가, 자막이 그 화면과 맞는가"를 본다 — 코드가 못 보는 것.

치명 불량이면 말(자막·훅)을 한 번 고쳐 재렌더한다. 두 번은 안 한다 —
그래도 안 되면 경고와 함께 사람(사장님)에게 넘긴다. 공장은 무한 재작업을
하지 않는다.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

#: 치명 불량 — 릴스-출하기준.md 의 목록과 반드시 일치시킬 것
FALSE_CLAIMS = ("갓 구운", "갓구운", "수제 베이글", "수제베이글",
                "직접 반죽", "오늘 구운", "매일 굽")
BANNED = ("역대급", "미쳤", "무조건", "반드시", "인생맛집", "가성비 끝판왕",
          "지금 아니면", "품절 대란", "줄 서서 먹는", "대박", "혜자")
#: 격식체 어미 — "~니다/~십시오"로 끝나면 화면 자막으로는 불량(해요체만)
_FORMAL_END = re.compile(r"(니다|십시오)[.!?~♥🤍✨\s]*$")


def _texts_of(plan: dict, caption: str = "") -> list[tuple[str, str]]:
    out = [("훅", (plan.get("hook") or {}).get("text", "")),
           ("CTA", (plan.get("cta") or {}).get("text", ""))]
    for i, s in enumerate(plan.get("shots") or [], 1):
        if s.get("caption"):
            out.append((f"샷{i} 자막", s["caption"]))
    if caption:
        out.append(("캡션", caption))
    return out


def deterministic_issues(plan: dict, caption: str = "") -> list[str]:
    """코드로 확정 판정 가능한 치명 불량. 비어 있으면 통과."""
    issues: list[str] = []
    for where, text in _texts_of(plan, caption):
        for w in FALSE_CLAIMS:
            if w in text:
                issues.append(f"{where}에 허위 표현 '{w}' — 베이글은 굽지 않음(그릴 토스팅)")
        for w in BANNED:
            if w in text:
                issues.append(f"{where}에 금지어 '{w}'")
        if where != "캡션" and _FORMAL_END.search(text.strip()):
            issues.append(f"{where}가 격식체로 끝남('{text.strip()[-8:]}') — 해요체로")
    shots = plan.get("shots") or []
    if shots:
        total = sum(s.get("dur", 0) for s in shots) - 0.25 * (len(shots) - 1)
        if not 10.0 <= total <= 24.0:
            issues.append(f"길이 {total:.1f}초 — 합격선(12~20초) 밖")
        roles = [s.get("role") for s in shots]
        if "페이오프" not in roles:
            issues.append("페이오프 샷 없음 — 릴스의 심장이 빠짐")
    return issues


_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"},
                   "description": "발견한 문제 (없으면 빈 배열)"},
        "fix_words": {"type": "boolean",
                      "description": "자막·훅을 고치면 해결되는 문제인가"},
    },
    "required": ["passed", "issues", "fix_words"],
    "additionalProperties": False,
}


def _shipping_rules() -> str:
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "knowledge", "릴스-출하기준.md")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()[:2500]
    except OSError:
        return ""


async def ai_review(video_path: str, plan: dict, memo: str = "") -> dict:
    """완성본 프레임을 다시 보고 출하기준으로 채점. 실패 시 예외."""
    from . import planner, video_editor

    dur = video_editor.probe_seconds(video_path)
    frames = video_editor.sample_frames(video_path, dur, every=2.0,
                                        max_frames=8, px=220)
    if not frames:
        raise RuntimeError("완성본에서 프레임을 뽑지 못했습니다.")
    shots_txt = "\n".join(
        f"샷{i} {s['role']} {s['dur']}초 자막={s.get('caption') or '(없음)'}"
        for i, s in enumerate(plan.get("shots") or [], 1))
    system = (
        "너는 릴스 출하 검수원이다. 완성본에서 뽑은 프레임(시간순)을 보고\n"
        "아래 출하 기준으로만 채점한다. 트집이 아니라 **출하 가능 여부** 판단이다.\n"
        "특히: ①첫 프레임이 제품 히어로인가 ②자막이 그 화면과 실제로 맞는가\n"
        "③비슷한 장면 반복은 없는가. 사소한 취향 문제는 issues 에 넣지 않는다.\n"
        "화면 규칙(오탐 방지): **훅 문구는 첫 샷 위에, CTA 는 마지막 샷 위에\n"
        "원래 뜬다** — 구성표에 그 샷 자막이 null 이어도 정상이니 불일치로 잡지\n"
        "말 것. 하단 자막은 샷 구간 안에서만 뜬다.\n\n"
        f"[출하 기준]\n{_shipping_rules()}"
    )
    user = (f"[구성표]\n{shots_txt}\n"
            + (f"\n[사장님 메모]\n{memo[:600]}\n" if memo else "")
            + f"\n프레임 {len(frames)}장은 완성본을 2초 간격으로 뽑은 것이다. 채점하라.")
    return await planner._ask(system, user, _REVIEW_SCHEMA,
                              images=[("image/jpeg", b) for _t, b in frames])


def run_qc(plan: dict, video_path: str, caption: str, memo: str = "") -> dict:
    """검수 1회분: 결정적 검사 + AI 검수. 렌더·수정은 호출부(auto_make)가 한다.

    반환: {passed, critical(치명·재작업 필요), warnings, fix_words}
    """
    import asyncio

    critical = deterministic_issues(plan, caption)
    warnings: list[str] = []
    fix_words = bool(critical)          # 결정적 불량은 전부 말 문제라 재작성으로 풀림
    if not critical:
        try:
            r = asyncio.run(ai_review(video_path, plan, memo))
            if r.get("passed"):
                warnings = [str(x) for x in r.get("issues") or []]
            else:
                issues = [str(x) for x in r.get("issues") or ["출하 기준 미달"]]
                if r.get("fix_words"):
                    critical, fix_words = issues, True
                else:
                    # 말로 못 고치는 문제(장면 자체) — 재작업 없이 경고 출하,
                    # 사람이 파이프라인에서 판단한다(공장은 무한 재작업 금지)
                    warnings = issues
        except Exception as e:
            logger.warning("AI 검수 실패(결정적 검사만으로 진행): %s", e)
            warnings = [f"AI 검수를 못 돌림: {e}"]
    return {"passed": not critical, "critical": critical,
            "warnings": warnings, "fix_words": fix_words}
