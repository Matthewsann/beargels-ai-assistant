"""
[에이전트 ③] 검수 (Editor/QC).

작가 에이전트가 만든 초안을 네이버 SEO 체크리스트로 심사하고 점수화합니다.
기준점(config: review.min_score, 기본 80) 미달이면 수정본을 만들어 교체합니다.

단독 실행:  python src/review_post.py           # 창고의 모든 ready 글 재심사(점수만 갱신)
파이프라인: generate_post.py 가 생성 직후 자동 호출 (config: review.enabled)
"""

from __future__ import annotations

import json
import re

REVIEW_PROMPT = """당신은 네이버 블로그 상위 노출 검수 전문가입니다.
아래 블로그 글 초안을 심사 기준으로 평가하고, JSON 으로만 답하세요.

[심사 기준 — 각 항목 감점 요소]
1. 제목: 대표 키워드가 앞쪽에 있는가, 25자 내외인가, 낚시성은 아닌가
2. 본문 분량: 공백 제외 1,500자 이상인가
3. 첫 문단(2~3줄)에 대표 키워드가 자연스럽게 1회 들어갔는가
4. 키워드 빈도: 대표 키워드 3~5회 (2회 이하=부족, 8회 이상=도배 위험)
5. 소제목 구조화 2~4개, 모바일에서 읽기 쉬운 짧은 문단인가
6. [📷 사진: ...] 위치 표시가 5개 이상인가
7. 하단 정보 블록(주소/영업시간)과 해시태그 6~10개가 있는가
8. fluff(알맹이 없는 문장) 없이 문장마다 정보가 있는가, 구체적 숫자가 있는가
9. 과장 광고 문구("국내 최고" 등)가 없는가
10. 캐릭터(베어그리 등) 사용이 1~2회로 절제되어 있는가

[초안]
제목: {title}
대표 키워드: {main_keyword}
본문:
{body}
태그: {tags}

[출력 — 순수 JSON만]
{{
  "score": 0~100 정수,
  "issues": ["발견한 문제를 짧게, 없으면 빈 배열"],
  "needs_revision": true/false,
  "revised": {{"title": "...", "body": "...", "tags": ["..."]}}
}}
- needs_revision 이 true 면 revised 에 문제를 고친 완성본을 넣으세요(본문 전체).
- false 면 revised 는 null 로 하세요.
"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        if brace:
            text = brace.group(0)
    return json.loads(text)


def review_one(client, cfg: dict, data: dict) -> tuple[dict, dict]:
    """초안을 심사한다. 반환: (최종 데이터, 심사 결과 {score, issues}).

    기준점 미달 + 수정본 존재 시 수정본으로 교체해 돌려준다.
    검수 자체가 실패하면(파싱 오류 등) 원본을 그대로 통과시킨다 — 파이프라인 중단 방지.
    """
    rcfg = cfg.get("review", {})
    min_score = int(rcfg.get("min_score", 80))
    gen = cfg.get("generate", {})

    prompt = REVIEW_PROMPT.format(
        title=data.get("title", ""),
        main_keyword=data.get("main_keyword", ""),
        body=data.get("body", ""),
        tags=", ".join(data.get("tags", [])),
    )
    try:
        msg = client.messages.create(
            model=gen.get("model", "claude-sonnet-5"),
            max_tokens=gen.get("max_tokens", 5000),
            messages=[{"role": "user", "content": prompt}],
        )
        result = _extract_json("".join(b.text for b in msg.content if b.type == "text"))
    except Exception as e:
        print(f"    ⚠ 검수 실패(원본 통과): {e}")
        return data, {"score": None, "issues": [f"검수 불가: {e}"]}

    score = int(result.get("score", 0))
    issues = result.get("issues", [])
    revised = result.get("revised")

    if score < min_score and isinstance(revised, dict) and revised.get("body"):
        final = dict(data)
        final["title"] = revised.get("title", data.get("title", ""))
        final["body"] = revised["body"]
        if revised.get("tags"):
            final["tags"] = revised["tags"]
        print(f"    ✎ 검수 {score}점(<{min_score}) → 수정본으로 교체")
        return final, {"score": score, "issues": issues, "revised": True}

    print(f"    ✔ 검수 {score}점 통과" + (f" (지적 {len(issues)}건)" if issues else ""))
    return data, {"score": score, "issues": issues, "revised": False}


def main() -> None:
    """창고의 ready 글을 일괄 재심사해 meta 에 점수를 기록한다(본문은 유지)."""
    from anthropic import Anthropic
    import generate_post
    import library

    cfg = generate_post.load_config()
    client = Anthropic()
    items = library.list_items(status=library.STATUS_READY)
    if not items:
        print("재심사할 ready 글이 없습니다.")
        return

    print(f"창고 {len(items)}건 재심사…")
    for m in items:
        data = library.load_post(m["id"])
        print(f"  #{m['id']:04d} {m.get('title','')}")
        _, review = review_one(client, cfg, data)
        meta = library.load_meta(m["id"])
        meta["review_score"] = review.get("score")
        meta["review_issues"] = review.get("issues", [])
        library.save_meta(m["id"], meta)
    print("완료. weekly_publish 목록에서 점수를 참고해 고르세요.")


if __name__ == "__main__":
    main()
