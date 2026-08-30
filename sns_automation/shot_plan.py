"""샷 구성표 — 기획안이자 편집 명세.

한 장의 문서가 두 곳에서 쓰인다.

    촬영 전 : "이 샷들을 찍으세요" 체크리스트
    편집 때 : 편집기가 그대로 실행하는 명세

이게 없으면 클립을 받은 순서대로 통째로 이어붙이는 수밖에 없어서,
**클립 안의 좋은 2초를 앞으로 끌어올 방법이 없다.**
(2026-08-16 실측: 히어로 샷인 귤 단면이 10초에야 등장 → 릴스로는 실격)

구조
    {
      "template": "T2",
      "hook":  {"text": "빵 사이에 귤이 통째로", "seconds": 2.4},
      "label": "송도 베어글스",              # 지역 노출 (방문 유도)
      "shots": [
        {"clip": "IMG_5946.MOV", "in": 10.0, "dur": 1.6,
         "caption": null, "role": "훅", "slow": 1.0, "audio": false},
        ...
      ],
      "cta":   {"text": "저장해두셨다가 놀러 오세요"}
    }

역할(role)은 사람이 읽는 라벨이자 촬영 체크리스트 항목이 된다.
"""

from __future__ import annotations

import json
import os

#: 릴스 한 편의 뼈대. 역할 → (기본 길이, 자막 방향)
#: 실제 클립이 부족하면 앞에서부터 채우고 나머지는 버린다.
ROLE_HOOK = "훅"
ROLE_PROCESS = "과정"
ROLE_DETAIL = "디테일"
ROLE_TENSION = "긴장"
ROLE_PAYOFF = "페이오프"
ROLE_CLOSE = "마무리"

#: 자르는 순간 → 단면 공개 순서를 지키는 기본 뼈대.
#: 사장님 지적(2026-08-17): "자르는 순간 다음 단면이 나와야 하지 않겠어?"
DEFAULT_SKELETON = [
    (ROLE_HOOK, 1.8, None),
    (ROLE_PROCESS, 3.2, "만드는 과정"),
    (ROLE_DETAIL, 2.8, "재료 디테일"),
    (ROLE_TENSION, 3.0, "자르는 순간"),
    (ROLE_PAYOFF, 2.8, "단면 공개"),
    (ROLE_CLOSE, 2.6, None),
]

#: 페이오프 샷은 살짝 느리게 — 클라이맥스를 눈에 남긴다.
PAYOFF_SLOW = 1.15
#: 원본 소리를 남길 역할. 나머지는 무음(발행 시 인기 음원을 얹으므로).
AUDIO_ROLES = (ROLE_TENSION,)


class ShotPlanError(ValueError):
    """구성표가 편집기에 넘길 수 없는 상태."""


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def normalize(plan: dict) -> dict:
    """느슨하게 들어온 구성표를 편집기가 쓸 수 있는 형태로 다듬는다.

    AI가 만들었든 사장님이 웹에서 고쳤든, 여기를 통과한 것만 편집기로 간다.
    """
    if not isinstance(plan, dict):
        raise ShotPlanError("구성표가 딕셔너리가 아닙니다.")

    hook = plan.get("hook") or {}
    if isinstance(hook, str):
        hook = {"text": hook}
    cta = plan.get("cta") or {}
    if isinstance(cta, str):
        cta = {"text": cta}

    shots = []
    for i, s in enumerate(plan.get("shots") or []):
        if not isinstance(s, dict) or not s.get("clip"):
            continue
        dur = _num(s.get("dur"), 0.0)
        if dur <= 0:
            # in/out 로 준 경우도 받아준다
            out = _num(s.get("out"), 0.0)
            dur = max(0.0, out - _num(s.get("in"), 0.0))
        if dur <= 0.3:
            continue  # 0.3초 미만은 편집에서 의미 없음
        role = (s.get("role") or "").strip() or ROLE_PROCESS
        slow = _num(s.get("slow"), 1.0) or 1.0
        shots.append({
            "clip": str(s["clip"]),
            "in": max(0.0, _num(s.get("in"), 0.0)),
            "dur": round(dur, 3),
            "caption": (s.get("caption") or "").strip() or None,
            "role": role,
            "slow": round(min(max(slow, 0.5), 2.0), 3),
            "audio": bool(s.get("audio", role in AUDIO_ROLES)),
        })

    if not shots:
        raise ShotPlanError("샷이 하나도 없습니다. 영상 구간을 지정해 주세요.")

    return {
        "template": plan.get("template") or "T1",
        "hook": {
            "text": (hook.get("text") or "").strip(),
            "seconds": round(_num(hook.get("seconds"), 2.4) or 2.4, 2),
        },
        "label": (plan.get("label") or "").strip(),
        "shots": shots,
        "cta": {"text": (cta.get("text") or "").strip()},
    }


def total_seconds(plan: dict, transition: float = 0.25) -> float:
    """크로스디졸브로 겹치는 만큼을 뺀 최종 길이(예상)."""
    shots = plan.get("shots") or []
    if not shots:
        return 0.0
    return round(sum(s["dur"] for s in shots) - transition * (len(shots) - 1), 2)


def from_clips(clips: list[dict], *, hook: str = "", menu: str = "",
               label: str = "", cta: str = "", template: str = "T1",
               skeleton=DEFAULT_SKELETON) -> dict:
    """클립 목록으로 기본 구성표를 만든다 (AI 없이 동작하는 폴백).

    clips: [{"name": 파일명, "duration": 초}] — 긴 클립일수록 뒤쪽 좋은 장면을
    담고 있을 확률이 높아, 훅/페이오프에 **긴 클립의 뒷부분**을 배정한다.
    """
    usable = [c for c in clips if _num(c.get("duration")) >= 1.0]
    if not usable:
        raise ShotPlanError("쓸 수 있는 영상이 없습니다(1초 이상 필요).")

    longest = max(usable, key=lambda c: _num(c.get("duration")))
    by_len = sorted(usable, key=lambda c: _num(c.get("duration")), reverse=True)

    shots = []
    for idx, (role, dur, cap) in enumerate(skeleton):
        if role in (ROLE_HOOK, ROLE_PAYOFF):
            # 가장 긴 클립의 뒷부분 = 보통 완성/공개 장면
            c = longest
            d = _num(c.get("duration"))
            start = max(0.0, d - dur - (0.0 if role == ROLE_HOOK else 1.4))
        else:
            c = by_len[idx % len(by_len)]
            d = _num(c.get("duration"))
            start = max(0.0, min(d - dur, d * 0.25))
        if d - start < dur:          # 클립이 짧으면 길이를 줄여 맞춘다
            dur = max(1.0, d - start)
        shots.append({
            "clip": c["name"], "in": round(start, 2), "dur": round(dur, 2),
            "caption": cap, "role": role,
            "slow": PAYOFF_SLOW if role == ROLE_PAYOFF else 1.0,
            "audio": role in AUDIO_ROLES,
        })

    return normalize({
        "template": template,
        "hook": {"text": hook, "seconds": 2.4},
        "label": label or menu,
        "shots": shots,
        "cta": {"text": cta},
    })


def checklist(plan: dict) -> list[str]:
    """구성표 → 촬영 체크리스트 문장. 촬영 전에 폰으로 보는 용도."""
    out = []
    for i, s in enumerate(plan.get("shots") or [], 1):
        cap = f" — 자막 「{s['caption']}」" if s.get("caption") else ""
        out.append(f"샷{i} · {s['role']} · {s['dur']:.1f}초{cap}")
    return out


def load(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return normalize(json.load(f))
    except (OSError, ValueError, ShotPlanError):
        return None


def save(plan: dict, path: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    return path
