"""블로그 자동화용 Supabase 저장소.

웹(service/app.py)과 집 PC 일꾼(worker) 이 같이 쓰는 데이터 계층이다.
연결은 database/supabase_client.py 의 get_client() 를 그대로 재사용한다.

테이블은 supabase/migrations/005_blog.sql 로 만든다:
    blog_posts / blog_ranks / blog_recommendations  (+ 기존 jobs 재사용)

역할 분담(리뷰답글과 동일한 패턴):
    웹   → request_blog_job(...) 으로 jobs 에 요청만 남긴다
    일꾼 → 그 잡을 집어 실행하고 결과를 blog_* 에 기록한다
"""

import logging
from datetime import datetime, timezone

from .supabase_client import get_client

logger = logging.getLogger(__name__)

POSTS = "blog_posts"
RANKS = "blog_ranks"
RECS = "blog_recommendations"
JOBS = "jobs"

# 웹이 일꾼에게 시킬 수 있는 일
JOB_KINDS = ("blog_recommend", "blog_draft", "blog_publish", "blog_rank",
             "blog_media", "blog_learn", "blog_react", "blog_plan")


def _now():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 글 창고
# ---------------------------------------------------------------------------

def save_post(title, body, post_type=None, main_keyword=None,
              sub_keywords=None, tags=None, status="ready"):
    """새 글(초안)을 창고에 넣고 id 를 돌려준다."""
    row = {
        "title": title, "body": body, "post_type": post_type,
        "main_keyword": main_keyword, "sub_keywords": sub_keywords or [],
        "tags": tags or [], "status": status,
    }
    res = get_client().table(POSTS).insert(row).execute()
    return res.data[0]["id"] if res.data else None


def list_posts(status=None, limit=100):
    q = get_client().table(POSTS).select("*").neq("status", "trashed")
    if status:
        q = q.eq("status", status)
    res = q.order("created_at", desc=True).limit(limit).execute()
    return res.data or []


def count_posts(status) -> int:
    """그 상태인 글 개수 — 홈 화면 배지용(본문까지 받아오면 무겁다)."""
    res = (get_client().table(POSTS).select("id", count="exact")
           .eq("status", status).execute())
    return res.count or 0


def get_post(post_id):
    res = get_client().table(POSTS).select("*").eq("id", post_id).limit(1).execute()
    return res.data[0] if res.data else None


def update_post(post_id, **fields):
    """본문 수정 등 부분 갱신. 예) update_post(3, body='...')"""
    fields["updated_at"] = _now()
    get_client().table(POSTS).update(fields).eq("id", post_id).execute()


def set_status(post_id, status, scheduled_at=None):
    """ready / scheduled / published / trashed"""
    fields = {"status": status, "updated_at": _now()}
    if status == "scheduled":
        fields["scheduled_at"] = scheduled_at
    elif status == "published":
        fields["published_at"] = _now()
    else:
        fields["scheduled_at"] = None
    get_client().table(POSTS).update(fields).eq("id", post_id).execute()


def trash_post(post_id):
    """삭제는 소프트 삭제(복원 가능)."""
    set_status(post_id, "trashed")


def upcoming_scheduled(limit=5):
    res = (get_client().table(POSTS).select("*")
           .eq("status", "scheduled")
           .gte("scheduled_at", _now())
           .order("scheduled_at").limit(limit).execute())
    return res.data or []


# ---------------------------------------------------------------------------
# 순위 추적
# ---------------------------------------------------------------------------

def save_ranks(results):
    """일꾼이 확인한 순위 결과 목록을 기록.

    results: [{keyword, found, rank, page, pos_in_page, scanned}, ...]
    """
    if not results:
        return
    rows = [{
        "keyword": r.get("keyword"), "found": bool(r.get("found")),
        "rank": r.get("rank"), "page": r.get("page"),
        "pos_in_page": r.get("pos_in_page"), "scanned": r.get("scanned"),
    } for r in results]
    get_client().table(RANKS).insert(rows).execute()


def latest_ranks(limit=200):
    """키워드별 최신 1건씩. (최근 기록을 받아 파이썬에서 추린다)"""
    res = (get_client().table(RANKS).select("*")
           .order("checked_at", desc=True).limit(limit).execute())
    seen, out = set(), []
    for r in res.data or []:
        if r["keyword"] in seen:
            continue
        seen.add(r["keyword"])
        out.append(r)
    return out


def rank_history(keyword, limit=30):
    res = (get_client().table(RANKS).select("*").eq("keyword", keyword)
           .order("checked_at", desc=True).limit(limit).execute())
    return list(reversed(res.data or []))


# ---------------------------------------------------------------------------
# 글감 추천
# ---------------------------------------------------------------------------

def replace_recommendations(items):
    """추천 글감을 통째로 새로 저장(기존 것은 지운다)."""
    c = get_client()
    c.table(RECS).delete().neq("id", 0).execute()
    if not items:
        return
    rows = [{
        "priority": r.get("priority"), "tier": r.get("tier"),
        "title": r.get("title"), "post_type": r.get("type") or r.get("post_type"),
        "main_keyword": r.get("main_keyword"), "sub_keywords": r.get("sub_keywords") or [],
        "competition": r.get("competition"), "search_intent": r.get("search_intent"),
        "why": r.get("why"), "timing": r.get("timing"),
    } for r in items]
    c.table(RECS).insert(rows).execute()


def list_recommendations():
    res = get_client().table(RECS).select("*").order("priority").execute()
    return res.data or []


# ---------------------------------------------------------------------------
# 잡(웹 → 일꾼 요청)
# ---------------------------------------------------------------------------

def request_blog_job(kind, payload=None, by=None):
    """웹에서 일꾼에게 블로그 작업을 요청한다. 이미 대기 중이면 그 잡을 돌려준다."""
    if kind not in JOB_KINDS:
        raise ValueError(f"알 수 없는 블로그 작업: {kind}")
    c = get_client()
    # blog_learn 은 수정 1건 = 잡 1건이어야 한다(각각 다른 내용) → 중복 억제 제외
    if kind != "blog_learn":
        same = (c.table(JOBS).select("*").eq("kind", kind)
                .in_("status", ["pending", "running"]).limit(1).execute())
        if same.data:
            return same.data[0]
    res = c.table(JOBS).insert({
        "kind": kind, "status": "pending", "requested_by": by, "payload": payload or {},
    }).execute()
    return res.data[0] if res.data else None


def latest_blog_job(kind=None):
    q = get_client().table(JOBS).select("*")
    q = q.eq("kind", kind) if kind else q.like("kind", "blog_%")
    res = q.order("requested_at", desc=True).limit(1).execute()
    return res.data[0] if res.data else None


def claim_next_blog_job():
    """일꾼이 부르는 함수 — 대기 중인 블로그 잡 하나를 집어 running 으로 바꾼다."""
    c = get_client()
    res = (c.table(JOBS).select("*").eq("status", "pending").like("kind", "blog_%")
           .order("requested_at").limit(1).execute())
    if not res.data:
        return None
    job = res.data[0]
    c.table(JOBS).update({"status": "running", "started_at": _now()}).eq("id", job["id"]).execute()
    return job


def finish_blog_job(job_id, status="done", message=None, result_count=None):
    get_client().table(JOBS).update({
        "status": status, "finished_at": _now(),
        "message": message, "result_count": result_count,
    }).eq("id", job_id).execute()


# ---------------------------------------------------------------------------
# 채널 배분안 (media_plans — 007_media_plans.sql)
# ---------------------------------------------------------------------------

PLANS = "media_plans"


def save_plan(topic, plan):
    """새 배분안 저장. 같은 주제의 이전 pending 은 자동 반려(최신 것만 살린다)."""
    c = get_client()
    c.table(PLANS).update({"status": "rejected", "decided_at": _now()}) \
        .eq("topic", topic).eq("status", "pending").execute()
    res = c.table(PLANS).insert({"topic": topic, "plan": plan}).execute()
    return res.data[0]["id"] if res.data else None


def latest_plans(limit=3):
    """웹에 보여줄 최근 배분안(대기 우선)."""
    res = (get_client().table(PLANS).select("*")
           .neq("status", "rejected")
           .order("created_at", desc=True).limit(limit).execute())
    return res.data or []


def get_plan(plan_id):
    res = get_client().table(PLANS).select("*").eq("id", plan_id).limit(1).execute()
    return res.data[0] if res.data else None


def decide_plan(plan_id, status):
    """approved / rejected"""
    get_client().table(PLANS).update(
        {"status": status, "decided_at": _now()}).eq("id", plan_id).execute()
