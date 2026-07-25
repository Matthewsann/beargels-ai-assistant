"""기획실 엔진 — 금고(knowledge/)를 읽어 마케팅 전문가처럼 블로그 글을 기획.

- make_plan(hint): 주제·키워드·제목후보·구성·촬영리스트를 담은 기획안(dict) 반환
- make_draft(topic, post_type): 기획을 바탕으로 실제 초안을 생성해 라이브러리에 적재, id 반환

둘 다 Claude(Anthropic) API 를 쓴다 → ANTHROPIC_API_KEY 와 크레딧 필요.
크레딧이 없으면 예외가 나며, 호출부(app.py)가 사장님께 '충전 안내'로 바꿔 보여준다.
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "automation" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# 금고에서 제외할 파일(색인/인스타 전용 채널 문서 — 블로그 기획엔 불필요)
KNOWLEDGE_EXCLUDE = {"README.md", "beargels_songdo.md", "growth_strategy.md", "reel_templates.md"}
# 철학 문서를 앞쪽에 오게 하는 우선순위(있으면 먼저 붙임). 없으면 무시.
KNOWLEDGE_ORDER = [
    "브랜드철학.md", "브랜드아이덴티티.md", "비전.md", "핵심가치.md", "의사결정원칙.md",
    "고객철학.md", "톤앤보이스.md", "AI행동지침.md", "브랜드.md",
    "매장정보.md", "메뉴.md", "고객.md", "운영시스템.md", "플랫폼운영.md",
]


def load_knowledge() -> tuple[str, str]:
    """knowledge/ 금고의 모든 .md 를 읽는다(철학 우선 정렬). 파일을 추가하면 자동 반영."""
    kdir = ROOT / "knowledge"
    found = {p.name: p for p in kdir.rglob("*.md") if p.name not in KNOWLEDGE_EXCLUDE}
    ordered_names = [n for n in KNOWLEDGE_ORDER if n in found]
    ordered_names += sorted(n for n in found if n not in KNOWLEDGE_ORDER)
    parts = [f"### {n}\n{found[n].read_text(encoding='utf-8')}" for n in ordered_names]
    seo_path = ROOT / "네이버-SEO-지식.md"
    seo = seo_path.read_text(encoding="utf-8") if seo_path.exists() else ""
    return "\n\n".join(parts), seo


def _client_cfg():
    import generate_post  # automation/src — .env 로드 + config.yaml
    cfg = generate_post.load_config()
    from anthropic import Anthropic
    return Anthropic(), cfg, generate_post


PLAN_PROMPT = """너는 베어글스 송도점의 네이버 블로그 마케팅 전문가다.
아래 '정보 금고'와 'SEO 지식'을 근거로만 판단한다(지어내지 말 것).

===== 정보 금고 =====
{knowledge}
===== SEO 지식 =====
{seo}
=====================

요청: 이번 주에 올릴 네이버 블로그 글 1편을 기획하라. {hint}
- 리뷰 데이터·인기 세트·단골 비중 등 금고 근거를 반영.
- 담백한 말투, 과장 금지. 정보 밀도 중시.
- 비어있는 사실(가격·영업시간 등)은 지어내지 말고 제목/구성에서 자연스럽게 비워둘 것.

아래 JSON 하나만 순수 출력(설명·코드블록 금지):
{{
  "topic": "이번 글 주제 한 줄",
  "why": "왜 이 주제인지 금고 근거 한 줄",
  "post_type": "정보성|신메뉴|이벤트|일상|후기 중 하나",
  "main_keyword": "대표 키워드",
  "sub_keywords": ["세부1","세부2","세부3"],
  "titles": ["제목후보1(25자 내외)","제목후보2","제목후보3"],
  "outline": ["도입 흐름","소제목①","소제목②","소제목③","마무리 흐름"],
  "shotlist": ["📷 컷 설명","📷 컷 설명","🎬 영상 아이디어"]
}}"""


def make_plan(hint: str = "") -> dict:
    client, cfg, gp = _client_cfg()
    knowledge, seo = load_knowledge()
    prompt = PLAN_PROMPT.format(knowledge=knowledge, seo=seo, hint=hint or "")
    msg = client.messages.create(
        model=cfg.get("generate", {}).get("model", "claude-sonnet-5"),
        max_tokens=2500,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(b.text for b in msg.content if b.type == "text")
    data = gp._extract_json(raw)
    data.setdefault("sub_keywords", [])
    data.setdefault("titles", [])
    data.setdefault("outline", [])
    data.setdefault("shotlist", [])
    data.setdefault("post_type", "정보성")
    return data


DRAFT_PROMPT = """너는 베어글스 송도점의 네이버 블로그 마케팅 전문가다.
아래 '정보 금고'(철학·톤·메뉴·고객)와 'SEO 지식', 그리고 '확정 기획'을 바탕으로
네이버에 그대로 붙여넣을 블로그 글 본문을 완성하라.

===== 정보 금고 =====
{knowledge}
===== SEO 지식 =====
{seo}
=====================

[확정 기획]
- 주제: {topic}
- 글 유형: {post_type}
- 제목: {title}
- 대표 키워드: {main_keyword}
- 세부 키워드: {sub_keywords}

[작성 규칙]
- 금고의 '톤앤보이스'와 '브랜드 철학'을 반드시 지킨다(따뜻·담백, 과장 금지, 자연스러운 ~요체).
- 글자 수 1,500자 이상. 첫 문단에 대표 키워드 1회, 본문 전체 3~5회(도배 금지).
- 소제목 2~4개로 구조화. 1인칭 경험. 구체적 숫자(가격·시간·온도)는 금고에 있는 것만, 없으면 비워둔다.
- 사진 위치를 본문에 5~8군데 [📷 사진: 무엇을 어떻게]로 안내.
- 사실(메뉴명·주소 등)은 금고 표기를 그대로 쓴다. 지어내지 않는다. 없는 정보는 비운다.
- 맨 아래에 매장정보(주소·영업시간 등, 금고에 있는 것만) 블록을 넣는다.

[출력] 아래 JSON 하나만 순수 출력(코드블록/설명 금지):
{{
  "main_keyword": "{main_keyword}",
  "sub_keywords": {sub_keywords_json},
  "title": "{title}",
  "body": "네이버에 붙여넣을 본문 전체(사진 위치·정보 블록 포함, 줄바꿈 \\n)",
  "tags": ["태그10개내외"]
}}"""


def make_draft(topic: str, post_type: str = "정보성", title: str = "",
               main_keyword: str = "", sub_keywords: list[str] | None = None) -> int:
    """확정 기획 + 금고 전체를 근거로 초안을 생성해 라이브러리에 적재."""
    client, cfg, gp = _client_cfg()
    import library
    knowledge, seo = load_knowledge()
    subs = sub_keywords or []
    prompt = DRAFT_PROMPT.format(
        knowledge=knowledge, seo=seo, topic=topic, post_type=post_type,
        title=title or topic, main_keyword=main_keyword,
        sub_keywords=", ".join(subs), sub_keywords_json=json.dumps(subs, ensure_ascii=False),
    )
    msg = client.messages.create(
        model=cfg.get("generate", {}).get("model", "claude-sonnet-5"),
        max_tokens=cfg.get("generate", {}).get("max_tokens", 5000),
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(b.text for b in msg.content if b.type == "text")
    data = gp._extract_json(raw)
    data.setdefault("tags", [])
    data.setdefault("sub_keywords", subs)
    data.setdefault("main_keyword", main_keyword)
    data.setdefault("title", title or topic)
    return library.create_item(post_type, data)
