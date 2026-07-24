"""릴스 템플릿 설정 (자동 편집기용).

knowledge/reel_templates.md 의 사람이 읽는 설계를 기계용으로 옮긴 것.
편집기는 이 설정으로 길이·페이스·음악 무드 등을 결정한다.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Template:
    id: str
    name: str
    target_seconds: int  # 목표 길이(초). 원본이 짧으면 그대로.
    pacing: str          # fast | medium | slow (클립당 노출 시간 힌트)
    music_mood: str      # 배경음악 무드(사람이 고를 때 참고)
    default_cta: str     # 아웃트로 CTA 문구


TEMPLATES: dict[str, Template] = {
    "T1": Template("T1", "제품 클로즈업", 20, "fast", "맛있는/잔잔", "베어글스 송도 · 저장해두세요"),
    "T2": Template("T2", "신메뉴 소개", 30, "medium", "트렌디/경쾌", "이번 주 한정 · 송도점에서"),
    "T3": Template("T3", "매장 분위기", 40, "slow", "감성 로파이", "송도 카페 · 위치는 프로필"),
    "T4": Template("T4", "정보·팁", 35, "medium", "밝고 가벼움", "저장해두고 따라 해보세요"),
    "T5": Template("T5", "비하인드·스토리", 45, "slow", "따뜻/잔잔", "베어글스 송도 이야기 · 팔로우"),
}

DEFAULT_TEMPLATE_ID = "T1"


def get_template(template_id: str | None) -> Template:
    return TEMPLATES.get(template_id or DEFAULT_TEMPLATE_ID, TEMPLATES[DEFAULT_TEMPLATE_ID])


def template_list_text() -> str:
    lines = ["릴스 템플릿:"]
    for t in TEMPLATES.values():
        lines.append(f"• {t.id} {t.name} (~{t.target_seconds}초, {t.pacing})")
    return "\n".join(lines)
