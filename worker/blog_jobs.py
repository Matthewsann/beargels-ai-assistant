"""집 PC 일꾼의 블로그 담당 — 클라우드 웹이 요청한 블로그 작업을 실제로 실행한다.

리뷰 수집과 똑같은 구조다:
    [클라우드 웹] jobs 에 요청  →  [집 PC 일꾼] 이 파일이 실행  →  Supabase 에 결과 기록

처리하는 작업(job kind):
    blog_recommend  금고를 읽고 글감 10개 추천        (AI)
    blog_draft      기획 주제로 초안 작성 → 창고 저장   (AI)
    blog_publish    글을 네이버에 임시저장(초안) 넣기    (브라우저)
    blog_rank       타겟 키워드 네이버 순위 확인        (브라우저)

무거운 일(AI 호출·크롬 조작)은 전부 여기서만 한다. 클라우드 웹은 버튼과 결과 표시만.
실제 '발행' 버튼은 사장님이 네이버에서 직접 누른다(자동 발행하지 않는다).
"""

from __future__ import annotations

import logging
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "webapp", ROOT / "automation" / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from database import blog_store as store  # noqa: E402

logger = logging.getLogger(__name__)

BLOG_KINDS = ("blog_recommend", "blog_draft", "blog_publish", "blog_rank")

# 순위 추적 기본 키워드(창고 글의 대표 키워드에 더해 항상 확인)
DEFAULT_KEYWORDS = ("송도 베이글", "송도 카페")


def handles(kind: str) -> bool:
    return (kind or "").startswith("blog_")


# ---------------------------------------------------------------------------
# 개별 작업
# ---------------------------------------------------------------------------

def do_recommend() -> tuple[int, str]:
    """금고 기반 글감 추천 → blog_recommendations 테이블 교체."""
    import planner
    items = planner.make_recommendations()
    store.replace_recommendations(items)
    return len(items), f"글감 {len(items)}개 추천"


def do_draft(payload: dict) -> tuple[int, str]:
    """기획 주제로 초안 작성 → blog_posts 에 저장."""
    import planner
    topic = (payload.get("topic") or payload.get("title") or "").strip()
    if not topic:
        raise ValueError("주제가 비어 있습니다.")
    post_type = payload.get("post_type") or "정보성"
    data = planner.make_draft_data(
        topic=topic,
        post_type=post_type,
        title=payload.get("title") or topic,
        main_keyword=payload.get("main_keyword") or "",
        sub_keywords=payload.get("sub_keywords") or [],
    )
    post_id = store.save_post(
        title=data.get("title"), body=data.get("body"), post_type=post_type,
        main_keyword=data.get("main_keyword"), sub_keywords=data.get("sub_keywords"),
        tags=data.get("tags"),
    )
    return 1, f"초안 저장 완료 (#{post_id}) — {data.get('title', '')[:40]}"


def do_publish(payload: dict) -> tuple[int, str]:
    """글 하나를 네이버 임시저장(초안)으로 넣는다. 발행은 사장님이 직접."""
    import naver_autodraft as na
    post_id = payload.get("post_id")
    post = store.get_post(post_id) if post_id else None
    if not post:
        raise ValueError(f"글 #{post_id} 를 찾을 수 없습니다.")

    cfg = na.load_config()
    headful = bool(cfg.get("naver", {}).get("headful", True))
    pw, ctx, page = na.launch(cfg, headful=headful)
    try:
        ok = na.draft_one(page, cfg, {"title": post.get("title"), "body": post.get("body")})
    finally:
        try:
            ctx.close()
            pw.stop()
        except Exception:  # noqa: BLE001
            pass
    if not ok:
        raise RuntimeError("네이버 에디터 입력 실패 (화면 구조가 바뀌었을 수 있어요)")
    store.update_post(post_id, prepared_at=store._now())
    return 1, f"네이버 임시저장 완료 — {post.get('title', '')[:40]}"


def do_rank(payload: dict) -> tuple[int, str]:
    """타겟 키워드들의 네이버 순위를 확인해 blog_ranks 에 기록."""
    import rank_checker as rc
    blog_id = rc.get_blog_id()
    if not blog_id:
        raise ValueError("config.yaml 에서 blog_id 를 찾지 못했습니다.")

    keywords = payload.get("keywords") or []
    if not keywords:
        for p in store.list_posts(limit=100):
            k = (p.get("main_keyword") or "").strip()
            if k and k not in keywords:
                keywords.append(k)
        for k in DEFAULT_KEYWORDS:
            if k not in keywords:
                keywords.append(k)

    results = []
    for kw in keywords:
        try:
            results.append(rc.check_keyword(kw, blog_id))
        except Exception as e:  # noqa: BLE001 — 한 키워드 실패로 전체를 멈추지 않는다
            logger.warning("순위 확인 실패(%s): %s", kw, str(e)[:120])
    store.save_ranks(results)
    found = sum(1 for r in results if r.get("found"))
    return len(results), f"키워드 {len(results)}개 확인 (노출 {found}개)"


# ---------------------------------------------------------------------------
# 진입점 — agent.py 가 부른다
# ---------------------------------------------------------------------------

_HANDLERS = {
    "blog_recommend": lambda p: do_recommend(),
    "blog_draft": do_draft,
    "blog_publish": do_publish,
    "blog_rank": do_rank,
}


def run(job: dict) -> tuple[int, str]:
    """블로그 잡 1건 처리. (처리 건수, 메시지) 반환. 실패 시 예외를 올린다."""
    kind = job.get("kind")
    handler = _HANDLERS.get(kind)
    if handler is None:
        raise ValueError(f"알 수 없는 블로그 작업: {kind}")
    payload = job.get("payload") or {}
    if isinstance(payload, str):
        import json
        try:
            payload = json.loads(payload)
        except Exception:  # noqa: BLE001
            payload = {}
    return handler(payload)
