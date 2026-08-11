"""
[에이전트 ①기획 + ②선택] 주제안 생성 → 사람이 번호로 선택 → content-plan.yaml 확정.

기획 재료: data/trends.yaml(시장조사) + 계절 힌트 + 창고 기존 제목(중복회피) + 유형 배분 원칙.
선택이 끝나면 content-plan.yaml 을 쓰고, 이어서 본문작성+AI검수까지 바로 실행할 수 있습니다.

    python src/propose_topics.py            # 주제안 10개 → 대화형 선택
    python src/propose_topics.py --count 6  # 주제안 6개
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import pathlib

import yaml
from anthropic import Anthropic

import generate_post
import library

ROOT = pathlib.Path(__file__).resolve().parent.parent
TRENDS = ROOT / "data" / "trends.yaml"
PLAN = ROOT / "content-plan.yaml"

PROPOSE_PROMPT = """당신은 네이버 블로그 콘텐츠 전략가입니다.
베어글스 송도점(수제 베이글 카페, '베이글을 사랑하는 곰들의 크림치즈공장' 세계관)의
다음 발행 주제안 {count}개를 만들어주세요.

[시장조사 데이터 — 사람들이 실제로 검색하는 키워드]
{trends}

[유형 배분 원칙]
- 정보성(검색 유입)·신메뉴/이벤트(전환)·일상/후기(신뢰)를 골고루.
- 유형은 반드시 다음 중 하나: 신메뉴 / 이벤트 / 일상 / 후기 / 정보성

[중복 금지 — 이미 창고에 있거나 발행한 제목들]
{existing}

[출력 — 순수 JSON 배열만]
[
  {{
    "type": "정보성",
    "topic": "글 주제 한 줄",
    "title_draft": "제목 가안 (지역+키워드 포함, 25자 내외)",
    "main_keyword": "대표 키워드",
    "sub_keywords": ["세부1", "세부2"],
    "reason": "이 주제를 지금 쓰면 좋은 이유 한 줄 (트렌드/계절/전환 근거)"
  }}
]"""


def load_trends() -> str:
    lines = []
    if TRENDS.exists():
        t = yaml.safe_load(TRENDS.read_text(encoding="utf-8"))
        lines.append(f"수집일: {t.get('updated')}")
        for seed, sugs in (t.get("suggestions") or {}).items():
            if sugs:
                lines.append(f"- '{seed}' 자동완성: {', '.join(sugs[:10])}")
        if t.get("season_hints"):
            lines.append(f"- 계절 힌트: {', '.join(t['season_hints'])}")
    # 학습 루프(⑧피드백): 실제 성과 데이터를 기획 근거로 주입
    learned_file = ROOT / "data" / "keywords-learned.yaml"
    if learned_file.exists():
        learned = yaml.safe_load(learned_file.read_text(encoding="utf-8")) or {}
        if learned.get("keywords"):
            lines.append(f"- ★실제 유입 검색어(성과 검증됨, 우선 반영): {', '.join(learned['keywords'])}")
        if learned.get("top_posts"):
            lines.append(f"- ★조회수 상위 글(이런 유형이 통함): {' / '.join(learned['top_posts'])}")
    return "\n".join(lines) or "(수집된 트렌드 없음 — 계절과 상식으로 판단)"


def propose(client: Anthropic, cfg: dict, count: int) -> list[dict]:
    gen = cfg.get("generate", {})
    existing = library.existing_titles()
    prompt = PROPOSE_PROMPT.format(
        count=count,
        trends=load_trends(),
        existing="\n".join(f"- {t}" for t in existing[-40:]) or "(없음)",
    )
    msg = client.messages.create(
        model=gen.get("model", "claude-sonnet-5"),
        max_tokens=gen.get("max_tokens", 5000),
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(b.text for b in msg.content if b.type == "text").strip()
    arr = re.search(r"\[.*\]", raw, re.DOTALL)
    return json.loads(arr.group(0) if arr else raw)


def show(proposals: list[dict]) -> None:
    print("\n📋 이번 주 주제안")
    print("=" * 64)
    for i, p in enumerate(proposals, 1):
        print(f"[{i}] ({p.get('type','')}) {p.get('title_draft','')}")
        print(f"    키워드: {p.get('main_keyword','')} / {', '.join(p.get('sub_keywords', []))}")
        print(f"    근거: {p.get('reason','')}")
    print("=" * 64)


def write_plan(selected: list[dict]) -> None:
    posts = []
    for p in selected:
        posts.append({
            "type": p.get("type", "정보성"),
            "vars": {
                "주제": p.get("topic", ""),
                "제목가안": p.get("title_draft", ""),
                "대표키워드": p.get("main_keyword", ""),
                "세부키워드": ", ".join(p.get("sub_keywords", [])),
            },
        })
    if PLAN.exists():
        PLAN.rename(PLAN.with_suffix(".yaml.bak"))
    PLAN.write_text(
        "# 주간 기획 확정본 — propose_topics.py 가 생성 (사람 선택 반영)\n"
        + yaml.safe_dump({"posts": posts}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="기획: 주제안 생성 + 선택 → content-plan.yaml")
    ap.add_argument("--count", type=int, default=10, help="주제안 개수 (기본 10)")
    args = ap.parse_args()

    cfg = generate_post.load_config()
    client = Anthropic()

    print("[⓪→①] 트렌드 반영 주제안 생성 중…")
    proposals = propose(client, cfg, args.count)
    show(proposals)

    pick = input("\n[②선택] 이번 주에 쓸 번호 (쉼표, 예: 1,4,7): ").strip()
    idx = [int(x) for x in pick.replace(" ", "").split(",") if x.isdigit()]
    selected = [proposals[i - 1] for i in idx if 1 <= i <= len(proposals)]
    if not selected:
        sys.exit("선택된 주제가 없습니다.")

    write_plan(selected)
    print(f"\n✓ {len(selected)}개 확정 → content-plan.yaml")

    if input("이어서 본문작성+AI검수까지 실행할까요? (y/n): ").strip().lower() == "y":
        subprocess.run([sys.executable, str(ROOT / "src" / "generate_post.py"), "--plan"])
        print("\n다음: weekly_publish.bat(예약발행) 또는 naver_autodraft.py --all(임시저장)")


if __name__ == "__main__":
    main()
