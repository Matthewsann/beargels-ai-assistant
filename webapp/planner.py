"""기획실 엔진 — 금고(knowledge/)를 읽어 마케팅 전문가처럼 블로그 글을 기획.

- make_plan(hint): 주제·키워드·제목후보·구성·촬영리스트를 담은 기획안(dict) 반환
- make_draft(topic, post_type): 기획을 바탕으로 실제 초안을 생성해 라이브러리에 적재, id 반환

둘 다 Claude(Anthropic) API 를 쓴다 → ANTHROPIC_API_KEY 와 크레딧 필요.
크레딧이 없으면 예외가 나며, 호출부(app.py)가 사장님께 '충전 안내'로 바꿔 보여준다.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "automation" / "src"
for _p in (SRC, ROOT, ROOT / "worker"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import llm  # noqa: E402 — 위에서 ROOT 를 sys.path 에 넣은 뒤여야 한다

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


def load_photos() -> tuple[str, dict]:
    """사진함에 **지금 실제로 있는** 사진 목록을 읽어온다.

    글을 먼저 쓰고 사진을 나중에 끼워 넣으면 글과 사진이 따로 논다. 그래서
    초안을 쓰기 전에 이 목록을 프롬프트에 넣어, 있는 사진으로 글을 짜게 한다.
    사진함이 비었거나 인덱스가 아직 없으면 빈 값 — 예전처럼 글만 나온다.
    """
    try:
        import blog_media
        cat = blog_media.catalog()
        return (blog_media.catalog_text(cat) if cat else ""), cat
    except Exception as e:  # noqa: BLE001 — 사진이 없다고 글쓰기가 멈추면 안 된다
        import logging
        logging.getLogger(__name__).warning("사진함을 읽지 못했습니다: %s", str(e)[:120])
        return "", {}


def load_performance() -> str:
    """발행 글들의 품질·반응·순위 요약(성과 피드백). 없으면 빈 문자열."""
    try:
        import blog_perf
        return blog_perf.perf_context()
    except Exception as e:  # noqa: BLE001 — 성과 데이터가 없어도 글쓰기는 계속
        import logging
        logging.getLogger(__name__).warning("성과 요약 실패: %s", str(e)[:120])
        return ""


def _client_cfg():
    """설정만 돌려준다. 실제 AI 호출은 llm.complete 가 공급자를 골라서 한다.

    (client 자리는 옛 호출부 호환을 위해 None 을 둔다 — 더는 쓰지 않는다.)
    """
    import generate_post  # automation/src — .env 로드 + config.yaml
    return None, generate_post.load_config(), generate_post


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
    raw = llm.complete(user=prompt, max_tokens=2500, prefer="gemini")
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
===== 지금 쓸 수 있는 사진 (사진함) =====
{photos}
===== 발행 글 성과 (반응 피드백) =====
{performance}
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
- ★사진은 위 '사진함' 목록에 **있는 것만** 쓴다. 본문에 `[📷 P07]` 처럼 번호만 적으면
  그 자리에 실제 사진이 들어간다. 5~8장. **목록에 없는 번호를 지어내지 마라.**
  · 첫 문단 앞에 대표사진(★대표감) 1장을 먼저 놓는다.
  · 사진 바로 앞 문장은 그 사진에 실제로 찍힌 것과 이어지게 쓴다(엉뚱한 설명 금지).
  · 같은 사진을 두 번 쓰지 않는다.
  · 영상(V01 …)이 목록에 있으면 글 중간에 1개까지 `[🎬 V01]` 로 넣어도 좋다.
  · 사진 목록이 비어 있으면 사진 표시 없이 글만 쓴다.
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


REC_PROMPT = """너는 베어글스 송도점의 네이버 블로그 마케팅 전문가다.
아래 '정보 금고'(철학·톤·메뉴·고객)와 'SEO 지식'을 근거로만 판단한다(지어내지 말 것).

===== 정보 금고 =====
{knowledge}
===== SEO 지식 =====
{seo}
===== 지금 사진함에 있는 사진 =====
{photos}
===== 발행 글 성과 (반응 피드백) =====
{performance}
=====================

★ 사진이 이미 있는 주제를 먼저 추천하라. 사진 없이 글만 있는 글은 상위노출도 안 되고
   사장님이 다시 촬영해야 해서 결국 안 올라간다. 위 사진 목록으로 **바로 쓸 수 있는**
   주제를 앞 번호(priority)에 두고, 촬영이 더 필요한 주제는 뒤로 미뤄라.
   각 글감의 "why" 끝에 쓸 사진 번호를 적어라(예: "… / 사진 P03,P11 있음").

베어글스답고(루틴·Basecamp·따뜻함) SEO 상위노출에 유리하며, 실제 인기메뉴·타겟(단골·직장인·건강지향)을
노리는 블로그 글감 10개를 추천하라. 유형(정보성·신메뉴·일상·후기·이벤트)을 다양하게 섞어라.

★ 가장 중요 — 키워드 승산 판단 (이 블로그는 오픈 10개월·SEO글 0인 신생):
- tier "green"(승산 높음): 롱테일·지역세부·정보성 키워드. 경쟁 낮아 신생 블로그도 1페이지 가능. → 먼저 발행.
- tier "yellow"(장기전): "송도 베이글","송도 카페" 같은 헤드 키워드. 검색량 크나 경쟁 높음 → C-Rank 쌓은 뒤.
- tier "red"(브랜드용): 검색 수요가 거의 없는 브랜드·일상글. SEO 유입 기대 낮음(각인·단골 소통용).
- priority: 1부터. green(승산+검색의도+전환 높은 것)을 앞 번호로, red를 뒤로.

아래 JSON 배열 하나만 순수 출력(설명·코드블록 금지):
[
  {{"priority":1,"tier":"green","title":"제목안(25자내외)","type":"정보성","main_keyword":"대표키워드","sub_keywords":["세부1","세부2"],"competition":"낮음/보통/높음","search_intent":"정보/지역방문/브랜드","why":"금고 근거 한 줄","timing":"여름/상시 등"}}
]
정확히 10개. priority 는 1~10 중복 없이."""


def _extract_json_array(text: str) -> list:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\[.*\])\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    else:
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            text = m.group(0)
    return json.loads(text)


def make_recommendations() -> list[dict]:
    """금고 전체를 읽고 베어글스 맞춤 글감 10개를 추천(JSON 배열)."""
    client, cfg, gp = _client_cfg()
    knowledge, seo = load_knowledge()
    photos, _cat = load_photos()
    prompt = REC_PROMPT.format(knowledge=knowledge, seo=seo,
                               photos=photos or "(사진함이 비어 있음 — 촬영부터 필요)",
                               performance=load_performance() or "(아직 성과 데이터 없음)")
    raw = llm.complete(user=prompt, max_tokens=2500, prefer="gemini")
    return _extract_json_array(raw)


def make_draft_data(topic: str, post_type: str = "정보성", title: str = "",
                    main_keyword: str = "", sub_keywords: list[str] | None = None) -> dict:
    """확정 기획 + 금고 전체를 근거로 초안 '데이터'만 만들어 돌려준다(저장은 호출자 몫).

    로컬 라이브러리에 넣을지, Supabase 에 넣을지는 부르는 쪽이 정한다.
    """
    client, cfg, gp = _client_cfg()
    knowledge, seo = load_knowledge()
    photos, _cat = load_photos()
    subs = sub_keywords or []
    prompt = DRAFT_PROMPT.format(
        knowledge=knowledge, seo=seo, photos=photos or "(사진함이 비어 있음)",
        performance=load_performance() or "(아직 성과 데이터 없음)",
        topic=topic, post_type=post_type,
        title=title or topic, main_keyword=main_keyword,
        sub_keywords=", ".join(subs), sub_keywords_json=json.dumps(subs, ensure_ascii=False),
    )
    raw = llm.complete(user=prompt, prefer="gemini",
                       max_tokens=cfg.get("generate", {}).get("max_tokens", 5000))
    data = gp._extract_json(raw)
    data.setdefault("tags", [])
    data.setdefault("sub_keywords", subs)
    data.setdefault("main_keyword", main_keyword)
    data.setdefault("title", title or topic)
    return data


def make_draft(topic: str, post_type: str = "정보성", title: str = "",
               main_keyword: str = "", sub_keywords: list[str] | None = None) -> int:
    """초안을 만들어 로컬 라이브러리(automation/library)에 적재하고 id 를 돌려준다."""
    import library
    data = make_draft_data(topic, post_type, title, main_keyword, sub_keywords)
    return library.create_item(post_type, data)
