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
from datetime import date, datetime

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
)

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
        rows.append(r)
    if not rows:
        return 0
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
            .in_("status", ["pending", "running"])
            .order("requested_at", desc=True).limit(1).execute().data)
    if live:
        return live[0]
    row = {"kind": "collect", "status": "pending", "requested_by": by or ""}
    return (get_client().table("jobs").insert(row).execute().data or [None])[0]


def latest_job():
    """가장 최근 수집 요청 1건(없으면 None). 웹에서 진행 상황 표시용."""
    rows = (get_client().table("jobs").select("*")
            .order("requested_at", desc=True).limit(1).execute().data)
    return rows[0] if rows else None


def claim_next_job():
    """집 PC 일꾼이 부른다: 대기 중인 요청 1건을 잡아 running 으로 바꾼다.

    없으면 None. (일꾼이 1대뿐이라 경합은 고려하지 않는다.)
    """
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
    """리뷰 1건의 답글 초안을 저장한다(직원이 고친 것도 여기로)."""
    (get_client().table("reviews").update({
        "reply_draft": text,
        "draft_updated_at": datetime.now().astimezone().isoformat(),
        "reply_status": status,
    }).eq("id", review_id).execute())


def mark_replied(review_id):
    """'답글 등록함' 표시 → 목록에서 내려간다."""
    (get_client().table("reviews").update({"reply_status": "posted"})
     .eq("id", review_id).execute())


def get_pending_reviews(limit=50):
    """답글이 아직 안 끝난 리뷰(none/drafted)를 최신순으로 가져온다."""
    return (get_client().table("reviews").select("*")
            .in_("reply_status", ["none", "drafted"])
            .order("written_date", desc=True)
            .order("collected_at", desc=True)
            .limit(limit).execute().data)


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
