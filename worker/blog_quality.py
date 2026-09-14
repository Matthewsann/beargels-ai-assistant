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
QUALITY_MIN = 90     # 사장님 2026-09-15: 90점 이상. 채점은 AI 라 ±3점 흔들린다 — 목표이지 보장은 아님
# 이 글자 수(사진 표시 제외) 밑이면 점수와 무관하게 확장 퇴고를 건다.
# 네이버 상위노출 기준 1,500자 — 짧은 글은 무료 모델의 고질 약점이다.
LENGTH_MIN = 1500   # evaluator·프롬프트와 동일 기준(2026-08-30 통일)
# 퇴고 최대 횟수. 1회로 부족한 경우(짧고+개선점 많음)를 위해 2회까지.
MAX_REVISIONS = 3    # 90점 목표라 한 번 더(호출 ≈ 초안 1 + 평가 4 + 퇴고 3)
# 대표 키워드가 본문에 이 횟수 미만이면 점수와 무관하게 퇴고를 건다(2026-09-15 사장님:
# "대표 키워드 반복 1회밖에 안 돼"). 검색은 정확한 문자열을 센다(evaluator 와 같은 기준).
KW_MIN = 3
KW_MAX = 5


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
- 소제목은 `## 소제목` 줄로 쓴다(◆ ■ ▶ 같은 기호 금지). 이야기가 크게 넘어가는
  곳에는 `---` 만 있는 줄을 둔다 — 이 두 표시가 네이버에서 소제목·구분선이 된다.

[출력] 고친 본문 전체만 순수 출력(설명·코드블록·JSON 금지).
위 `[제목]` `[대표 키워드]` `[초안]` 같은 **머리말은 절대 다시 쓰지 마라** — 본문 첫
줄부터 시작한다(실측 2026-09-06: 머리말을 그대로 베껴 네이버 본문 맨 위에 찍혔다)."""


# 퇴고 프롬프트의 머리말(`[제목] …`)을 모델이 그대로 베껴 본문 맨 위에 남기는
# 일이 있다 — 그대로 네이버에 찍힌다(2026-09-06 글#2 실측). 프롬프트로도 막고
# 여기서 한 번 더 잘라낸다. 사진 표시 `[📷 …]` 는 건드리지 않는다.
_META_HEAD = re.compile(
    r"^\s*(?:\[\s*(?:제목|대표\s*키워드|세부\s*키워드|초안|본문|출력)\s*\][^\n]*\n+)+")


def strip_meta_head(text: str) -> str:
    """본문 맨 앞에 붙은 프롬프트 머리말 줄들을 걷어낸다."""
    return _META_HEAD.sub("", text or "").lstrip("\n")


def _restore_marks(original: str, revised: str) -> str:
    """퇴고본이 잃어버린 사진 표시를 원래 자리(앞 문단과 가장 닮은 문단 뒤)에 되돌려 넣는다.

    무료 모델은 퇴고하며 `[📷 …]` 를 곧잘 빼먹는다(2026-09-15 실측: 4장 중 3장). 예전엔
    그 퇴고를 통째로 버려 짧고 낮은 초안이 그대로 저장됐다. 표시만 되살리면 퇴고는 살릴 수 있다.
    """
    import difflib
    mark_re = re.compile(r"\[[📷🎬][^\]]*\]")
    o_chunks = [c for c in re.split(r"\n\s*\n", original.strip()) if c.strip()]
    r_chunks = [c for c in re.split(r"\n\s*\n", revised.strip()) if c.strip()]
    present = set(mark_re.findall(revised))
    missing = []                     # (표시, 원본에서 바로 앞 글 토막)
    prev_text = ""
    for c in o_chunks:
        if mark_re.fullmatch(c.strip()):
            if c.strip() not in present:
                missing.append((c.strip(), prev_text))
        else:
            prev_text = mark_re.sub("", c).strip()
    if not missing:
        return revised
    for mark, anchor in missing:
        best, best_r = None, 0.35
        for k, rc in enumerate(r_chunks):
            if mark_re.fullmatch(rc.strip()):
                continue
            r = difflib.SequenceMatcher(None, anchor[:120], mark_re.sub("", rc)[:120]).ratio()
            if r > best_r:
                best, best_r = k, r
        if best is None:             # 닮은 문단이 없으면 매장 정보·해시태그 앞에
            tail = next((k for k, rc in enumerate(r_chunks)
                         if rc.lstrip().startswith("[매장 정보]") or rc.lstrip().startswith("#")), len(r_chunks))
            r_chunks.insert(tail, mark)
        else:
            r_chunks.insert(best + 1, mark)
    logger.info("퇴고본에 사진 표시 %d개를 되돌려 넣음", len(missing))
    return "\n\n".join(r_chunks)


def improve(body: str, title: str, main_keyword: str,
            improvements: list[str]) -> str | None:
    """개선점을 먹여 한 번 퇴고한 본문. 사진 표시가 깨졌으면 버린다(None)."""
    import llm
    imp = "\n".join(f"- {i}" for i in improvements[:6]) or "- 전반적 완성도"
    # 유료 Claude API 에 의지하지 않는다(사장님 확정 2026-08-30) — 퇴고도
    # 무료(Gemini) 기본. 확장 퇴고 루프가 무료 모델의 분량 약점을 메운다.
    raw = llm.complete(user=REVISE_PROMPT.format(
        title=title, main_keyword=main_keyword, body=body, improvements=imp),
        max_tokens=4000, prefer="gemini", quality=True).strip()
    raw = re.sub(r"^```.*?\n|\n```$", "", raw, flags=re.DOTALL)
    raw = strip_meta_head(raw)
    # 퇴고가 사진 표시를 잃어버렸으면 원본이 낫다
    marks = re.findall(r"\[[📷🎬][^\]]*\]", body)
    kept = sum(1 for m in marks if m in raw)
    if marks and kept < len(marks):
        raw = _restore_marks(body, raw)              # 잃은 표시를 되돌려 넣고 다시 센다
        kept = sum(1 for m in marks if m in raw)
        if kept < len(marks):
            logger.warning("퇴고본이 사진 표시 %d/%d개를 잃음 — 원본 유지",
                           kept, len(marks))
            return None
    return raw


KW_PROMPT = """아래 블로그 본문에 대표 키워드 「{kw}」를 **이 글자 그대로** {need}번 더 넣어라.
자리: **본문 시작 200자 안(첫 문장)에 없으면 거기에 반드시 1번**, 소제목(`## ` 줄) 하나에 1번,
마무리 문단에 1번 — 자연스러운 문장 안에.
규칙: 다른 문장은 한 글자도 바꾸지 마라. `[📷 …]` `[🎬 …]` `[매장 정보]` 블록·해시태그는 그대로.
키워드를 줄이거나 바꿔 쓰면(예: 앞 단어 떼기) 안 센다. 설명 없이 고친 본문 전체만 출력.

[본문]
{body}"""


def ensure_keyword(body: str, main_keyword: str) -> tuple[str, int]:
    """마지막 안전망: 퇴고 뒤에도 대표 키워드가 KW_MIN 미만이면 그것만 끼워 넣는 짧은 호출.

    돌려주는 값: (본문, 최종 횟수). 사진 표시가 깨지거나 횟수가 안 늘면 원본 유지.
    """
    if not main_keyword:
        return body, 0
    n = body.count(main_keyword)
    intro_ok = main_keyword in body[:200]
    if n >= KW_MIN and intro_ok:
        return body, n
    import llm
    try:
        raw = llm.complete(user=KW_PROMPT.format(kw=main_keyword, need=max(1, KW_MIN - n), body=body),
                           max_tokens=4000, prefer="gemini", quality=True).strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("키워드 보강 호출 실패: %s", str(e)[:100])
        return body, n
    raw = re.sub(r"^```.*?\n|\n```$", "", raw, flags=re.DOTALL)
    raw = strip_meta_head(raw)
    marks = re.findall(r"\[[📷🎬][^\]]*\]", body)
    if any(m not in raw for m in marks):
        logger.warning("키워드 보강본이 사진 표시를 잃음 — 원본 유지")
        return body, n
    if _plain_len(raw) < _plain_len(body) * 0.9:
        logger.warning("키워드 보강본이 본문을 줄임 — 원본 유지")
        return body, n
    n2 = raw.count(main_keyword)
    if n2 < n or (n2 == n and intro_ok) or (main_keyword not in raw[:200] and not intro_ok and n2 <= n):
        return body, n
    logger.info("대표 키워드 보강: %d → %d회", n, n2)
    return raw, n2


_TIME_RE = re.compile(
    r"(?:오전|오후|아침|저녁|밤|새벽)?\s*\d{1,2}\s*시(?:\s*\d{1,2}\s*분|\s*반)?"
    r"|\b\d{1,2}:\d{2}\b")


def _unconfirmed_times(body: str) -> list[str]:
    """본문(매장 정보 블록 제외)에서 확정 영업시간에 없는 시각 표현을 찾는다."""
    try:
        import blog_jobs
        hours = (blog_jobs.store_info().get("hours") or "")
    except Exception:  # noqa: BLE001
        hours = ""
    text = re.sub(r"\[매장 정보\][^\n]*(?:\n(?![ \t]*\n)[^\n]*)*", "", body or "")
    text = re.sub(r"\[[📷🎬][^\]]*\]", "", text)
    ok_nums = set(re.findall(r"\d{1,2}", hours))
    out = []
    for m in _TIME_RE.finditer(text):
        tok = m.group(0).strip()
        nums = re.findall(r"\d{1,2}", tok)
        if nums and all(n in ok_nums for n in nums):
            continue                      # 확정 영업시간에 있는 숫자면 통과
        if tok not in out:
            out.append(tok)
    return out[:4]


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
        kw_n = body.count(main_keyword) if main_keyword else KW_MIN
        kw_intro = (main_keyword in body[:200]) if main_keyword else True   # evaluator 와 같은 기준
        if q["score"] >= QUALITY_MIN and not short and kw_n >= KW_MIN and kw_intro:
            break
        improvements = list(q.get("improvements") or [])
        # 기계 점검 경고(키워드 횟수·제목 길이·사진 수)는 AI 총평보다 객관적이다 — 항상 먹인다
        improvements = [w for w in (q.get("warns") or [])] + improvements
        # 확정 매장 사실에 없는 시간 표현(예: "매일 오전 10시")은 거짓 정보 — 지우게 한다
        # (2026-09-15 실측: 본문 '오전 10시' vs 매장 정보 '7:20~23:00' 을 채점기가 잡아냈다)
        for bad in _unconfirmed_times(body):
            improvements.insert(0, (
                f"본문의 「{bad}」 는 확정 매장 사실(영업시간)에 없는 시간이다 — 그 표현을 빼거나, "
                f"확정값 그대로만 써라. 시간·가격·수치는 지어내지 않는다."))
        if not kw_intro:
            improvements.insert(0, (
                f"본문 첫 문단(시작 200자 안)에 대표 키워드 「{main_keyword}」가 없다. 첫 문장을 이 키워드가 "
                f"그 글자 그대로 들어가게 고쳐라(사진 표시는 그대로 두고 그 다음 글줄부터)."))
        if kw_n < KW_MIN and main_keyword:
            improvements.insert(0, (
                f"대표 키워드 「{main_keyword}」가 본문에 {kw_n}회뿐이다. **이 글자 그대로** "
                f"{KW_MIN}~{KW_MAX}회가 되게 {KW_MIN - kw_n}번 이상 더 넣어라 — 첫 문단·소제목 하나·"
                f"마무리 문단에 자연스럽게. 줄여 쓰거나 바꿔 쓰면 안 센다. 다른 문장은 그대로 둔다."))
        if short:
            improvements.insert(0, (
                f"본문이 {_plain_len(body)}자로 짧다. 금고에 있는 사실만으로 "
                f"{LENGTH_MIN + 100}자 이상으로 확장하라 — 먹는 팁, 어울리는 음료, "
                f"방문 시간대, 포장 여부 같은 실제 정보 밀도를 높여서. 뻔한 인사말로 늘리지 마라."))
        if not improvements:
            break
        logger.info("품질 %d점·%d자 — 자동 퇴고", q["score"], _plain_len(body))
        better = improve(body, title, main_keyword, improvements)
        if not better:                                # 무료 모델은 한 번 실패해도 다음엔 된다
            logger.info("퇴고 실패 — 한 번 더")
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
    # 퇴고를 다 돌고도 대표 키워드가 모자라면, 그것만 끼워 넣는 짧은 호출로 채운다
    try:
        body, kw_final = ensure_keyword(body, main_keyword)
        q["kw_count"] = kw_final
    except Exception as e:  # noqa: BLE001
        logger.warning("키워드 안전망 실패(무시): %s", str(e)[:100])
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
