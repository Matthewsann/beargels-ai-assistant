"""
[미디어 4] 곰 캐릭터 일러스트 자동 생성 (AI).

베어글스 곰 캐릭터를 '고정된 스타일 설명'으로 생성해 media/character/ 에 저장합니다.
포즈 세트(인사/베이글/커피/오븐/계절)를 한 번에 뽑아, 마음에 드는 컷을 골라
썸네일·포스터·블로그에 반복 사용하세요.

솔직한 한계: 생성형 AI는 매번 캐릭터가 조금씩 달라집니다. 그래서 이 스크립트는
  1) 스타일 문구를 고정해 최대한 비슷하게 뽑고
  2) 여러 장 생성 → 베스트를 골라 '공식 캐릭터'로 재사용
하는 방식입니다. (한 번 세트를 확정하면 다시 뽑을 필요가 없습니다)

필요: .env 에 OPENAI_API_KEY + `pip install openai`
키가 없으면 → 프롬프트 파일만 저장됩니다. 이 프롬프트를 무료 이미지 생성 서비스
(빙 이미지 크리에이터 등)에 붙여넣어 직접 뽑아도 결과는 같습니다.

    python src/make_character.py            # 기본 5포즈 생성
    python src/make_character.py 인사 커피   # 지정 포즈만
"""

from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "media" / "character"

# 캐릭터 일관성의 핵심 — 이 스타일 문구를 절대 바꾸지 말 것
STYLE = (
    "귀여운 갈색 아기곰 마스코트 캐릭터 '베어글스', 둥근 얼굴과 통통한 몸, "
    "크림색 주둥이, 작고 반짝이는 눈, 플랫 파스텔 일러스트, 두껍고 부드러운 외곽선, "
    "미니멀한 크림색 배경, 스티커 스타일, 벡터 느낌, 텍스트 없음"
)

POSES = {
    "인사": "한 손을 들어 반갑게 인사하는 포즈",
    "베이글": "커다란 베이글을 두 손으로 안고 행복해하는 포즈",
    "커피": "김이 나는 커피잔을 들고 미소짓는 포즈",
    "오븐": "빵집 앞치마를 두르고 갓 구운 빵 트레이를 든 포즈",
    "계절": "계절 소품(예: 밀짚모자)을 쓰고 앉아있는 포즈",
}


def build_prompt(pose_desc: str) -> str:
    return f"{STYLE}. 포즈: {pose_desc}."


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    names = [a for a in sys.argv[1:] if a in POSES] or list(POSES)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    prompts = {n: build_prompt(POSES[n]) for n in names}
    prompt_file = OUT_DIR / "캐릭터_프롬프트.txt"
    prompt_file.write_text(
        "\n\n".join(f"[{n}]\n{p}" for n, p in prompts.items()), encoding="utf-8")

    if not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY 가 없어 프롬프트만 저장했습니다:")
        print(f"  → {prompt_file.relative_to(ROOT)}")
        print("  이 프롬프트를 빙 이미지 크리에이터 등에 붙여넣어 생성하세요. (스타일 문구 유지가 핵심)")
        return

    try:
        import base64
        from openai import OpenAI
    except ImportError:
        sys.exit("openai 패키지가 없습니다:  pip install openai")

    client = OpenAI()
    print(f"곰 캐릭터 {len(names)}포즈 생성 시작…")
    ok = 0
    for n in names:
        try:
            res = client.images.generate(model="gpt-image-1", prompt=prompts[n], size="1024x1024")
            out = OUT_DIR / f"곰돌이_{n}.png"
            out.write_bytes(base64.b64decode(res.data[0].b64_json))
            ok += 1
            print(f"  ✓ {out.name}")
        except Exception as e:
            print(f"  ✗ {n}: {e}")

    print(f"\n완료: {ok}/{len(names)}장 → media/character/")
    print("마음에 드는 컷을 '공식 캐릭터'로 정하고, 썸네일·포스터에 반복 사용하세요.")
    print("(마음에 안 들면 같은 명령을 다시 실행 — 스타일은 고정, 그림만 새로 뽑힘)")


if __name__ == "__main__":
    main()
