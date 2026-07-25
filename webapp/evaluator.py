"""블로그 전문가 평가 엔진 — 발행/작성된 글을 SEO·브랜드 기준으로 평가.

- mechanical_check(): AI 없이 즉시. 글자수·키워드·소제목·사진자리 등 객관 지표 점검.
- expert_review(): 블로그 SEO 전문가(Claude)가 금고·SEO지식 기준으로 점수·강점·개선점 평가.
  → 크레딧 필요. 없으면 예외 → app.py 가 친절 안내로 변환.
"""
from __future__ import annotations

import re

import planner  # load_knowledge, _client_cfg 재사용


def _clean_len(body: str) -> int:
    """마크다운 기호·사진자리·태그를 대충 걷어낸 본문 글자 수(공백 제외)."""
    t = re.sub(r"\[📷[^\]]*\]", "", body)
    t = re.sub(r"[#>*`\-]", "", t)
    t = re.sub(r"#\S+", "", t)  # 해시태그
    t = re.sub(r"\s+", "", t)
    return len(t)


def mechanical_check(body: str, title: str, main_keyword: str) -> list[dict]:
    """SEO 기준 대비 객관 점검. 각 항목 status: ok | warn."""
    checks = []

    tlen = len(title or "")
    checks.append({
        "label": "제목 길이", "value": f"{tlen}자",
        "status": "ok" if 10 <= tlen <= 30 else "warn",
        "hint": "25자 내외 권장(검색결과에서 안 잘리게)",
    })

    blen = _clean_len(body)
    checks.append({
        "label": "본문 글자 수", "value": f"약 {blen}자",
        "status": "ok" if blen >= 1500 else "warn",
        "hint": "1,500자 이상 권장(정보가 충실해야 상위)",
    })

    kw_count = body.count(main_keyword) if main_keyword else 0
    checks.append({
        "label": "대표 키워드 반복", "value": f"{kw_count}회",
        "status": "ok" if 3 <= kw_count <= 6 else "warn",
        "hint": f"'{main_keyword}' 3~5회 적정(도배는 어뷰징)" if main_keyword else "대표 키워드 미설정",
    })

    kw_in_intro = False
    if main_keyword:
        intro = body[:200]
        kw_in_intro = main_keyword in intro
    checks.append({
        "label": "첫 문단 키워드", "value": "있음" if kw_in_intro else "없음",
        "status": "ok" if kw_in_intro else "warn",
        "hint": "도입부(초반)에 대표 키워드 1회",
    })

    subs = len(re.findall(r"^\s*##+\s", body, flags=re.MULTILINE))
    checks.append({
        "label": "소제목 수", "value": f"{subs}개",
        "status": "ok" if 2 <= subs <= 6 else "warn",
        "hint": "소제목 2~4개로 구조화(체류시간↑)",
    })

    photos = len(re.findall(r"\[📷", body))
    checks.append({
        "label": "사진 위치", "value": f"{photos}곳",
        "status": "ok" if photos >= 5 else "warn",
        "hint": "실물 사진 7~10장(D.I.A. 점수 핵심)",
    })

    has_info = bool(re.search(r"(주소|영업시간|인천|연수구|☎|전화)", body))
    checks.append({
        "label": "매장 정보 블록", "value": "있음" if has_info else "없음",
        "status": "ok" if has_info else "warn",
        "hint": "하단에 주소·영업시간(로컬 노출)",
    })

    return checks


REVIEW_PROMPT = """너는 네이버 블로그 상위노출 전문 SEO 컨설턴트다.
아래 '정보 금고'(브랜드 철학·톤·메뉴)와 'SEO 지식'을 기준으로, 주어진 블로그 글을 냉정하게 평가하라.
칭찬만 하지 말고 실제로 개선할 점을 구체적으로 짚어라.

===== 정보 금고 =====
{knowledge}
===== SEO 지식 =====
{seo}
=====================

[평가할 글]
제목: {title}
대표 키워드: {main_keyword}
본문:
{body}

아래 JSON 하나만 순수 출력(코드블록/설명 금지):
{{
  "score": 0~100 정수(상위노출 가능성 종합),
  "one_line": "한 줄 총평",
  "strengths": ["잘한 점", "..."],
  "improvements": ["구체적 개선 제안(무엇을 어떻게)", "..."],
  "brand_fit": "브랜드 철학·톤 부합도 한 줄(톤앤보이스 기준)"
}}"""


def expert_review(body: str, title: str, main_keyword: str) -> dict:
    client, cfg, gp = planner._client_cfg()
    knowledge, seo = planner.load_knowledge()
    prompt = REVIEW_PROMPT.format(
        knowledge=knowledge, seo=seo, title=title,
        main_keyword=main_keyword, body=body,
    )
    msg = client.messages.create(
        model=cfg.get("generate", {}).get("model", "claude-sonnet-5"),
        max_tokens=1800,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(b.text for b in msg.content if b.type == "text")
    data = gp._extract_json(raw)
    data.setdefault("score", None)
    data.setdefault("one_line", "")
    data.setdefault("strengths", [])
    data.setdefault("improvements", [])
    data.setdefault("brand_fit", "")
    return data
