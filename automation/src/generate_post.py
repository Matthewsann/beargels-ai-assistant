"""
글 초안 자동 생성기.

content-plan.yaml 의 각 글에 대해 Claude 를 호출하여
제목/본문/태그가 담긴 초안(JSON + 사람이 읽는 md)을 posts/ 에 저장합니다.

이 스크립트는 네이버에 접속하지 않습니다. 순수하게 '글 만들기'만 담당합니다.
"""

from __future__ import annotations

import json
import pathlib
import re

import yaml
from anthropic import Anthropic

# ── 경로 ─────────────────────────────────────────────────────────────
ROOT = pathlib.Path(__file__).resolve().parent.parent      # automation/
POSTS_DIR = ROOT / "posts"

# 글 유형별 작성 지침. README/프롬프트 폴더의 5종과 1:1 대응됩니다.
TYPE_GUIDE = {
    "신메뉴": (
        "신메뉴/대표메뉴 소개 글. 흐름: 소개 계기 스토리 → 재료·과정을 감각적으로 "
        "(냄새·식감·온도) → 추천 먹는 법/어울리는 음료 → 방문 유도. 800~1200자."
    ),
    "이벤트": (
        "이벤트/프로모션 홍보 글. 혜택·기간·참여법이 한눈에 보이게. 서두르게 만드는 "
        "마무리. 과장 광고 문구 금지. 600~900자."
    ),
    "일상": (
        "일상/비하인드 에세이. 판매보다 사람 냄새로 단골과 소통. 마지막 2~3줄에서만 "
        "자연스럽게 매장으로 연결. 700~1000자."
    ),
    "후기": (
        "고객후기 재구성 글. 실제 손님 반응을 인용하듯 담백하게, 자화자찬 금지. "
        "'직접 드셔보시라'는 초대 마무리. 700~1000자."
    ),
    "정보성": (
        "정보성 글(베이글/빵 상식). 진짜 유용한 정보를 소제목·번호로 정리하고 구체적 "
        "숫자(온도·시간)를 포함. 마지막에 '베어글스에서는 이렇게 해요'로 연결. 900~1300자."
    ),
}


def build_prompt(brand: dict, post_type: str, variables: dict) -> str:
    """브랜드 정보 + 글 유형 + 이번 주 변수로 최종 프롬프트를 조립한다."""
    region = brand.get("region", "")
    guide = TYPE_GUIDE.get(post_type, TYPE_GUIDE["신메뉴"])
    var_lines = "\n".join(f"- {k}: {v}" for k, v in (variables or {}).items())

    footer = (
        f"📍 베어글스 {region}점 / {brand.get('address','')} / "
        f"🕒 {brand.get('hours','')} / ☎️ {brand.get('phone','')} / "
        f"📷 인스타그램 {brand.get('instagram','')}"
    )

    return f"""당신은 베어글스 베이커리 카페(Bear+Bagels, 곰 캐릭터 수제 베이글 카페)의
다정한 사장님이자 블로그 운영자입니다. 지역은 '{region}' 입니다.

[톤] 다정하고 따뜻하게, 과장 없이 솔직하게. 이모지는 🐻🥯☕️ 위주로 문단당 1~2개.
짧고 리듬감 있는 문장. "국내 최고" 같은 과장 광고 문구 금지.

[이번 글 유형] {post_type}
{guide}

[이번 글에 반영할 정보]
{var_lines or "(별도 변수 없음 — 유형에 맞게 자연스럽게 작성)"}

[검색 노출 규칙]
- 제목과 본문 첫 문단에 '{region}'과 핵심 키워드(메뉴/주제)를 자연스럽게 넣을 것.
- 본문 곳곳에 [📷 사진: 무엇을 찍으면 좋은지] 형식으로 사진 위치를 4~5군데 표시.
- 본문 맨 아래에 다음 정보 블록을 그대로 넣을 것:
{footer}
- 그리고 지역+메뉴+브랜드가 섞인 해시태그 6~10개.

[출력 형식] 반드시 아래 JSON 하나만 출력하세요. 다른 말·코드블록 표시 없이 순수 JSON.
{{
  "title": "검색이 잘 되는 제목 (25자 내외)",
  "body": "네이버 블로그에 그대로 붙여넣을 본문 전체. 사진 위치 표시와 정보 블록 포함. 줄바꿈은 \\n 으로.",
  "tags": ["태그1", "태그2", "..."]
}}"""


def _extract_json(text: str) -> dict:
    """모델이 혹시 코드블록/설명을 붙여도 JSON 부분만 안전하게 파싱한다."""
    text = text.strip()
    # ```json ... ``` 제거
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        if brace:
            text = brace.group(0)
    return json.loads(text)


def generate_one(client: Anthropic, cfg: dict, post_type: str, variables: dict) -> dict:
    gen = cfg.get("generate", {})
    msg = client.messages.create(
        model=gen.get("model", "claude-sonnet-5"),
        max_tokens=gen.get("max_tokens", 4000),
        messages=[{
            "role": "user",
            "content": build_prompt(cfg.get("brand", {}), post_type, variables),
        }],
    )
    raw = "".join(block.text for block in msg.content if block.type == "text")
    data = _extract_json(raw)
    data.setdefault("tags", [])
    return data


def save_post(index: int, post_type: str, data: dict) -> pathlib.Path:
    """생성 결과를 posts/ 에 JSON(자동화용) + MD(사람 확인용)로 저장."""
    POSTS_DIR.mkdir(exist_ok=True)
    stem = f"{index:02d}-{post_type}"

    (POSTS_DIR / f"{stem}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    tags = " ".join(f"#{t.lstrip('#')}" for t in data.get("tags", []))
    md = f"# {data.get('title','(제목 없음)')}\n\n{data.get('body','')}\n\n{tags}\n"
    md_path = POSTS_DIR / f"{stem}.md"
    md_path.write_text(md, encoding="utf-8")
    return md_path


def load_config() -> dict:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    cfg_path = ROOT / "config.yaml"
    if not cfg_path.exists():
        raise SystemExit("config.yaml 이 없습니다. config.example.yaml 을 복사해 만들어주세요.")
    return yaml.safe_load(cfg_path.read_text(encoding="utf-8"))


def main() -> None:
    cfg = load_config()
    plan_path = ROOT / "content-plan.yaml"
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    posts = plan.get("posts", [])
    if not posts:
        raise SystemExit("content-plan.yaml 에 posts 가 비어 있습니다.")

    client = Anthropic()  # ANTHROPIC_API_KEY 를 .env 에서 읽음
    print(f"총 {len(posts)}개 글 생성 시작…\n")
    for i, item in enumerate(posts, start=1):
        ptype = item.get("type", "신메뉴")
        print(f"[{i}/{len(posts)}] '{ptype}' 글 생성 중…")
        data = generate_one(client, cfg, ptype, item.get("vars", {}))
        path = save_post(i, ptype, data)
        print(f"    ✓ 저장: {path.name}  | 제목: {data.get('title','')}\n")
    print("완료! posts/ 폴더에서 초안을 확인하세요.")


if __name__ == "__main__":
    main()
