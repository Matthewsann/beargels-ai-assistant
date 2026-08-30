"""발행 전 품질 게이트 — 초안의 품질 점수를 매기고, 낮으면 스스로 한 번 고친다.

왜 필요한가:
    글을 쌓는 속도보다 중요한 게 '나간 글의 수준'이다. 점수가 낮은 초안이
    그대로 창고에 들어가면 사장님이 일일이 읽고 걸러야 한다. 여기서
    ①기계 점검(글자수·키워드·사진 수 — webapp/evaluator.py 재사용)과
    ②AI 전문가 평가(SEO·브랜드 톤)를 합쳐 100점 만점 점수를 내고,
    기준 미달이면 개선점을 먹여 **1회 자동 퇴고** 후 더 나은 쪽을 저장한다.

점수 기록은 data/blog_quality.json 에 쌓인다 — 발행 후 반응(blog_perf.py)과
합쳐져 "품질 몇 점짜리 글이 실제로 반응이 좋았나"를 다음 기획에 알려준다.
"""
from __future__ import annotations

import json
import logging
import pathlib
import re
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "webapp", ROOT / "worker", ROOT / "automation" / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

logger = logging.getLogger(__name__)

STORE = ROOT / "data" / "blog_quality.json"

# 이 점수 밑이면 자동 퇴고를 시도한다. 퇴고 후에도 낮으면 그대로 저장하되
# 점수가 메시지에 붙어 사장님이 걸러 볼 수 있다.
QUALITY_MIN = 80
# 이 글자 수(사진 표시 제외) 밑이면 점수와 무관하게 확장 퇴고를 건다.
# 네이버 상위노출 기준 1,500자 — 짧은 글은 무료 모델의 고질 약점이다.
LENGTH_MIN = 1500   # evaluator·프롬프트와 동일 기준(2026-08-30 통일)
# 퇴고 최대 횟수. 1회로 부족한 경우(짧고+개선점 많음)를 위해 2회까지.
MAX_REVISIONS = 2


def _load() -> dict:
    if not STORE.exists():
        return {}
    try:
        return json.loads(STORE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save(d: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def score(body: str, title: str, main_keyword: str) -> dict:
    """기계 점검 + AI 전문가 평가 → 종합 점수와 개선점.

    종합 = AI 점수에서 기계 점검 경고당 3점 감점(사진 부족·키워드 도배 같은
    객관 결함은 AI 총평과 별개로 반드시 점수에 반영돼야 한다).
    """
    import evaluator
    checks = evaluator.mechanical_check(body, title, main_keyword)
    warns = [c for c in checks if c.get("status") == "warn"]
    review = {}
    for attempt in range(2):              # 무료 모델이 깨진 JSON 을 줄 때가 있다
        try:
            review = evaluator.expert_review(body, title, main_keyword)
            break
        except Exception as e:  # noqa: BLE001
            logger.warning("AI 평가 실패(%d/2): %s", attempt + 1, str(e)[:100])
    if not review:
        # AI 평가가 끝내 안 되면 기계 점검만으로 보수적으로 낸다
        review = {"score": 80, "one_line": "(AI 평가 실패 — 기계 점검만 반영)",
                  "improvements": [], "brand_fit": ""}
    ai = review.get("score") or 60
    final = max(0, min(100, int(ai) - 3 * len(warns)))
    return {
        "score": final, "ai_score": ai,
        "one_line": review.get("one_line", ""),
        "improvements": review.get("improvements", []),
        "brand_fit": review.get("brand_fit", ""),
        "warns": [f"{c['label']}: {c['value']} ({c['hint']})" for c in warns],
    }


REVISE_PROMPT = """너는 베어글스 송도점의 네이버 블로그 전문가다.
아래 블로그 초안을 지적된 개선점대로 **직접 고쳐 써라**. 새로 쓰지 말고 고쳐라.

[제목] {title}
[대표 키워드] {main_keyword}

[초안]
{body}

[반드시 반영할 개선점]
{improvements}

[규칙]
- 사진/영상 표시 `[📷 …]` `[🎬 …]` 는 **한 글자도 바꾸지 말고 그 위치 그대로** 둔다.
  (표시 속 파일 경로가 실제 업로드에 쓰인다 — 지어내거나 옮기면 사진이 깨진다)
- 사실(메뉴·주소·재료)은 초안에 있는 것만 쓴다. 새 사실을 지어내지 않는다.
- 따뜻하고 담백한 해요체. 과장 금지(역대급/미쳤다/인생맛집/대박/혜자 금지).
- 이모지는 문단마다 1~2개 유지(없으면 채워라). 제목에는 쓰지 않는다.
- 전체 분량은 늘리면 늘렸지 줄이지 않는다.

[출력] 고친 본문 전체만 순수 출력(설명·코드블록·JSON 금지)."""


def improve(body: str, title: str, main_keyword: str,
            improvements: list[str]) -> str | None:
    """개선점을 먹여 한 번 퇴고한 본문. 사진 표시가 깨졌으면 버린다(None)."""
    import llm
    imp = "\n".join(f"- {i}" for i in improvements[:6]) or "- 전반적 완성도"
    # 유료 Claude API 에 의지하지 않는다(사장님 확정 2026-08-30) — 퇴고도
    # 무료(Gemini) 기본. 확장 퇴고 루프가 무료 모델의 분량 약점을 메운다.
    raw = llm.complete(user=REVISE_PROMPT.format(
        title=title, main_keyword=main_keyword, body=body, improvements=imp),
        max_tokens=4000, prefer="gemini").strip()
    raw = re.sub(r"^```.*?\n|\n```$", "", raw, flags=re.DOTALL)
    # 퇴고가 사진 표시를 잃어버렸으면 원본이 낫다
    marks = re.findall(r"\[[📷🎬][^\]]*\]", body)
    kept = sum(1 for m in marks if m in raw)
    if marks and kept < len(marks):
        logger.warning("퇴고본이 사진 표시 %d/%d개를 잃음 — 원본 유지",
                       kept, len(marks))
        return None
    return raw


def _plain_len(body: str) -> int:
    """사진 표시를 뺀 본문 글자 수."""
    return len(re.sub(r"\[[📷🎬][^\]]*\]", "", body).strip())


def gate(body: str, title: str, main_keyword: str) -> tuple[str, dict]:
    """품질 게이트: 점수·분량을 재고, 모자라면 최대 2회 퇴고해 최선을 돌려준다.

    기준(사장님 2026-08-28): '충분한 퀄리티와 분량'.
    → 80점 미만 **또는** 1,400자 미만이면 확장·개선 퇴고를 건다.
    반환: (최종 본문, 품질 기록 dict)
    """
    q = score(body, title, main_keyword)
    q["revised"] = False
    first_score = q["score"]
    for _ in range(MAX_REVISIONS):
        short = _plain_len(body) < LENGTH_MIN
        if q["score"] >= QUALITY_MIN and not short:
            break
        improvements = list(q.get("improvements") or [])
        if short:
            improvements.insert(0, (
                f"본문이 {_plain_len(body)}자로 짧다. 금고에 있는 사실만으로 "
                f"{LENGTH_MIN + 100}자 이상으로 확장하라 — 먹는 팁, 어울리는 음료, "
                f"방문 시간대, 포장 여부 같은 실제 정보 밀도를 높여서. 뻔한 인사말로 늘리지 마라."))
        if not improvements:
            break
        logger.info("품질 %d점·%d자 — 자동 퇴고", q["score"], _plain_len(body))
        better = improve(body, title, main_keyword, improvements)
        if not better:
            break
        q2 = score(better, title, main_keyword)
        # 분량이 늘었으면 점수가 같아도 취한다(분량 자체가 기준이므로)
        if q2["score"] >= q["score"] or _plain_len(better) > _plain_len(body):
            body, q = better, q2
            q["revised"] = True
        else:
            break
    if q.get("revised"):
        q["before_score"] = first_score
    q["chars"] = _plain_len(body)
    return body, q


def record(post_id: int, title: str, q: dict) -> None:
    """품질 기록 저장 — 반응 데이터와 짝지어 '품질→성과' 학습에 쓴다."""
    d = _load()
    d[str(post_id)] = {
        "title": title, "score": q.get("score"),
        "one_line": q.get("one_line", ""), "revised": q.get("revised", False),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    _save(d)


def get(post_id) -> dict | None:
    return _load().get(str(post_id))
