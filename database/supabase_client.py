"""
Supabase 클라이언트

수집 데이터(주문/리뷰)와 운영 데이터(할 일/일일 요약)를 Supabase에 저장·조회한다.

- 저장은 upsert(중복 무시): 같은 주문/리뷰를 여러 번 수집해도
  (platform, order_no) / (platform, review_no) 기준 한 행만 유지.
- 최초 1회 database/schema.sql 을 Supabase SQL Editor 에서 실행해야 함
  (publishable 키로는 DDL 불가).
"""

import logging
import os
import re
from datetime import date, datetime, timedelta

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
# service_role(secret) 키가 있으면 우선 사용 — RLS 를 우회하므로 정책 없이도
# 모든 테이블에 읽기/쓰기가 된다(서버 백엔드 표준). 없으면 publishable 키 사용.
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY", "")

_ORDER_COLS = (
    "platform", "order_no", "status", "ordered_at", "ordered_date", "menu",
    "price", "pay_type", "delivery_method", "ad_service", "raw",
)
_REVIEW_COLS = (
    "platform", "review_no", "author", "rating", "content", "written_at",
    "written_date", "menus", "delivery_type", "raw",
    # 플랫폼에 실제 등록돼 있는 사장님 답글 본문(schema_v6) — AI 공부 데이터.
    "platform_reply",
)
# ⚠️ reply_status 는 일부러 넣지 않는다. 우리가 'drafted/posted' 로 바꾼 값을
#    재수집 때 크롤러 값('none')이 덮어써 버리기 때문. 플랫폼에 이미 답글이
#    달렸는지는 별도 칼럼 platform_replied 로만 반영한다(아래 save_reviews).

_client: Client | None = None


def get_client() -> Client:
    """Supabase 클라이언트(싱글턴)."""
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError(".env 에 SUPABASE_URL / SUPABASE_KEY 를 설정하세요.")
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client


# ---------------------------------------------------------------------------
# 날짜 파싱 (크롤러의 한국어 원문 → ISO date)
# ---------------------------------------------------------------------------

def parse_order_date(s):
    """'2026. 07. 22. (수) 오후 02:39:09' → '2026-07-22'."""
    m = re.search(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})", s or "")
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def parse_review_date(s):
    """'2026년 7월 21일' → '2026-07-21'."""
    m = re.search(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일", s or "")
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def _pick(row, cols):
    return {c: row.get(c) for c in cols}


# ---------------------------------------------------------------------------
# 주문 / 리뷰 저장 (upsert)
# ---------------------------------------------------------------------------

def save_orders(orders):
    """주문 데이터를 저장한다(플랫폼+주문번호 upsert). 저장 행 수 반환."""
    rows = []
    for o in orders:
        if not (o.get("platform") and o.get("order_no")):
            continue
        r = _pick(o, _ORDER_COLS)
        r["ordered_date"] = o.get("ordered_date") or parse_order_date(
            o.get("ordered_at"))
        rows.append(r)
    if not rows:
        return 0
    resp = (get_client().table("orders")
            .upsert(rows, on_conflict="platform,order_no").execute())
    n = len(resp.data or [])
    logger.info("주문 %d건 저장(upsert)", n)
    return n


def save_reviews(reviews):
    """리뷰 데이터를 저장한다(플랫폼+리뷰번호 upsert). 저장 행 수 반환."""
    rows = []
    for r0 in reviews:
        if not (r0.get("platform") and r0.get("review_no")):
            continue
        r = _pick(r0, _REVIEW_COLS)
        r["written_date"] = r0.get("written_date") or parse_review_date(
            r0.get("written_at"))
        # 플랫폼에 이미 사장님 답글이 달렸는지(모르면 null 로 둔다).
        rs = r0.get("reply_status")
        r["platform_replied"] = (True if rs == "posted"
                                 else False if rs == "none" else None)
        # '마지막으로 수집된 시각'으로 갱신 — insert 기본값은 최초 1회뿐이라
        # 재수집돼도 옛 값이 남아, '최근 수집분' 조회(답글 공부)가 빈손이 된다.
        r["collected_at"] = datetime.now().astimezone().isoformat()
        rows.append(r)
    if not rows:
        return 0
    try:
        resp = (get_client().table("reviews")
                .upsert(rows, on_conflict="platform,review_no").execute())
    except Exception as e:  # noqa: BLE001 — schema_v6(platform_reply) 미적용 대비
        if getattr(e, "code", None) not in _MISSING_COLUMN_CODES:
            raise
        logger.warning("schema_v6 미적용 — platform_reply 없이 저장 "
                       "(SQL Editor 에서 database/schema_v6.sql 실행 필요)")
        for r in rows:
            r.pop("platform_reply", None)
        resp = (get_client().table("reviews")
                .upsert(rows, on_conflict="platform,review_no").execute())
    n = len(resp.data or [])
    logger.info("리뷰 %d건 저장(upsert)", n)
    return n


# ---------------------------------------------------------------------------
# 주문 / 리뷰 조회
# ---------------------------------------------------------------------------

def get_orders_by_date(day, platform=None):
    """특정 날짜(date/'YYYY-MM-DD')의 주문을 조회한다."""
    day = day.isoformat() if isinstance(day, date) else day
    q = get_client().table("orders").select("*").eq("ordered_date", day)
    if platform:
        q = q.eq("platform", platform)
    return q.execute().data


def get_reviews(platform=None, reply_status=None, limit=100):
    """리뷰를 조회한다. reply_status 로 미답변만 필터 가능."""
    q = get_client().table("reviews").select("*")
    if platform:
        q = q.eq("platform", platform)
    if reply_status:
        q = q.eq("reply_status", reply_status)
    return q.order("collected_at", desc=True).limit(limit).execute().data


# ---------------------------------------------------------------------------
# 할 일 (tasks)
# ---------------------------------------------------------------------------

def add_task(description, priority=3, task_date=None):
    """할 일 1건 추가. 저장된 행(dict) 반환."""
    row = {"description": description, "priority": priority}
    if task_date:
        row["task_date"] = task_date.isoformat() if isinstance(
            task_date, date) else task_date
    resp = get_client().table("tasks").insert(row).execute()
    return (resp.data or [None])[0]


def add_tasks(descriptions, task_date=None):
    """여러 할 일을 한 번에 추가. 저장 행 수 반환."""
    rows = []
    for i, d in enumerate(descriptions):
        r = {"description": d, "priority": i + 1}  # 입력 순서를 우선순위로
        if task_date:
            r["task_date"] = task_date.isoformat() if isinstance(
                task_date, date) else task_date
        rows.append(r)
    if not rows:
        return 0
    resp = get_client().table("tasks").insert(rows).execute()
    return len(resp.data or [])


def get_tasks(task_date=None, status=None):
    """할 일 목록 조회(기본: 오늘). status='pending'|'done' 필터 가능."""
    day = task_date or date.today()
    day = day.isoformat() if isinstance(day, date) else day
    q = get_client().table("tasks").select("*").eq("task_date", day)
    if status:
        q = q.eq("status", status)
    return q.order("priority").execute().data


def complete_task(task_id):
    """task_id 를 완료 처리한다."""
    resp = (get_client().table("tasks")
            .update({"status": "done",
                     "completed_at": datetime.now().astimezone().isoformat()})
            .eq("id", task_id).execute())
    return (resp.data or [None])[0]


def find_pending_task(text, task_date=None):
    """오늘의 미완료 할 일 중 text 를 포함하는 첫 항목을 반환(없으면 None).

    '재료 주문 완료' 같은 입력에서 완료할 항목을 찾는 데 쓴다.
    """
    text = (text or "").strip()
    for t in get_tasks(task_date, status="pending"):
        desc = t.get("description", "")
        if text and (text in desc or desc in text):
            return t
    return None


# ---------------------------------------------------------------------------
# 일일 요약 로그 (아침 브리핑 / 저녁 리뷰)
# ---------------------------------------------------------------------------

def save_summary(kind, content, summary_date=None):
    """일일 요약을 저장한다(같은 날짜+종류면 갱신). kind: morning|evening."""
    day = summary_date or date.today()
    row = {
        "kind": kind, "content": content,
        "summary_date": day.isoformat() if isinstance(day, date) else day,
    }
    resp = (get_client().table("daily_summaries")
            .upsert(row, on_conflict="summary_date,kind").execute())
    return (resp.data or [None])[0]


def get_summary(kind, summary_date=None):
    """특정 날짜의 요약을 조회한다(없으면 None)."""
    day = summary_date or date.today()
    day = day.isoformat() if isinstance(day, date) else day
    resp = (get_client().table("daily_summaries").select("*")
            .eq("summary_date", day).eq("kind", kind).execute())
    return (resp.data or [None])[0]


# ---------------------------------------------------------------------------
# 직원용 웹서비스 ↔ 집 PC 일꾼 연결 (jobs / worker_status / 답글 초안)
#   먼저 database/schema_v2.sql 을 Supabase SQL Editor 에서 실행해야 한다.
# ---------------------------------------------------------------------------

WORKER_NAME = "home-pc"


def request_collect(by=None):
    """직원이 '리뷰수집'을 눌렀다 → 대기열에 요청 1건 추가. 만든 행 반환.

    이미 대기/진행 중인 요청이 있으면 새로 만들지 않고 그걸 돌려준다
    (버튼 연타로 크롤링이 여러 번 도는 걸 막는다).
    """
    live = (get_client().table("jobs").select("*")
            .eq("kind", "collect")
            .in_("status", ["pending", "running"])
            .order("requested_at", desc=True).limit(1).execute().data)
    if live:
        return live[0]
    row = {"kind": "collect", "status": "pending", "requested_by": by or ""}
    return (get_client().table("jobs").insert(row).execute().data or [None])[0]


def request_collect_all(by=None):
    """'전체 리뷰 수집' — 남아 있는 리뷰를 끝까지 긁어오는 요청.

    평소 수집(collect)과 달리 오래 걸리므로 별도 종류로 둔다. 연타 방지는
    같다(대기·진행 중이면 그걸 재사용).
    """
    live = (get_client().table("jobs").select("*")
            .eq("kind", "collect_all")
            .in_("status", ["pending", "running"])
            .order("requested_at", desc=True).limit(1).execute().data)
    if live:
        return live[0]
    row = {"kind": "collect_all", "status": "pending", "requested_by": by or ""}
    return (get_client().table("jobs").insert(row).execute().data or [None])[0]


def request_wake():
    """웹의 '프로그램 깨우기' — wake 요청 1건을 대기열에 넣는다.

    일꾼이 살아나면(감시견이 5분 안에 되살린다) 이 요청을 done 으로 닫아,
    웹이 '깨어났음'을 확인할 수 있다. 이미 대기 중이면 재사용(연타 방지).
    """
    live = (get_client().table("jobs").select("*")
            .eq("kind", "wake").eq("status", "pending")
            .order("requested_at", desc=True).limit(1).execute().data)
    if live:
        return live[0]
    row = {"kind": "wake", "status": "pending", "requested_by": ""}
    return (get_client().table("jobs").insert(row).execute().data or [None])[0]


def request_regen(review_id, by=None):
    """직원이 'AI 재생성'을 눌렀다 → 집 PC 일꾼에게 초안 재생성을 요청한다.

    jobs 에 payload 컬럼이 없어(DDL 회피) 대상 리뷰 id 를 message 에 담는다 —
    일꾼이 읽은 뒤 finish_job 이 결과 문구로 덮어쓴다.
    같은 리뷰의 재생성이 이미 대기/진행 중이면 재사용(연타 방지).
    """
    live = (get_client().table("jobs").select("*")
            .eq("kind", "regen").eq("message", str(review_id))
            .in_("status", ["pending", "running"])
            .order("requested_at", desc=True).limit(1).execute().data)
    if live:
        return live[0]
    row = {"kind": "regen", "status": "pending",
           "requested_by": by or "", "message": str(review_id)}
    return (get_client().table("jobs").insert(row).execute().data or [None])[0]


def get_review(review_id):
    """리뷰 1건(없으면 None) — 재생성 등 단건 작업용."""
    rows = (get_client().table("reviews").select("*")
            .eq("id", review_id).limit(1).execute().data)
    return rows[0] if rows else None


def latest_job():
    """가장 최근 '리뷰 수집' 요청 1건(없으면 None). 웹에서 진행 상황 표시용.

    블로그 등 다른 종류(kind)의 잡은 제외한다 — 리뷰 화면에 엉뚱한 상태가 뜨지 않게.
    """
    rows = (get_client().table("jobs").select("*")
            .eq("kind", "collect")
            .order("requested_at", desc=True).limit(1).execute().data)
    return rows[0] if rows else None


# 직원이 화면 앞에서 결과를 기다리는 작업 — 리뷰수집(수 분) 뒤에 밀리면
# 3분 폴링 안에 끝나지 않아 '아직 확인이 안 돼요'로 보인다. 먼저 집는다.
INTERACTIVE_JOB_KINDS = ("post", "post_edit", "regen", "wake")


def claim_next_job():
    """집 PC 일꾼이 부른다: 대기 중인 요청 1건을 잡아 running 으로 바꾼다.

    직원이 기다리는 작업(등록·수정·재생성·깨우기)을 먼저 집고, 없을 때만
    나머지(리뷰수집·메뉴수집·블로그)를 오래된 순으로 처리한다. 없으면 None.
    (일꾼이 1대뿐이라 경합은 고려하지 않는다.)
    """
    rows = (get_client().table("jobs").select("*").eq("status", "pending")
            .in_("kind", list(INTERACTIVE_JOB_KINDS))
            .order("requested_at").limit(1).execute().data)
    if not rows:
        rows = (get_client().table("jobs").select("*").eq("status", "pending")
                .order("requested_at").limit(1).execute().data)
    if not rows:
        return None
    job = rows[0]
    (get_client().table("jobs")
     .update({"status": "running", "started_at": datetime.now().astimezone().isoformat()})
     .eq("id", job["id"]).execute())
    return job


def finish_job(job_id, status="done", message=None, result_count=None):
    """요청 처리 종료(done/error) 기록."""
    (get_client().table("jobs").update({
        "status": status,
        "finished_at": datetime.now().astimezone().isoformat(),
        "message": (message or "")[:500],
        "result_count": result_count,
    }).eq("id", job_id).execute())


def worker_ping(state="idle", message=None):
    """집 PC 일꾼이 살아있음을 알린다(주기적으로 호출)."""
    (get_client().table("worker_status").upsert({
        "name": WORKER_NAME,
        "last_seen": datetime.now().astimezone().isoformat(),
        "state": state,
        "message": (message or "")[:300],
    }, on_conflict="name").execute())


def worker_status():
    """집 PC 일꾼 상태(dict) 또는 None. 웹에서 'PC 꺼짐' 표시용."""
    rows = (get_client().table("worker_status").select("*")
            .eq("name", WORKER_NAME).limit(1).execute().data)
    return rows[0] if rows else None


def save_reply_draft(review_id, text, status="drafted"):
    """리뷰 1건의 답글 초안을 저장한다(직원이 고친 것도 여기로).

    ⚠️ ai_draft(AI 원본)는 건드리지 않는다 — 수정률 측정의 기준값이다.
    """
    (get_client().table("reviews").update({
        "reply_draft": text,
        "draft_updated_at": datetime.now().astimezone().isoformat(),
        "reply_status": status,
    }).eq("id", review_id).execute())


# schema_v5.sql 이 추가하는 컬럼들. 아직 Supabase 에 적용 전이면 이 컬럼들만
# 빼고 재시도한다 — 초안 저장·'등록함' 버튼이 마이그레이션 하나 때문에 통째로
# 죽으면 안 된다(수정률 측정만 잠시 포기).
# 에러코드: UPDATE 는 PGRST204(스키마 캐시에 컬럼 없음), SELECT 는 42703.
_V5_COLS = ("ai_draft", "kind", "posted_at")
_MISSING_COLUMN_CODES = ("PGRST204", "42703")


def _update_review(review_id, payload):
    client = get_client()
    try:
        client.table("reviews").update(payload).eq("id", review_id).execute()
    except Exception as e:  # noqa: BLE001 — postgrest APIError
        if getattr(e, "code", None) not in _MISSING_COLUMN_CODES:
            raise
        slim = {k: v for k, v in payload.items() if k not in _V5_COLS}
        if not slim:
            raise
        logger.warning("schema_v5 미적용 — %s 없이 저장 (SQL Editor 에서 "
                       "database/schema_v5.sql 실행 필요)",
                       [k for k in payload if k in _V5_COLS])
        client.table("reviews").update(slim).eq("id", review_id).execute()


def save_ai_draft(review_id, text, kind=None):
    """일꾼이 만든 AI 초안을 저장한다 — 원본(ai_draft)과 편집본(reply_draft)에
    같은 값을 넣고 시작한다. 이후 직원 수정은 reply_draft 만 바꾼다."""
    _update_review(review_id, {
        "ai_draft": text,
        "reply_draft": text,
        "kind": kind,
        "draft_updated_at": datetime.now().astimezone().isoformat(),
        "reply_status": "drafted",
    })


def mark_replied(review_id):
    """실제 게시 완료 표시. 등록 시각을 남겨 수정률 집계에 쓴다.

    (2026-08-06 흐름 변경 후) 자동 등록이 성공했을 때 일꾼이 부른다.
    """
    _update_review(review_id, {
        "reply_status": "posted",
        "posted_at": datetime.now().astimezone().isoformat(),
    })


def mark_approved(review_id):
    """직원이 '수정 완료'를 눌렀다 — 자동 등록 대기 상태로.

    실제 게시는 일꾼이 정해진 시간에 일괄 수행(run_auto_post)하고,
    성공하면 mark_replied 로 posted 가 된다.
    """
    _update_review(review_id, {"reply_status": "approved"})


def mark_drafted(review_id):
    """검토 대기(drafted)로 되돌린다 — 등록 실패 시 카드가 다시 나타나
    직원이 재시도할 수 있게."""
    _update_review(review_id, {"reply_status": "drafted"})


def _request_review_job(kind, review_id, by=None):
    """리뷰 1건 대상 잡(post/post_edit/regen 류)을 넣는다. 연타 방지 재사용.

    jobs 에 payload 컬럼이 없어(DDL 회피) 리뷰 id 는 message 에 담는다 —
    일꾼이 읽은 뒤 finish_job 이 결과 문구로 덮어쓴다.
    """
    live = (get_client().table("jobs").select("*")
            .eq("kind", kind).eq("message", str(review_id))
            .in_("status", ["pending", "running"])
            .order("requested_at", desc=True).limit(1).execute().data)
    if live:
        return live[0]
    row = {"kind": kind, "status": "pending",
           "requested_by": by or "", "message": str(review_id)}
    return (get_client().table("jobs").insert(row).execute().data or [None])[0]


def request_post(review_id, by=None):
    """'답글 등록' 버튼 — 이 리뷰 1건의 즉시 게시 요청(2026-08-10 흐름)."""
    return _request_review_job("post", review_id, by)


def request_post_edit(review_id, by=None):
    """'답글 수정' — 이미 게시된 답글을 새 내용으로 고쳐 재게시 요청."""
    return _request_review_job("post_edit", review_id, by)


def get_job(job_id):
    """잡 1건을 id 로 정확히 가져온다(없으면 None) — 화면 폴링용.

    ⚠️ latest_review_job 은 message 문구로 찾는다. 그런데 finish_job 이
    '알 수 없는 잡 종류: post_edit — 일꾼 업데이트 필요' 처럼 리뷰 id 가 없는
    문구로 덮어쓰면 그 잡을 영영 못 찾아, 화면은 이유도 모른 채 3분 폴링
    끝에 '아직 확인이 안 돼요'만 띄운다(사장님 제보 2026-08-13). id 로 찾으면
    문구가 무엇이든 상태와 사유를 그대로 보여줄 수 있다.
    """
    rows = (get_client().table("jobs").select("*")
            .eq("id", job_id).limit(1).execute().data)
    return rows[0] if rows else None


def latest_review_job(kind, review_id):
    """이 리뷰를 대상으로 한 최근 잡 1건(없으면 None) — 화면 폴링용.

    대기 중엔 message 가 리뷰 id 그대로지만, finish_job 이 결과 문구
    ('리뷰 {id} 답글 수정 완료' 등)로 덮어쓰므로 둘 다 매칭한다.
    """
    rows = (get_client().table("jobs").select("*")
            .eq("kind", kind)
            .or_(f"message.eq.{review_id},message.like.리뷰 {review_id} *")
            .order("requested_at", desc=True).limit(1).execute().data)
    return rows[0] if rows else None


def get_posted_reviews(limit=50):
    """우리가 게시한 답글 목록 — 최근 등록순. '등록한 답글' 화면용."""
    return (get_client().table("reviews").select("*")
            .eq("reply_status", "posted")
            .not_.is_("posted_at", "null")
            .order("posted_at", desc=True)
            .limit(limit).execute().data)


def mark_skipped(review_id):
    """'넘어가기' — 이미 앱에서 직접 등록했거나 답글이 필요 없는 리뷰.

    posted 와 달리 수정률 집계(edit_rate_by_kind)에 안 들어간다:
    우리가 게시한 최종본이 아니라서 AI 학습 데이터로 쓰면 오염된다.
    """
    _update_review(review_id, {"reply_status": "skipped"})


def search_reviews(platform=None, rating=None, replied=None, q=None,
                   limit=50, offset=0, sort="new"):
    """수집된 **모든** 리뷰를 조건으로 찾는다 — 전체 리뷰 관리 화면용.

    Args:
        platform: 'baemin' | 'coupang' | None(전체)
        rating: 1~5 정수면 그 별점만, None 이면 전체.
        replied: True=답글 있는 것, False=답글 없는 것, None=전체.
                 '답글 있음'의 기준은 ①우리가 등록(posted)했거나
                 ②플랫폼에 이미 달려 있는(platform_replied) 경우다.
        q: 리뷰 본문 부분 검색어(없으면 무시).
        sort: 'new'(최신순) | 'old' | 'low'(낮은 별점순)
    Returns: (행 목록, 조건에 맞는 전체 건수)
    """
    def _base():
        s = get_client().table("reviews").select("*", count="exact")
        if platform:
            s = s.eq("platform", platform)
        if rating:
            s = s.eq("rating", int(rating))
        if q:
            s = s.ilike("content", f"%{q}%")
        if replied is True:
            s = s.or_("reply_status.eq.posted,platform_replied.is.true")
        elif replied is False:
            s = (s.neq("reply_status", "posted")
                  .not_.is_("platform_replied", "true"))
        return s

    order = {"old": ("written_date", False),
             "low": ("rating", False)}.get(sort, ("written_date", True))
    try:
        resp = (_base().order(order[0], desc=order[1])
                .range(offset, offset + limit - 1).execute())
    except Exception:  # noqa: BLE001 — platform_replied 미적용 스키마 대비
        logger.exception("리뷰 검색 실패")
        return [], 0
    return resp.data or [], (resp.count if resp.count is not None
                             else len(resp.data or []))


def get_approved_reviews(limit=50):
    """자동 등록을 기다리는(수정 완료된) 리뷰 목록 — 오래된 순."""
    return (get_client().table("reviews").select("*")
            .eq("reply_status", "approved")
            .order("written_date", desc=False)
            .limit(limit).execute().data)


def edit_rate_by_kind(limit=500):
    """유형(kind)별 수정률을 계산한다.

    Returns: {kind: {"n": 등록건수, "edited": 고친건수, "rate": 수정률}}
    등록(posted)됐고 ai_draft 가 있는 리뷰만 대상 — 1단계 배포 이후 데이터.
    공백 차이만 있는 건 '수정 안 함'으로 본다.
    """
    rows = (get_client().table("reviews")
            .select("kind, ai_draft, reply_draft")
            .eq("reply_status", "posted").not_.is_("ai_draft", "null")
            .order("posted_at", desc=True).limit(limit).execute().data)
    stats = {}
    for r in rows:
        k = r.get("kind") or "unknown"
        s = stats.setdefault(k, {"n": 0, "edited": 0})
        s["n"] += 1
        a = " ".join((r.get("ai_draft") or "").split())
        b = " ".join((r.get("reply_draft") or "").split())
        if a != b:
            s["edited"] += 1
    for s in stats.values():
        s["rate"] = round(s["edited"] / s["n"], 3) if s["n"] else 0.0
    return stats


def get_edit_pairs(days=1, limit=50):
    """최근 며칠 사이 직원이 실제로 고친 (AI원본, 최종본) 쌍 — 새벽 공부용."""
    since = (datetime.now().astimezone() - timedelta(days=days)).isoformat()
    rows = (get_client().table("reviews")
            .select("id, platform, kind, rating, content, ai_draft, reply_draft")
            .eq("reply_status", "posted").not_.is_("ai_draft", "null")
            .gte("posted_at", since).limit(limit).execute().data)
    pairs = []
    for r in rows:
        a = " ".join((r.get("ai_draft") or "").split())
        b = " ".join((r.get("reply_draft") or "").split())
        if a and b and a != b:
            pairs.append(r)
    return pairs


def get_platform_reply_examples(days=2, limit=30):
    """플랫폼에 실제 등록돼 있는 답글 예시 — 새벽 답글 공부용.

    최근 수집분 중 답글 본문이 있는 리뷰를 (리뷰, 실제 답글) 쌍으로 준다.
    schema_v6(platform_reply) 미적용이면 조용히 빈 목록.
    """
    since = (datetime.now().astimezone() - timedelta(days=days)).isoformat()
    try:
        return (get_client().table("reviews")
                .select("id, platform, rating, content, menus, platform_reply")
                .not_.is_("platform_reply", "null")
                .gte("collected_at", since)
                .order("written_date", desc=True)
                .limit(limit).execute().data)
    except Exception as e:  # noqa: BLE001
        if getattr(e, "code", None) in _MISSING_COLUMN_CODES:
            return []
        raise


def log_error(source, message, kind=None, path=None, detail=None):
    """오류 1건을 error_log 에 남긴다(집 PC 새벽 점검이 이걸 읽는다).

    ⚠️ 기록 자체가 실패해도 절대 예외를 올리지 않는다 — 로그 때문에 화면이
       죽으면 본말전도다.
    """
    try:
        get_client().table("error_log").insert({
            "source": source,
            "path": (path or "")[:200],
            "kind": (kind or "")[:100],
            "message": (message or "")[:500],
            "detail": (detail or "")[:4000],
        }).execute()
    except Exception:  # noqa: BLE001
        logger.warning("에러 기록 실패(무시): %s", (message or "")[:100])


def get_errors(only_unfixed=True, limit=100):
    """최근 오류 목록. 새벽 자동 점검이 읽는다."""
    q = get_client().table("error_log").select("*")
    if only_unfixed:
        q = q.eq("fixed", False)
    return q.order("at", desc=True).limit(limit).execute().data


def mark_error_fixed(error_id, note=None):
    """처리 완료 표시 — 다음 점검 때 또 보지 않게 한다."""
    (get_client().table("error_log")
     .update({"fixed": True, "note": (note or "")[:500]})
     .eq("id", error_id).execute())


def get_pending_reviews(limit=50):
    """답글이 아직 안 끝난 리뷰를 **오래된 순**으로 가져온다.

    오래된 순인 이유: 플랫폼 답글 기한이 임박한 리뷰부터 처리해야 하는데,
    최신순이면 그런 리뷰가 목록 맨 아래에 묻힌다(UIUX 검토 2026-08-06).
    제외: 우리가 '등록함'으로 표시한 것(reply_status='posted') +
          플랫폼에 이미 사장님 답글이 달려 있는 것(platform_replied=true).
    """
    return (get_client().table("reviews").select("*")
            .in_("reply_status", ["none", "drafted"])
            .or_("platform_replied.is.null,platform_replied.eq.false")
            .order("written_date", desc=False)
            .order("collected_at", desc=False)
            .limit(limit).execute().data)


# ---------------------------------------------------------------------------
# 메뉴 정본 (menu_items / menu_channels / menu_settings)
# ---------------------------------------------------------------------------

# 웹 화면에서 고칠 수 있는 칼럼만 받는다(오타·악의 입력으로 스키마 밖 칼럼이
# 들어오는 것을 막는다).
_MENU_ITEM_COLS = (
    "menu_type", "category", "group_name", "name", "composition", "description",
    "store_price", "delivery_price", "ingredient_cost", "cost_source",
    "store_active", "delivery_active", "sort_order",
)


def menu_all():
    """정본 메뉴 전체 — 카테고리·정렬 순."""
    return (get_client().table("menu_items").select("*")
            .order("sort_order").execute().data)


def menu_channels_all():
    return get_client().table("menu_channels").select("*").execute().data


def menu_settings_all():
    rows = get_client().table("menu_settings").select("*").execute().data
    return {r["key"]: r["value"] for r in rows}


def menu_update_item(sku, fields: dict):
    payload = {k: v for k, v in fields.items() if k in _MENU_ITEM_COLS}
    if not payload:
        return None
    payload["updated_at"] = datetime.utcnow().isoformat() + "Z"
    return (get_client().table("menu_items").update(payload)
            .eq("sku", sku).execute().data)


def menu_upsert_channel(sku, channel, fields: dict):
    allowed = ("name_override", "price_override", "active", "note")
    payload = {k: fields.get(k) for k in allowed if k in fields}
    payload.update({"sku": sku, "channel": channel,
                    "updated_at": datetime.utcnow().isoformat() + "Z"})
    return (get_client().table("menu_channels")
            .upsert(payload, on_conflict="sku,channel").execute().data)


def menu_set_setting(key, value):
    return (get_client().table("menu_settings")
            .upsert({"key": key, "value": value,
                     "updated_at": datetime.utcnow().isoformat() + "Z"},
                    on_conflict="key").execute().data)


def request_menu_collect(by=None):
    """직원이 '채널수집'을 눌렀다 — 집 PC 일꾼에게 채널 메뉴 수집 요청."""
    live = (get_client().table("jobs").select("*")
            .eq("kind", "menu_collect")
            .in_("status", ["pending", "running"])
            .order("requested_at", desc=True).limit(1).execute().data)
    if live:
        return live[0]
    row = {"kind": "menu_collect", "status": "pending", "requested_by": by or ""}
    return (get_client().table("jobs").insert(row).execute().data or [None])[0]


def normalize_menu_name(name):
    """채널 표기 차이를 무시하기 위한 메뉴명 정규화.

    '[SET] 베이글 샌드위치+음료' 와 '베이글샌드위치 + 음료' 를 같게 본다.
    """
    s = re.sub(r"\[[^\]]*\]", "", name or "")          # [SET] [COUPLE] 등 제거
    s = re.sub(r"[^0-9a-zA-Z가-힣]", "", s)             # 공백·기호 제거
    return s.lower()


def save_menu_snapshots(channel, rows):
    """채널 스냅샷 갈아끼우기 + 정본 SKU 자동 매칭."""
    sb = get_client()
    masters = sb.table("menu_items").select("sku,name").execute().data
    overrides = (sb.table("menu_channels").select("sku,name_override")
                 .eq("channel", channel).execute().data)
    by_norm = {}
    for m in masters:
        by_norm.setdefault(normalize_menu_name(m["name"]), m["sku"])
    for o in overrides:                       # 채널 예외 이름이 우선
        if o.get("name_override"):
            by_norm[normalize_menu_name(o["name_override"])] = o["sku"]

    sb.table("menu_channel_snapshots").delete().eq("channel", channel).execute()
    payload = []
    for r in rows:
        payload.append({
            "channel": channel,
            "menu_name": r["menu_name"][:200],
            "price": r.get("price"),
            "category": r.get("category"),
            "description": (r.get("description") or None),
            "matched_sku": by_norm.get(normalize_menu_name(r["menu_name"])),
            "raw": r.get("raw"),
        })
    if payload:
        sb.table("menu_channel_snapshots").insert(payload).execute()
    return len(payload)


def order_stats(days=90):
    """채널별 실측 객단가 — orders 테이블의 주문금액 평균.

    배달비는 주문당 1회 발생하므로, '메뉴 하나가 배달비를 얼마나 짊어지는가'를
    보려면 실제 객단가가 있어야 한다. 추정 대신 실주문에서 뽑는다.
    취소·환불 주문은 금액이 0이거나 음수로 들어올 수 있어 0 이하를 제외한다.
    """
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = (get_client().table("orders").select("platform,price")
            .gte("ordered_date", since).limit(10000).execute().data)
    agg = {}
    for r in rows:
        p = r.get("price")
        if not isinstance(p, (int, float)) or p <= 0:
            continue
        a = agg.setdefault(r.get("platform") or "?", {"sum": 0, "n": 0})
        a["sum"] += p
        a["n"] += 1
    return {
        "days": days,
        "aov": {k: round(v["sum"] / v["n"]) for k, v in agg.items() if v["n"]},
        "orders": {k: v["n"] for k, v in agg.items()},
    }


# ---------------------------------------------------------------------------
# 자재(원부자재) · 레시피 · 원가 자동 계산
# ---------------------------------------------------------------------------

_ING_COLS = ("name", "unit", "pack_qty", "pack_cost", "category", "supplier", "note")


def ingredients_all():
    return (get_client().table("ingredients").select("*")
            .order("category").order("name").execute().data)


def recipes_all():
    return get_client().table("menu_recipes").select("*").execute().data


class DuplicateIngredient(ValueError):
    """같은 이름의 자재가 이미 있을 때."""


def _norm_ing_name(s):
    """이름 비교용 정규화 — 공백·대소문자 무시('플레인 베이글'='플레인베이글')."""
    return re.sub(r"\s+", "", (s or "")).lower()


def ingredient_upsert(fields, ing_id=None):
    payload = {k: fields.get(k) for k in _ING_COLS if k in fields}
    payload["updated_at"] = datetime.utcnow().isoformat() + "Z"
    sb = get_client()

    # 이름 중복 차단 — 단위가 달라도 같은 이름은 하나만 둔다(사장님 요청).
    name = payload.get("name")
    if name:
        target = _norm_ing_name(name)
        allrows = ingredients_all()
        me = next((r for r in allrows if r["id"] == ing_id), None) if ing_id else None
        # 이름을 안 바꾼 수정(분류·발주처만 고침)은 검사하지 않는다 — 예전부터
        # 이름이 겹쳐 있던 자재까지 통째로 수정 불가가 되기 때문.
        unchanged = me is not None and _norm_ing_name(me["name"]) == target
        for row in ([] if unchanged else allrows):
            if row["id"] == ing_id:
                continue                      # 자기 자신은 제외(단순 수정)
            if _norm_ing_name(row["name"]) == target:
                raise DuplicateIngredient(
                    f"'{row['name']}'({row['unit']})가 이미 있습니다. "
                    f"새로 만들지 말고 그 자재를 수정해 주세요.")
    def _write(p):
        if ing_id:
            return sb.table("ingredients").update(p).eq("id", ing_id).execute().data
        return sb.table("ingredients").upsert(
            p, on_conflict="name,unit").execute().data

    try:
        rows = _write(payload)
    except Exception as e:  # noqa: BLE001 — 008 마이그레이션 전이면 supplier 컬럼이 없다
        if getattr(e, "code", None) not in _MISSING_COLUMN_CODES:
            raise
        logger.warning("008 미적용 — supplier 없이 저장 "
                       "(SQL Editor 에서 supabase/migrations/008_ingredient_supplier.sql 실행)")
        payload.pop("supplier", None)
        rows = _write(payload)
    row = (rows or [None])[0]
    # 응답에 id 가 없을 수 있다(설정에 따라 빈 응답). 그때는 다시 찾아서 채운다 —
    # 호출부가 id 로 '이 자재를 쓰는 메뉴'를 재계산하기 때문.
    if not row or "id" not in row:
        q = sb.table("ingredients").select("*")
        if ing_id:
            q = q.eq("id", ing_id)
        else:
            q = q.eq("name", payload.get("name")).eq("unit", payload.get("unit"))
        found = q.execute().data
        if found:
            row = found[0]
    return row


def ingredient_delete(ing_id):
    """자재 삭제 — 레시피에서 쓰는 중이면 DB 제약(restrict)으로 실패한다."""
    get_client().table("ingredients").delete().eq("id", ing_id).execute()


def recipe_upsert(sku, ingredient_id, qty):
    return (get_client().table("menu_recipes").upsert(
        {"sku": sku, "ingredient_id": ingredient_id, "qty": qty,
         "updated_at": datetime.utcnow().isoformat() + "Z"},
        on_conflict="sku,ingredient_id").execute().data or [None])[0]


def recipe_delete(rid):
    rows = (get_client().table("menu_recipes").select("sku")
            .eq("id", rid).execute().data)
    get_client().table("menu_recipes").delete().eq("id", rid).execute()
    return rows[0]["sku"] if rows else None


def recompute_costs(skus=None, force=False):
    """레시피 기반으로 menu_items.ingredient_cost 재계산.

    Args:
        skus: 대상 SKU 목록(None=레시피가 있는 전 메뉴).
        force: True 면 '웹에서 직접 입력' 원가도 덮어쓴다. 레시피를 사람이
               직접 고친 직후에는 True 로 부른다(레시피가 더 최신 의사표시).
    Returns: 갱신된 {sku: cost}
    """
    sb = get_client()
    ings = {i["id"]: i for i in sb.table("ingredients").select("*").execute().data}
    q = sb.table("menu_recipes").select("*")
    if skus:
        q = q.in_("sku", list(skus))
    lines = q.execute().data
    by_sku = {}
    for ln in lines:
        by_sku.setdefault(ln["sku"], []).append(ln)
    if not by_sku:
        return {}
    items = (sb.table("menu_items").select("sku,ingredient_cost,cost_source")
             .in_("sku", list(by_sku)).execute().data)
    src_by = {i["sku"]: (i.get("cost_source") or "") for i in items}
    updated = {}
    stamp = f"레시피 자동계산({date.today().isoformat()})"
    for sku, lns in by_sku.items():
        if not force and src_by.get(sku, "").startswith("웹에서 직접 입력"):
            continue
        total = 0.0
        for ln in lns:
            ing = ings.get(ln["ingredient_id"])
            if not ing or not ing.get("pack_qty"):
                continue
            total += float(ln["qty"]) * float(ing["pack_cost"]) / float(ing["pack_qty"])
        cost = round(total, 1)
        sb.table("menu_items").update(
            {"ingredient_cost": cost, "cost_source": stamp}).eq("sku", sku).execute()
        updated[sku] = cost
    return updated


def seed_ingredients_bulk(spec):
    """자재·레시피 시드를 일괄 주입(웹에서 버튼 1회용).

    이미 있는 자재의 가격은 건드리지 않고, 없는 것만 넣는다.
    레시피도 없는 라인만 추가. 끝으로 관련 메뉴 원가를 재계산한다
    ('웹에서 직접 입력' 원가는 보존).
    """
    sb = get_client()
    existing = {(i["name"], i["unit"]) for i in ingredients_all()}
    new_rows = [{k: ing.get(k) for k in _ING_COLS}
                for ing in spec["ingredients"]
                if (ing["name"], ing["unit"]) not in existing]
    if new_rows:
        sb.table("ingredients").upsert(new_rows, on_conflict="name,unit").execute()

    ing_id = {(i["name"], i["unit"]): i["id"] for i in ingredients_all()}
    valid = {i["sku"] for i in sb.table("menu_items").select("sku").execute().data}
    have = {(r["sku"], r["ingredient_id"]) for r in recipes_all()}
    # 같은 메뉴에 같은 자재가 두 줄이면 사용량을 합친다 — 한 upsert 안에
    # 키가 중복되면 Postgres 가 21000 오류를 낸다(실사고 2026-08).
    merged = {}
    for ln in spec["recipes"]:
        iid = ing_id.get((ln["ingredient"], ln["unit"]))
        if not iid or ln["sku"] not in valid or (ln["sku"], iid) in have:
            continue
        merged[(ln["sku"], iid)] = merged.get((ln["sku"], iid), 0) + ln["qty"]
    lines = [{"sku": s, "ingredient_id": i, "qty": round(q, 3)}
             for (s, i), q in merged.items()]
    if lines:
        sb.table("menu_recipes").upsert(
            lines, on_conflict="sku,ingredient_id").execute()
    touched = sorted({ln["sku"] for ln in lines})
    updated = recompute_costs(touched) if touched else {}
    return {"ingredients_added": len(new_rows), "lines_added": len(lines),
            "recomputed": len(updated)}


def skus_using_ingredient(ing_id):
    rows = (get_client().table("menu_recipes").select("sku")
            .eq("ingredient_id", ing_id).execute().data)
    return sorted({r["sku"] for r in rows})


def menu_snapshots_all():
    return (get_client().table("menu_channel_snapshots").select("*")
            .order("channel").execute().data)


if __name__ == "__main__":
    # 단독 실행: 연결 및 테이블 존재 확인
    logging.basicConfig(level=logging.INFO)
    try:
        get_client().table("orders").select("id").limit(1).execute()
        get_client().table("tasks").select("id").limit(1).execute()
        print("✅ 연결 OK — orders/reviews/tasks/daily_summaries 접근 가능")
    except Exception as e:  # noqa: BLE001
        print("❌ 테이블 확인 실패:", str(e)[:160])
        print("→ database/schema.sql 을 Supabase SQL Editor 에서 실행하세요.")
