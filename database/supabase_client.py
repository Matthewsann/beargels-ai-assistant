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
    # 우리가 '다시 쓰기'로 큐에 올려 둔 리뷰(drafted)는 크롤 결과로 큐에서
    # 빼지 않는다. 플랫폼엔 (잘못된) 답글이 남아 있어 platform_replied=true 로
    # 돌아오는데, 그러면 재작성 대상이 목록에서 사라진다(2026-08-24).
    try:
        keep = {(r["platform"], r["review_no"]) for r in
                (get_client().table("reviews").select("platform,review_no")
                 .eq("reply_status", "drafted").limit(500).execute().data or [])}
        for r in rows:
            if (r.get("platform"), r.get("review_no")) in keep:
                r.pop("platform_replied", None)
    except Exception:  # noqa: BLE001 — 보호막이 저장을 막으면 안 된다
        logger.warning("drafted 보호 조회 실패(무시)")
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


def last_collect_at():
    """마지막으로 리뷰 수집이 **성공한** 시각(ISO 문자열) 또는 None.

    홈 화면이 "이 숫자는 언제 기준인가"를 알려줄 때 쓴다. 화면을 열 때마다
    크롤링할 수는 없어서(수 분 걸린다) 숫자는 항상 마지막 수집 시점의 것이다.
    실패한 수집은 데이터를 갱신하지 못했으므로 세지 않는다.
    """
    rows = (get_client().table("jobs").select("finished_at")
            .in_("kind", ["collect", "collect_all"])
            .eq("status", "done")
            .not_.is_("finished_at", "null")
            .order("finished_at", desc=True).limit(1).execute().data)
    return rows[0].get("finished_at") if rows else None


# 직원이 화면 앞에서 결과를 기다리는 작업 — 리뷰수집(수 분) 뒤에 밀리면
# 3분 폴링 안에 끝나지 않아 '아직 확인이 안 돼요'로 보인다. 먼저 집는다.
INTERACTIVE_JOB_KINDS = ("post", "post_edit", "regen", "wake")


def claim_next_job(interactive_only=False):
    """집 PC 일꾼이 부른다: 대기 중인 요청 1건을 잡아 running 으로 바꾼다.

    직원이 기다리는 작업(등록·수집·재생성·깨우기)을 먼저 집고, 없을 때만
    나머지(리뷰수집·메뉴수집·블로그)를 오래된 순으로 처리한다. 없으면 None.
    (일꾼이 1대뿐이라 경합은 고려하지 않는다.)

    interactive_only: True 면 **직원이 화면 앞에서 기다리는 잡만** 본다 —
        일꾼의 빠른 박자(1~2초 주기)용. 조회 1회짜리 가벼운 확인이라 자주
        불러도 부담이 없다. 수집·블로그 같은 배경 잡은 느린 박자(15초)가
        집는다(급하지 않고, 그쪽까지 자주 물으면 왕복이 배로 는다).
    """
    rows = (get_client().table("jobs").select("*").eq("status", "pending")
            .in_("kind", list(INTERACTIVE_JOB_KINDS))
            .order("requested_at").limit(1).execute().data)
    if not rows and not interactive_only:
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


# 이미 결론이 난 상태 — 초안 자동저장이 여기로 되돌리면 안 된다.
_SETTLED_STATUSES = ("posted", "approved", "skipped", "scheduled")

# '아직 손님에게 안 나간 = 답글 화면에 남아야 할' 상태들.
# scheduled(아침에 등록 예약)도 여기 든다 — 목록에서 사라지면 직원이 예약한
# 걸 확인·취소할 길이 없고, 할 일 배지 숫자와 목록 길이도 어긋난다.
_PENDING_STATUSES = ["none", "drafted", "scheduled"]


def save_reply_draft(review_id, text, status="drafted"):
    """리뷰 1건의 답글 초안을 저장한다(직원이 고친 것도 여기로).

    ⚠️ ai_draft(AI 원본)는 건드리지 않는다 — 수정률 측정의 기준값이다.
    ⚠️ **이미 등록(posted)·등록대기(approved)·넘어감(skipped)인 리뷰의 상태는
       내리지 않는다.** 초안칸은 칸에서 나갈 때 자동저장되는데, 등록 직후
       화면을 건드리면 그 저장이 상태를 'drafted' 로 되돌려 리뷰가 '할 일'
       목록에 되살아났다 → 직원이 한 번 더 등록해 **고객에게 중복 답글**이
       나갈 수 있다(2026-08-16 실제 발생, 리뷰 2783).
    """
    payload = {
        "reply_draft": text,
        "draft_updated_at": datetime.now().astimezone().isoformat(),
    }
    cur = (get_review(review_id) or {}).get("reply_status")
    if cur not in _SETTLED_STATUSES:
        payload["reply_status"] = status
    else:
        logger.info("리뷰 %s 는 이미 '%s' — 본문만 저장하고 상태는 유지",
                    review_id, cur)
    get_client().table("reviews").update(payload).eq("id", review_id).execute()


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


def save_ai_draft(review_id, text, kind=None, keep_status=False):
    """일꾼이 만든 AI 초안을 저장한다 — 원본(ai_draft)과 편집본(reply_draft)에
    같은 값을 넣고 시작한다. 이후 직원 수정은 reply_draft 만 바꾼다.

    keep_status: 이미 등록(posted)한 답글을 **고치려고** 다시 만든 경우에는
        상태를 건드리지 않는다. 예전엔 무조건 'drafted' 로 되돌려서, 등록한
        답글 화면에서 AI 재생성을 누르면 그 답글이 목록에서 사라졌다
        (2026-08-24).
    """
    patch = {
        "ai_draft": text,
        "reply_draft": text,
        "kind": kind,
        "draft_updated_at": datetime.now().astimezone().isoformat(),
    }
    if not keep_status:
        patch["reply_status"] = "drafted"
    _update_review(review_id, patch)


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


def mark_scheduled(review_id):
    """'아침에 등록' — 지금 올리지 않고 다음 아침 슬롯까지 재워 둔다.

    왜 approved 를 안 쓰나: approved 는 '지금 등록 대기'라, 일꾼의 자동복구
    (rescue_stuck_approved)가 잡 없는 approved 를 발견하면 **즉시** 줄을
    세운다. 새벽에 쓴 답글이 새벽에 나가면 안 되므로 상태를 따로 둔다.
    (reply_status 는 자유 텍스트 컬럼이라 표 변경 없이 값만 늘리면 된다.)
    """
    _update_review(review_id, {"reply_status": "scheduled"})


def get_scheduled_reviews(limit=200):
    """아침 일괄 등록을 기다리는 리뷰 — 오래된 순(기한 임박한 것부터)."""
    return (get_client().table("reviews").select("*")
            .eq("reply_status", "scheduled")
            .order("written_date", desc=False).order("review_no", desc=False)
            .limit(limit).execute().data)


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


def _search_filter(q):
    """검색어 → PostgREST or_ 조건 문자열.

    - 리뷰 본문뿐 아니라 **작성자**도 찾는다(닉네임으로 찾는 일이 잦다).
    - 띄어쓰기를 무시한다: '크림 치즈'로도 '크림치즈'가 걸리도록 공백을 뺀
      검색어를 함께 본다(사장님 보고 2026-08-13 — 0건이 나왔다).
    - or_ 문법을 깨뜨리는 쉼표·괄호는 제거한다.
    """
    base = re.sub(r"[,()]", " ", q or "").strip()
    terms = {base, base.replace(" ", "")} - {""}
    parts = []
    for t in terms:
        # raw(플랫폼 원본)에는 주문 메뉴명이 들어 있다 — '크림치즈' 로 그 메뉴를
        # 시킨 리뷰를 모아 볼 수 있게 함께 훑는다(2026-08-23).
        parts += [f"content.ilike.%{t}%", f"author.ilike.%{t}%",
                  f"raw.ilike.%{t}%"]
    return ",".join(parts)


def search_reviews(platform=None, rating=None, replied=None, q=None,
                   limit=50, offset=0, sort="new", days=None, kind=None,
                   rating_max=None, source=None, count_only=False,
                   pending_only=False, has_draft=None):
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
    def _base(select="*"):
        s = get_client().table("reviews").select(select, count="exact")
        if platform:
            s = s.eq("platform", platform)
        if rating:
            s = s.eq("rating", int(rating))
        if rating_max:                      # '4점 이하'처럼 범위로 보기
            s = s.lte("rating", int(rating_max))
        if kind:                            # 답글 유형(불만·질문·칭찬…)
            s = s.in_("kind", list(kind) if isinstance(kind, (list, tuple))
                      else [kind])
        if days:                            # 최근 N일
            since = (datetime.now().date() - timedelta(days=int(days))).isoformat()
            s = s.gte("written_date", since)
        if q:
            s = s.or_(_search_filter(q))
        # 답글을 '누가' 달았는지 — 우리 페이지로 등록한 것과 직원이 앱에서
        # 직접 단 것을 구분해야 학습 재료가 어디서 새는지 보인다.
        if source == "ours":
            s = s.eq("reply_status", "posted")
        elif source == "app":
            s = s.neq("reply_status", "posted").eq("platform_replied", True)
        if replied is True:
            s = s.or_("reply_status.eq.posted,platform_replied.is.true")
        elif replied is False:
            s = (s.neq("reply_status", "posted")
                  .not_.is_("platform_replied", "true"))
        # '지금 답글 달 것'(답글 화면)의 조건 — get_pending_reviews 와 같은 기준.
        # 필터를 화면마다 따로 만들지 않고 한 곳에서 쓰기 위해 여기에 뒀다.
        if pending_only:
            s = (s.in_("reply_status", _PENDING_STATUSES)
                  .or_("platform_replied.is.null,platform_replied.eq.false"))
        if has_draft is True:
            s = s.not_.is_("reply_draft", "null")
        elif has_draft is False:
            s = s.is_("reply_draft", "null")
        return s

    try:
        if count_only:                      # 요약용 — 별점만 받아 온다
            resp = _base("rating").limit(limit).execute()
            return resp.data or [], (resp.count if resp.count is not None
                                     else len(resp.data or []))
        resp = (_order_reviews(_base(), sort)
                .range(offset, offset + limit - 1).execute())
    except Exception:  # noqa: BLE001 — platform_replied 미적용 스키마 대비
        logger.exception("리뷰 검색 실패")
        return [], 0
    return resp.data or [], (resp.count if resp.count is not None
                             else len(resp.data or []))


def _order_reviews(q, sort="new"):
    """리뷰 목록 정렬 — **배민/쿠팡 앱 화면과 같은 순서**로 맞춘다.

    written_date 는 시각이 없는 '날짜'라, 같은 날 리뷰끼리는 순서가 정해지지
    않는다(Postgres 는 동점 행의 순서를 보장하지 않는다). 그래서 화면을 열
    때마다·재수집할 때마다 같은 날 리뷰가 뒤섞여, 플랫폼 앱 순서와 달랐다
    (사장님 보고 2026-08-21). 리뷰번호로 2차 정렬해 고친다:

      - 배민 review_no = 'YYYYMMDD' + 8자리 일련번호 (16자리 고정)
      - 쿠팡 review_no = orderReviewId, 작성 시간순 증가 (9자리)

    둘 다 자릿수가 고정이라 문자열 정렬 = 작성 시간순이다. (쿠팡 번호가 언젠가
    10자리가 되면 그때만 자릿수 보정이 필요하다.)

    sort: 'new'(최신순) | 'old'(오래된순) | 'low'(낮은 별점순)
    """
    # 등록한 답글 화면은 '언제 등록했는지'가 기준이다(리뷰 작성일이 아니라).
    if sort in ("posted", "posted_old"):
        return q.order("posted_at", desc=(sort == "posted"))
    if sort in ("low", "high"):
        return (q.order("rating", desc=(sort == "high"))
                 .order("written_date", desc=True).order("review_no", desc=True))
    desc = sort != "old"
    return q.order("written_date", desc=desc).order("review_no", desc=desc)


# '관리 필요' 리뷰 판정 — 별점이 만점이 아니거나, 답글 유형이 불만/민감인 것.
# kind 는 초안 생성 때 붙는다(없는 옛 리뷰는 별점으로만 걸린다).
CS_KINDS = ("complaint", "escalate")

# '문제 리뷰'의 기준 별점 (사장님 확정 2026-08-27).
# 예전엔 5점 미만(=★4 포함)이었는데, ★4 는 대부분 만족 리뷰다 — "포장 깔끔,
# 양도 적절, 맛있게 잘 먹었습니다" 같은 칭찬 글이 '문제'로 잡혀 배지 숫자가
# 부풀었다. 진짜 손봐야 하는 건 ★3 이하이거나 불만·민감 유형이다.
# ★4 만 따로 보고 싶으면 전체 리뷰 화면의 '★4 이하' 필터를 쓴다.
ATTENTION_MAX_RATING = int(os.getenv("ATTENTION_MAX_RATING", "3"))
# 답글 기한(플랫폼 30일)이 지난 리뷰는 눌러도 등록이 안 된다 — 큐에 남겨
# 두면 '할 일'처럼 보이기만 한다.
ATTENTION_WINDOW_DAYS = int(os.getenv("REPLY_EDIT_DAYS", "30"))


def get_attention_reviews(platform=None, mode="all", limit=30, offset=0,
                          sort="new", days=None, replied=None, select="*"):
    """별점 5점 미만 + CS(불만·민감) 리뷰만 모아 본다 — 관리 필요 화면용.

    Args:
        mode: 'all'(둘 다) | 'low'(별점 5점 미만만) | 'cs'(CS 유형만)
        select: 배지에서 건수만 셀 때는 "id" 로 좁혀 payload 를 줄인다.
    Returns: (행 목록, 조건에 맞는 전체 건수)
    """
    try:
        q = get_client().table("reviews").select(select, count="exact")
        if platform:
            q = q.eq("platform", platform)
        kinds = ",".join(CS_KINDS)
        if mode == "low":
            q = q.lte("rating", ATTENTION_MAX_RATING)
        elif mode == "cs":
            q = q.in_("kind", list(CS_KINDS))
        else:
            q = q.or_(f"rating.lte.{ATTENTION_MAX_RATING},kind.in.({kinds})")
        if days:                          # 최근 N일만 (요즘 흐름 보기)
            since = (datetime.now().date() - timedelta(days=int(days))).isoformat()
            q = q.gte("written_date", since)
        if replied is True:
            q = q.or_("reply_status.eq.posted,platform_replied.is.true")
        elif replied is False:            # 아직 답글 안 단 문제 리뷰 = 제일 급함
            # ⚠️ '지금 손댈 수 있는 것'만 남긴다(사장님 지적 2026-08-27:
            #    넘김 처리했고 기한도 320일 지난 리뷰가 '문제 3건'에 껴 있었다).
            #    · 넘김(skipped) = 직원이 판단을 끝낸 건 — 다시 올리지 않는다
            #    · 기한 지난 것  = 눌러도 등록이 안 된다
            since = (datetime.now().date()
                     - timedelta(days=ATTENTION_WINDOW_DAYS)).isoformat()
            q = (q.neq("reply_status", "posted")
                  .neq("reply_status", "skipped")
                  .not_.is_("platform_replied", "true")
                  .gte("written_date", since))
        resp = (_order_reviews(q, sort)
                .range(offset, offset + limit - 1).execute())
    except Exception:  # noqa: BLE001 — 조회 실패가 화면을 막지 않게
        logger.exception("관리 필요 리뷰 조회 실패")
        return [], 0
    return resp.data or [], (resp.count if resp.count is not None
                             else len(resp.data or []))


# ---------------------------------------------------------------------------
# 화면용 숫자 세기 — 목록을 통째로 받지 않는다
# ---------------------------------------------------------------------------
# 왜: 대시보드가 '몇 건인지'만 쓰면서 리뷰 500건을 통째로(180KB) 받아오고
#     있었다. PythonAnywhere ↔ Supabase 왕복이 한 번에 0.3~1.3초라, 화면
#     하나에 5초가 걸렸다(2026-08-21 서버 실측). 개수는 서버에서 세게 한다.

def _pending_base(select="id"):
    """답글이 아직 안 끝난 리뷰의 공통 조건(get_pending_reviews 와 같은 기준)."""
    return (get_client().table("reviews").select(select, count="exact")
            .in_("reply_status", _PENDING_STATUSES)
            .or_("platform_replied.is.null,platform_replied.eq.false"))


def _count(q) -> int:
    """조건에 맞는 건수만 받아온다(행은 안 받는다)."""
    try:
        return q.limit(1).execute().count or 0
    except Exception:  # noqa: BLE001 — 숫자 하나 때문에 화면이 죽으면 안 된다
        logger.exception("건수 조회 실패")
        return 0


def count_pending(with_draft=None, platform=None, escalate=False) -> int:
    """등록해야 할 리뷰 건수.

    with_draft: True=초안 있는 것만, False=초안 없는 것만, None=전체
    escalate:   True=사장님이 직접 대응할 민감 리뷰만
    """
    q = _pending_base()
    if platform:
        q = q.eq("platform", platform)
    if with_draft is True:
        q = q.not_.is_("reply_draft", "null")
    elif with_draft is False:
        q = q.is_("reply_draft", "null")
    if escalate:
        q = q.or_("kind.eq.escalate,reply_draft.like.⚠️%")
    return _count(q)


def count_by_status(status) -> int:
    """reply_status 가 그 상태인 리뷰 건수(approved/posted 등)."""
    return _count(get_client().table("reviews").select("id", count="exact")
                  .eq("reply_status", status))


def oldest_pending_date():
    """가장 오래 기다린(초안 있는) 리뷰의 작성일 — 기한 감각용. 없으면 None."""
    try:
        rows = (_pending_base("written_date").not_.is_("reply_draft", "null")
                .order("written_date", desc=False).limit(1).execute().data)
    except Exception:  # noqa: BLE001
        return None
    return (rows[0].get("written_date") if rows else None)


def get_approved_reviews(limit=50):
    """자동 등록을 기다리는(수정 완료된) 리뷰 목록 — 오래된 순."""
    return (get_client().table("reviews").select("*")
            .eq("reply_status", "approved")
            .order("written_date", desc=False).order("review_no", desc=False)
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


# AI 초안을 직원이 얼마나 고쳤는지 = 답글 품질의 성적표. 이 숫자가 충분히
# 쌓이고(표본) 충분히 낮아지면(수정률), 그때 API 없는 자체 모델로 갈아탈
# 재료가 된다(사장님 결정 2026-08-21: 데이터부터 모으기).
LEARNING_TARGET_PAIRS = 500


def learning_progress(limit=1000) -> dict:
    """학습 재료 현황 — {pairs, edited, rate, target}.

    pairs: 우리가 등록한 답글 중 'AI 원본'이 남아 있는 건수(=학습 쌍).
    rate:  그중 직원이 손댄 비율(공백 차이만 있으면 안 고친 것으로 본다).
    """
    try:
        rows = (get_client().table("reviews").select("ai_draft, reply_draft")
                .eq("reply_status", "posted").not_.is_("ai_draft", "null")
                .limit(limit).execute().data)
    except Exception:  # noqa: BLE001 — 숫자 하나 때문에 화면이 죽지 않게
        logger.exception("학습 현황 조회 실패")
        return {"pairs": 0, "edited": 0, "rate": 0.0,
                "target": LEARNING_TARGET_PAIRS}
    edited = sum(1 for r in rows
                 if " ".join((r.get("ai_draft") or "").split())
                 != " ".join((r.get("reply_draft") or "").split()))
    n = len(rows)
    return {"pairs": n, "edited": edited,
            "rate": round(edited / n, 3) if n else 0.0,
            "target": LEARNING_TARGET_PAIRS}


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
            .in_("reply_status", _PENDING_STATUSES)
            .or_("platform_replied.is.null,platform_replied.eq.false")
            .order("written_date", desc=False)
            # 같은 날 리뷰의 순서 — collected_at 은 '마지막 수집 시각'이라
            # 재수집 때마다 값이 바뀌어 순서가 흔들렸다(플랫폼 앱과 불일치).
            # 리뷰번호는 작성 순서대로라 순서가 고정된다(_order_reviews 참고).
            .order("review_no", desc=False)
            .limit(limit).execute().data)


# ---------------------------------------------------------------------------
# 메뉴 정본 (menu_items / menu_channels / menu_settings)
# ---------------------------------------------------------------------------

# 웹 화면에서 고칠 수 있는 칼럼만 받는다(오타·악의 입력으로 스키마 밖 칼럼이
# 들어오는 것을 막는다).
_MENU_ITEM_COLS = (
    "menu_type", "category", "group_name", "name", "composition", "description",
    # 플랫폼(네이버·배민·쿠팡·토스 키오스크)에 넣을 짧은 소개. description 은
    # 정본용 긴 글이라 그대로 붙이기엔 길다.
    "intro_ko", "intro_en",
    # 키오스크·영문 메뉴판에 거는 이름(문장이 아니라 이름)
    "name_en",
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


def get_setting(key, default=None):
    """menu_settings 에서 값 하나만 읽는다.

    이름과 달리 메뉴 전용 표가 아니라 범용 key-value 창고로 쓴다
    (예: 홈 화면 담당자 'home_owners'). 새 표를 만들려면 사장님이
    SQL 을 직접 실행해야 해서, 있는 표를 재사용한다.
    """
    rows = (get_client().table("menu_settings").select("value")
            .eq("key", key).limit(1).execute().data)
    return rows[0]["value"] if rows else default


def menu_update_item(sku, fields: dict):
    payload = {k: v for k, v in fields.items() if k in _MENU_ITEM_COLS}
    if not payload:
        return None
    payload["updated_at"] = datetime.utcnow().isoformat() + "Z"
    return (get_client().table("menu_items").update(payload)
            .eq("sku", sku).execute().data)


# ── 분류(카테고리) 관리 ────────────────────────────────────────────
# 분류는 메뉴마다 붙은 글자라, 이름을 바꾸려면 그 분류의 메뉴를 전부 고쳐야 한다.
# 게다가 목표 원가율이 분류 '이름'을 열쇠로 저장돼 있어서, 메뉴만 고치면
# 목표값이 통째로 미아가 되고 전부 '기타 35%' 로 떨어진다 — 함께 옮긴다.
_CAT_SETTING_KEYS = ("target_cost_rates",)


def category_rename(old: str, new: str) -> dict:
    old, new = (old or "").strip(), (new or "").strip()
    if not old or not new:
        raise ValueError("분류 이름이 비었습니다")
    if old == new:
        return {"moved": 0}
    cli = get_client()
    moved = (cli.table("menu_items")
             .update({"category": new,
                      "updated_at": datetime.utcnow().isoformat() + "Z"})
             .eq("category", old).execute().data) or []
    for key in _CAT_SETTING_KEYS:
        cur = menu_settings_all().get(key)
        if isinstance(cur, dict) and old in cur:
            cur[new] = cur.pop(old)      # 새 이름이 이미 있으면 옛 값이 이긴다
            menu_set_setting(key, cur)
    return {"moved": len(moved)}


def category_reorder(order: list) -> dict:
    """분류 순서 = 메뉴판 순서. sort_order 를 분류 단위로 다시 매긴다.

    분류 안에서의 기존 줄 순서는 그대로 둔다 — 여기서 바꾸려는 건 분류끼리의
    앞뒤일 뿐이다.
    """
    if not order:
        raise ValueError("분류 순서가 비어 있습니다")
    items = menu_all()
    rank = {c: i for i, c in enumerate(order)}
    # 목록에 없던 분류는 뒤로 — 지금 메뉴판에 있는 순서 그대로 붙인다.
    # 가나다순으로 정렬하면 알려주지 않은 분류가 제멋대로 재배치된다.
    for it in items:
        rank.setdefault(it["category"], len(rank))
    items.sort(key=lambda i: (rank.get(i["category"], 9999), i.get("sort_order") or 0))
    cli = get_client()
    n = 0
    for i, it in enumerate(items, 1):
        want = (rank.get(it["category"], 9999) + 1) * 1000 + i
        if it.get("sort_order") != want:
            cli.table("menu_items").update({"sort_order": want}).eq(
                "sku", it["sku"]).execute()
            n += 1
    return {"updated": n}


def items_reorder(skus: list) -> dict:
    """받은 차례대로 sort_order 를 다시 매긴다 — 화면 순서 = 채널 메뉴판 순서.

    목록에 없는 메뉴는 건드리지 않고 뒤에 그대로 남긴다(분류 칩으로 걸러 놓고
    한 분류만 손보는 경우가 대부분이라, 안 보이던 메뉴가 밀려나면 안 된다).
    """
    if not skus:
        raise ValueError("순서 목록이 비어 있습니다")
    items = menu_all()
    pos = {s: i for i, s in enumerate(skus)}
    rest = [i for i in items if i["sku"] not in pos]
    ordered = sorted((i for i in items if i["sku"] in pos),
                     key=lambda i: pos[i["sku"]]) + rest
    cli = get_client()
    n = 0
    for i, it in enumerate(ordered, 1):
        want = i * 10
        if it.get("sort_order") != want:
            cli.table("menu_items").update({"sort_order": want}).eq(
                "sku", it["sku"]).execute()
            n += 1
    return {"updated": n}


PREP_CATEGORY = "반제품"          # 매장에서 만들어 쓰는 것 — 판매 메뉴가 아니다
PREP_SUFFIX = "(반제품)"
PREP_SUPPLIER = "직접제조"


# 분류 → SKU 접두사. 새 메뉴를 만들 때 번호를 이어 붙인다.
# (기존 데이터에서 실제로 쓰이는 규칙을 그대로 옮긴 것)
_CATEGORY_PREFIX = {
    "베이커리": "BK", "크림치즈": "CREAM", "샌드위치": "SAND", "샐러드": "SALAD",
    "디저트": "DESRT", "커피": "COF", "논커피": "NCOF", "시그니처&스페셜": "SIG",
    # 케이크·산도는 '케이크 · 산도' 한 분류였다가 둘로 나뉘었다(사장님 2026-08-24).
    # 기존 메뉴가 전부 DESRT-xxx 라 새 메뉴도 같은 번호를 이어 붙인다 —
    # 접두사를 새로 만들면 한 진열대의 메뉴가 두 체계로 갈린다.
    "케이크": "DESRT", "산도": "DESRT",
    "에이드&스무디": "ADSM", "티": "TEA", "보틀": "BOT", "세트": "SET",
    "반제품": "PREP",
}


def next_sku(category):
    """그 분류의 다음 SKU. 분류를 모르면 ETC-001 부터.

    번호는 **기존 최대 + 1**로 준다. 중간에 빈 번호가 있어도 재사용하지 않는다 —
    지웠다 다시 만든 메뉴가 옛 SKU 를 물려받으면 채널 대조 기록과 엉킨다.
    """
    prefix = _CATEGORY_PREFIX.get((category or "").strip(), "ETC")
    rows = get_client().table("menu_items").select("sku").execute().data
    n = 0
    for r in rows:
        sku = str(r["sku"] or "")
        if not sku.startswith(prefix + "-"):
            continue
        tail = sku.rsplit("-", 1)[-1]
        if tail.isdigit():
            n = max(n, int(tail))
    return f"{prefix}-{n + 1:03d}"


def menu_create(fields):
    """새 메뉴 한 줄. 이름과 분류는 필수, SKU 는 분류에서 자동으로 만든다."""
    name = (fields.get("name") or "").strip()
    category = (fields.get("category") or "").strip()
    if not name:
        raise ValueError("메뉴 이름을 입력하세요.")
    if not category:
        raise ValueError("분류를 골라 주세요.")
    for m in menu_all():
        if (m.get("name") or "").strip() == name:
            raise ValueError(f"'{name}' 메뉴가 이미 있습니다({m['sku']}).")

    sku = next_sku(category)
    payload = {k: v for k, v in fields.items() if k in _MENU_ITEM_COLS}
    payload.update({
        "sku": sku, "name": name, "category": category,
        "menu_type": payload.get("menu_type") or "단일",
        "store_active": payload.get("store_active", True),
        "delivery_active": payload.get("delivery_active", True),
        # 목록 맨 아래에 붙는다 — 순서는 나중에 손으로 정리한다.
        "sort_order": payload.get("sort_order") or 9999,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    })
    get_client().table("menu_items").insert(payload).execute()
    return {"sku": sku, "name": name, "category": category}


def menu_delete(sku):
    """메뉴 삭제 — 레시피·세트 구성·채널 예외까지 같이 지운다.

    남겨두면 '어디에도 안 보이는 메뉴'의 레시피가 유령처럼 남는다.
    """
    sb = get_client()
    sku = str(sku)
    for r in recipes_all():
        if r["sku"] == sku:
            sb.table("menu_recipes").delete().eq("id", r["id"]).execute()
    parents = set()
    for c in components_all():
        if c["sku"] == sku or c["component_sku"] == sku:
            if c["component_sku"] == sku:
                parents.add(c["sku"])       # 이 메뉴를 품던 세트 — 지운 뒤 재계산
            sb.table("menu_components").delete().eq("id", c["id"]).execute()
    sb.table("menu_channels").delete().eq("sku", sku).execute()
    sb.table("menu_items").delete().eq("sku", sku).execute()
    # 구성품이 빠졌으니 세트 원가가 달라진다 — 경고만 하고 손 놓던 지점(감사).
    # 남은 구성으로 다시 계산하고, 구성이 다 사라진 세트는 component_delete 와
    # 같은 규칙으로 원가를 비운다.
    for p in parents:
        left = (sb.table("menu_components").select("id")
                .eq("sku", p).limit(1).execute().data)
        if left:
            recompute_costs([p])
        else:
            cur = (sb.table("menu_items").select("cost_source")
                   .eq("sku", p).execute().data)
            if cur and (cur[0].get("cost_source") or "").startswith("세트 구성"):
                sb.table("menu_items").update(
                    {"ingredient_cost": None, "cost_source": None}
                ).eq("sku", p).execute()
    return {"deleted": sku}


def next_prep_sku():
    rows = (get_client().table("menu_items").select("sku")
            .eq("category", PREP_CATEGORY).execute().data)
    n = 0
    for r in rows:
        try:
            n = max(n, int(str(r["sku"]).rsplit("-", 1)[-1]))
        except ValueError:
            pass
    return f"PREP-{n + 1:03d}"


def prep_create(name, yield_qty, unit="g"):
    """반제품 하나를 만든다 — 자재 1줄 + 제조용 메뉴 1줄을 이름으로 묶는다.

    이름이 연결고리다(자재명 == 메뉴명). 따로 컬럼을 두지 않아 마이그레이션이
    필요 없고, 화면에서도 무엇과 무엇이 짝인지 그대로 보인다.
    산출량(yield_qty)은 자재의 pack_qty 로 두고, 배치 원가가 pack_cost 가 된다.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("이름이 필요합니다")
    if not name.endswith(PREP_SUFFIX):
        name += PREP_SUFFIX
    yield_qty = float(yield_qty or 0)
    if yield_qty <= 0:
        raise ValueError("산출량(한 번 만들면 몇 g/ml/개 나오는지)을 입력하세요")

    sb = get_client()
    exists = [i for i in ingredients_all() if _norm_ing_name(i["name"]) == _norm_ing_name(name)]
    if exists:
        raise DuplicateIngredient(f"'{name}' 자재가 이미 있습니다.")

    sku = next_prep_sku()
    sb.table("menu_items").upsert({
        "sku": sku, "name": name, "category": PREP_CATEGORY,
        "menu_type": PREP_CATEGORY,
        "store_price": None, "delivery_price": None,
        "store_active": False, "delivery_active": False,
        "description": f"반제품 — 한 번 만들면 {yield_qty:g}{unit} 산출",
        "sort_order": 9000,
    }, on_conflict="sku").execute()

    ing = ingredient_upsert({
        "name": name, "unit": unit,
        "pack_qty": yield_qty, "pack_cost": 0,
        "category": "반제품 재료", "supplier": PREP_SUPPLIER,
        "note": f"제조 레시피: {sku}",
    })
    return {"sku": sku, "ingredient": ing, "name": name}


def prep_sync(skus=None, _seen=None):
    """반제품 메뉴의 원가 → 짝인 자재의 pack_cost 로 흘려보낸다.

    자재값이 바뀌었으니 그 자재를 쓰는 메뉴 원가도 다시 계산해야 한다.
    Returns: (자재가 바뀐 수, 뒤이어 재계산된 {sku: cost})
    """
    sb = get_client()
    preps = [i for i in sb.table("menu_items").select("sku,name,ingredient_cost")
             .eq("category", PREP_CATEGORY).execute().data
             if skus is None or i["sku"] in set(skus)]
    if not preps:
        return 0, {}
    by_name = {_norm_ing_name(i["name"]): i for i in ingredients_all()}
    touched, downstream = 0, set()
    for m in preps:
        ing = by_name.get(_norm_ing_name(m["name"]))
        cost = m.get("ingredient_cost")
        if not ing or cost is None:
            continue
        if float(ing.get("pack_cost") or 0) == float(cost):
            continue
        sb.table("ingredients").update(
            {"pack_cost": cost,
             "updated_at": datetime.utcnow().isoformat() + "Z"}
        ).eq("id", ing["id"]).execute()
        touched += 1
        downstream.update(skus_using_ingredient(ing["id"]))
    downstream -= {m["sku"] for m in preps}     # 자기 자신은 다시 돌지 않는다
    if _seen is not None:
        downstream -= _seen                      # 연쇄 재귀에서 이미 돈 메뉴 제외
    updated = (recompute_costs(list(downstream), force=True, _seen=_seen)
               if downstream else {})
    return touched, updated


def cascade_menu_cost(sku):
    """이 메뉴의 원가가 (수단 불문 — 수기 입력 포함) 바뀐 뒤 호출한다.

    이 메뉴를 품은 세트를 다시 계산하고, 반제품이면 짝 자재로 흘려보낸다.
    recompute_costs 꼬리 연쇄는 '재계산으로 바뀐' 메뉴만 알기 때문에,
    수기 입력처럼 재계산 밖에서 바뀐 경우는 이 함수가 그 첫 발을 놓는다.
    """
    seen = {sku}
    updated = {}
    parents = {c["sku"] for c in components_all() if c["component_sku"] == sku}
    if parents:
        updated.update(recompute_costs(list(parents), _seen=seen))
    row = (get_client().table("menu_items").select("category")
           .eq("sku", sku).execute().data)
    if row and row[0].get("category") == PREP_CATEGORY:
        _, more = prep_sync([sku], _seen=seen)
        updated.update(more)
    return updated


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


# ── 채널 대조 요약 · 추세 ────────────────────────────────────
# 계산 골자는 scripts/menu_diff_report.py 와 같다(정규화 매칭 → 유사도 0.8
# 승격 → 예외/비활성 제외). 숫자가 화면과 조금 달라도 추세용으로는 충분하고,
# 대조 로직을 한 곳으로 모으는 작업(감사 2차 4번)의 첫 이사분이다.

_DIFF_NOISE = re.compile(
    r"^\[B\]|^\[신메뉴\]$|^\[1~2인 세트\]$|^커피$|^보틀\(1L\)$|^단체주문 10인$"
    r"|^든든한 샌드위치 세트$|^BEARGLS HEALTHY|^Bear Cream Cheese")


def channel_diff():
    """채널 대조 정본 — 모든 화면·리포트가 이 결과 하나를 그린다.

    Returns: {ch: None(수집 없음) | {
        "collected_at": str,
        "obs": {sku: {"price": int, "name": str}},   # 채널에 실제 노출 중인 값
        "items": [ {"type": "price","name",cur,to,sku}
                 | {"type": "name","name",to,sku}
                 | {"type": "maybe","name",cur,guess,guessSku,guessPrice}
                 | {"type": "extra","name",cur}
                 | {"type": "add","name",to,sku} ],
        "counts": {"price","name","maybe","extra","add","total"},
    }}
    확정 규칙: 네이버 누락=매장 판매 기준 · active:false 는 모든 검사 제외 ·
    노이즈 제외 · 유사도 0.8 자동 승격 / 0.5 maybe (작업지시서와 동일).
    항목 필드는 작업지시서 완료 체크 키(ch|type|name)와 호환되게 유지한다.
    """
    from difflib import SequenceMatcher
    items = menu_all()
    overrides = {(o["sku"], o["channel"]): o for o in menu_channels_all()}
    snaps = menu_snapshots_all()
    by_sku = {i["sku"]: i for i in items}
    by_norm = {}
    for i in items:
        by_norm.setdefault(normalize_menu_name(i["name"]), i["sku"])

    def sim(a, b):
        return SequenceMatcher(None, a, b).ratio()

    out = {}
    for ch in ("baemin", "coupang", "naver"):
        store_based = ch == "naver"

        def exp_price(item, ov):
            if store_based:
                return item.get("store_price")
            if ov and ov.get("price_override") is not None:
                return ov["price_override"]
            return item.get("delivery_price") or item.get("store_price")

        def exp_name(item, ov):
            return (ov.get("name_override") if ov and ov.get("name_override")
                    else item["name"])

        rows = [x for x in snaps if x["channel"] == ch
                and not _DIFF_NOISE.search(x["menu_name"] or "")]
        if not rows:
            out[ch] = None
            continue
        # 채널 이름 예외로 인정한 메뉴는 예외 이름으로도 찾는다 — 안 그러면
        # 예외로 인정해도 다음 대조에서 또 '정본에 없음'으로 잡힌다.
        norm_ov = {normalize_menu_name(o["name_override"]): sku
                   for (sku, c), o in overrides.items()
                   if c == ch and o.get("name_override")}
        tasks, extras, matched, obs = [], [], set(), {}
        for x in rows:
            nm = normalize_menu_name(x["menu_name"])
            sku = (x.get("matched_sku") if x.get("matched_sku") in by_sku else None)                 or by_norm.get(nm) or norm_ov.get(nm)
            item = by_sku.get(sku)
            if not item:
                extras.append(x)
                continue
            matched.add(sku)
            obs[sku] = {"price": x.get("price"), "name": x["menu_name"]}
            ov = overrides.get((sku, ch))
            if ov and ov.get("active") is False:
                continue
            ep, en = exp_price(item, ov), exp_name(item, ov)
            if x.get("price") is not None and ep is not None and x["price"] != ep:
                tasks.append({"type": "price", "name": x["menu_name"],
                              "cur": x["price"], "to": ep, "sku": sku})
            if nm != normalize_menu_name(en):
                tasks.append({"type": "name", "name": x["menu_name"],
                              "to": en, "sku": sku})
        missing = []
        for item in items:
            on = item.get("store_active") if store_based else item.get("delivery_active")
            if not on or item["sku"] in matched:
                continue
            ov = overrides.get((item["sku"], ch))
            if ov and ov.get("active") is False:
                continue
            missing.append(item)
        # 0.8 이상 = 오타·표기 차이 — 이름(±가격) 수정으로 자동 승격
        still, used = [], set()
        for x in extras:
            nm = normalize_menu_name(x["menu_name"])
            best, score = None, 0.0
            for i, item in enumerate(missing):
                if i in used:
                    continue
                r = sim(nm, normalize_menu_name(item["name"]))
                if r > score:
                    best, score = i, r
            if best is not None and score >= 0.8:
                item = missing[best]
                used.add(best)
                ov = overrides.get((item["sku"], ch))
                obs[item["sku"]] = {"price": x.get("price"), "name": x["menu_name"]}
                tasks.append({"type": "name", "name": x["menu_name"],
                              "to": exp_name(item, ov), "sku": item["sku"]})
                ep = exp_price(item, ov)
                if x.get("price") is not None and ep is not None and x["price"] != ep:
                    tasks.append({"type": "price", "name": x["menu_name"],
                                  "cur": x["price"], "to": ep, "sku": item["sku"]})
            else:
                still.append(x)
        missing = [m for i, m in enumerate(missing) if i not in used]
        # 0.5~0.8 = 사람이 판단할 자리(maybe) — '정본에 추가'로 밀면 중복이 생긴다
        for x in still:
            nm = normalize_menu_name(x["menu_name"])
            best, score = None, 0.0
            for item in items:
                r = sim(nm, normalize_menu_name(item["name"]))
                if r > score:
                    best, score = item, r
            if best is not None and score >= 0.5:
                ov = overrides.get((best["sku"], ch))
                tasks.append({"type": "maybe", "name": x["menu_name"],
                              "cur": x.get("price"), "guess": best["name"],
                              "guessSku": best["sku"],
                              "guessPrice": exp_price(best, ov)})
            else:
                tasks.append({"type": "extra", "name": x["menu_name"],
                              "cur": x.get("price")})
        for item in missing:
            ov = overrides.get((item["sku"], ch))
            tasks.append({"type": "add", "name": item["name"],
                          "to": exp_price(item, ov), "sku": item["sku"]})
        counts = {}
        for t in tasks:
            counts[t["type"]] = counts.get(t["type"], 0) + 1
        counts["total"] = len(tasks)
        out[ch] = {"collected_at": rows[0].get("collected_at"),
                   "obs": obs, "items": tasks, "counts": counts}
    return out


def channel_diff_counts():
    """채널별 불일치 요약 — channel_diff 의 파생값."""
    return {ch: (v["counts"] if v else None)
            for ch, v in channel_diff().items()}


def append_diff_history():
    """수집 직후 호출 — 채널별 불일치 요약을 이력으로 한 줄 쌓는다(최근 30회)."""
    counts = channel_diff_counts()
    hist = get_setting("diff_history", []) or []
    hist.append({"at": datetime.utcnow().isoformat() + "Z", **{
        ch: v for ch, v in counts.items() if v is not None}})
    menu_set_setting("diff_history", hist[-30:])
    return counts


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


# ── 발주처별 시세 ────────────────────────────────────────────
# 같은 자재라도 발주처마다 값이 다르다(엠즈푸드 vs 쿠팡 사입). 자재 본체의
# pack_qty/pack_cost 는 "실제로 사는 조건" 하나만 갖고, 다른 발주처 시세는
# 이 표에 곁들여 둔다. 화면이 최저가를 견줘 어디서 사는 게 싼지 알려준다.
# 009 마이그레이션 전이면 표가 없다 — 조용히 건너뛴다(기능만 빠지고 동작한다).

_OFFERS_MISSING = ("42P01", "PGRST205", "PGRST200")   # 표 없음


def offers_all():
    try:
        return (get_client().table("ingredient_offers").select("*")
                .execute().data)
    except Exception as e:  # noqa: BLE001
        if getattr(e, "code", None) in _OFFERS_MISSING:
            return []
        raise


def offer_upsert(ingredient_id, supplier, pack_qty, pack_cost, note=None):
    supplier = (supplier or "").strip()
    if not supplier or not ingredient_id:
        return
    try:
        get_client().table("ingredient_offers").upsert({
            "ingredient_id": int(ingredient_id),
            "supplier": supplier,
            "pack_qty": pack_qty,
            "pack_cost": pack_cost,
            "note": note,
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }, on_conflict="ingredient_id,supplier").execute()
    except Exception as e:  # noqa: BLE001
        if getattr(e, "code", None) in _OFFERS_MISSING:
            logger.warning("009 미적용 — 발주처 시세 기록 생략 "
                           "(supabase/migrations/009_ingredient_offers.sql 실행)")
            return
        raise


def offer_delete(offer_id):
    get_client().table("ingredient_offers").delete().eq("id", int(offer_id)).execute()


def _offers_move(from_id, to_id):
    """자재를 합칠 때 시세 기록도 따라가게 한다(없으면 아무 일 없음)."""
    try:
        rows = (get_client().table("ingredient_offers").select("*")
                .eq("ingredient_id", int(from_id)).execute().data)
        for r in rows:
            offer_upsert(to_id, r["supplier"], r.get("pack_qty"),
                         r.get("pack_cost"), r.get("note"))
    except Exception as e:  # noqa: BLE001
        if getattr(e, "code", None) not in _OFFERS_MISSING:
            raise


class DuplicateIngredient(ValueError):
    """같은 이름의 자재가 이미 있을 때."""


def _norm_ing_name(s):
    """이름 비교용 정규화 — 공백·구두점·대소문자 무시.

    '플레인 베이글' = '플레인베이글' = '플레인-베이글' = '플레인(베이글)'.
    실제로 겹쳐 등록된 것들이 대개 띄어쓰기나 괄호 차이였다.
    """
    return re.sub(r"[\s·\-_/,.()\[\]]+", "", (s or "")).lower()


def ingredient_merge(keep_id, drop_id, price_from="keep"):
    """자재 둘을 하나로 합친다 — 레시피를 옮기고 남는 쪽을 지운다.

    같은 메뉴에 둘 다 들어가 있으면 사용량을 더한다(둘로 나눠 적어둔 것이므로).
    Returns: {"moved": 옮긴 레시피 줄 수, "merged": 합쳐진 줄 수, "recomputed": {...}}
    """
    keep_id, drop_id = int(keep_id), int(drop_id)
    if keep_id == drop_id:
        raise ValueError("같은 자재입니다")
    sb = get_client()
    ings = {i["id"]: i for i in ingredients_all()}
    keep, drop = ings.get(keep_id), ings.get(drop_id)
    if not keep or not drop:
        raise ValueError("자재를 찾을 수 없습니다")
    if (keep.get("unit") or "") != (drop.get("unit") or ""):
        raise ValueError(
            f"단위가 다릅니다({keep['unit']} vs {drop['unit']}). "
            f"사용량 뜻이 달라 자동으로 합칠 수 없습니다 — 단위를 먼저 맞춰주세요.")

    # 없어질 쪽의 값은 발주처 시세로 곁에 남긴다 — 같은 자재를 발주처마다
    # 다른 값에 파는 게 실제 상황이라, 지워버리면 최저가 비교를 못 한다.
    _offers_move(drop_id, keep_id)
    if drop.get("supplier") and drop.get("pack_cost"):
        offer_upsert(keep_id, drop["supplier"], drop.get("pack_qty"),
                     drop.get("pack_cost"), drop.get("note"))

    # 발주 사이트에서 새로 받은 쪽이 구매 단위·가격은 정확하다. 레시피는
    # 기존 자재에 붙어 있으므로, 껍데기는 기존을 두고 값만 새 것으로 가져온다.
    # 단, 기존 자재를 **다른 발주처에서 더 싸게 사입 중**이면 본체 값은 지킨다
    # — 원가는 실제로 산 가격이어야 한다(사장님 확인 2026-08-16). 그 경우
    # 새 값은 위에서 시세로만 남는다.
    keep_sup = (keep.get("supplier") or "").strip()
    drop_sup = (drop.get("supplier") or "").strip()
    other_supplier = (keep_sup and drop_sup and keep_sup != drop_sup
                      and keep.get("pack_cost"))
    if price_from == "drop" and not other_supplier:
        sb.table("ingredients").update({
            "pack_qty": drop.get("pack_qty"),
            "pack_cost": drop.get("pack_cost"),
            "supplier": drop.get("supplier") or keep.get("supplier"),
            "note": drop.get("note") or keep.get("note"),
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }).eq("id", keep_id).execute()

    rows = sb.table("menu_recipes").select("*").execute().data
    keep_by_sku = {r["sku"]: r for r in rows if r["ingredient_id"] == keep_id}
    moving = [r for r in rows if r["ingredient_id"] == drop_id]

    # 되돌리기용 기록 — 합치기는 자재 한 줄을 지우는 되돌릴 수 없는 작업이라,
    # 잘못 누르면 손으로 복구해야 했다(사장님 요청 2026-08-16).
    undo = {
        "dropped": {k: drop.get(k) for k in
                    ("name", "unit", "pack_qty", "pack_cost", "category",
                     "supplier", "note")},
        "keep_id": keep_id,
        "keep_before": {k: keep.get(k) for k in
                        ("pack_qty", "pack_cost", "supplier", "note")},
        "moved": [], "merged": [],
        "kept_name": keep["name"], "dropped_name": drop["name"],
        "at": datetime.utcnow().isoformat() + "Z",
    }

    moved = merged = 0
    for r in moving:
        other = keep_by_sku.get(r["sku"])
        if other:                              # 한 메뉴에 둘 다 있으면 사용량을 더한다
            undo["merged"].append({"keep_row": other["id"], "sku": r["sku"],
                                   "keep_qty": float(other["qty"]),
                                   "drop_qty": float(r["qty"])})
            sb.table("menu_recipes").update(
                {"qty": float(other["qty"]) + float(r["qty"])}
            ).eq("id", other["id"]).execute()
            sb.table("menu_recipes").delete().eq("id", r["id"]).execute()
            merged += 1
        else:
            undo["moved"].append({"row": r["id"], "sku": r["sku"]})
            sb.table("menu_recipes").update(
                {"ingredient_id": keep_id}).eq("id", r["id"]).execute()
            moved += 1

    affected = sorted({r["sku"] for r in moving} | set(keep_by_sku))
    sb.table("ingredients").delete().eq("id", drop_id).execute()
    updated = recompute_costs(affected, force=True) if affected else {}
    menu_set_setting("last_merge", undo)
    return {"moved": moved, "merged": merged, "recomputed": updated,
            "kept": keep["name"], "dropped": drop["name"]}


def merge_undo_info():
    """되돌릴 수 있는 합치기가 있으면 그 요약. 없으면 None."""
    rec = menu_settings_all().get("last_merge")
    if not rec:
        return None
    return {"kept": rec.get("kept_name"), "dropped": rec.get("dropped_name"),
            "at": rec.get("at"),
            "lines": len(rec.get("moved") or []) + len(rec.get("merged") or [])}


def ingredient_merge_undo():
    """직전 합치기를 되돌린다 — 지운 자재를 되살리고 레시피를 제자리로.

    자재는 새 id 로 되살아난다(원래 id 는 이미 사라졌다). 레시피가 그 새 id 를
    가리키게 하므로 원가는 합치기 전과 같아진다.
    """
    rec = menu_settings_all().get("last_merge")
    if not rec:
        raise ValueError("되돌릴 합치기가 없습니다.")
    sb = get_client()

    d = rec["dropped"]
    payload = {k: v for k, v in d.items() if k in _ING_COLS}
    payload["updated_at"] = datetime.utcnow().isoformat() + "Z"
    try:
        rows = sb.table("ingredients").insert(payload).execute().data
    except Exception as e:  # noqa: BLE001 — 008 이전이면 supplier 컬럼이 없다
        if getattr(e, "code", None) not in _MISSING_COLUMN_CODES:
            raise
        payload.pop("supplier", None)
        rows = sb.table("ingredients").insert(payload).execute().data
    new_id = rows[0]["id"]

    # 옮겨갔던 줄을 되살린 자재로 돌린다
    for m in rec.get("moved") or []:
        sb.table("menu_recipes").update({"ingredient_id": new_id}).eq(
            "id", m["row"]).execute()
    # 합쳐졌던 줄은 사용량을 되돌리고, 없어진 줄을 다시 만든다
    for m in rec.get("merged") or []:
        sb.table("menu_recipes").update({"qty": m["keep_qty"]}).eq(
            "id", m["keep_row"]).execute()
        sb.table("menu_recipes").insert(
            {"sku": m["sku"], "ingredient_id": new_id, "qty": m["drop_qty"],
             "updated_at": datetime.utcnow().isoformat() + "Z"}).execute()

    # 남긴 쪽 값이 덮였으면 되돌린다
    before = {k: v for k, v in (rec.get("keep_before") or {}).items()
              if k in _ING_COLS}
    if before:
        before["updated_at"] = datetime.utcnow().isoformat() + "Z"
        try:
            sb.table("ingredients").update(before).eq("id", rec["keep_id"]).execute()
        except Exception as e:  # noqa: BLE001
            if getattr(e, "code", None) not in _MISSING_COLUMN_CODES:
                raise
            before.pop("supplier", None)
            sb.table("ingredients").update(before).eq("id", rec["keep_id"]).execute()

    skus = sorted({m["sku"] for m in (rec.get("moved") or [])} |
                  {m["sku"] for m in (rec.get("merged") or [])})
    updated = recompute_costs(skus, force=True) if skus else {}
    menu_set_setting("last_merge", {})
    return {"restored": d.get("name"), "recomputed": updated,
            "lines": len(skus)}


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


class IngredientInUse(ValueError):
    """레시피에서 쓰는 중인 자재를 지우려 할 때. skus 에 쓰는 메뉴가 담긴다."""

    def __init__(self, message, skus):
        super().__init__(message)
        self.skus = skus


def ingredient_delete(ing_id, force=False):
    """자재 삭제.

    레시피에서 쓰는 중이면 그냥 지우지 않는다 — 지우면 그 메뉴 원가가 조용히
    틀려진다. 어떤 메뉴가 쓰는지 알려주고, force 를 받으면 그 레시피 줄까지
    지운 뒤 해당 메뉴 원가를 다시 계산한다(사장님 요청 2026-08-17).
    """
    ing_id = int(ing_id)
    sb = get_client()
    lines = [r for r in recipes_all() if r["ingredient_id"] == ing_id]
    skus = sorted({r["sku"] for r in lines})
    if lines and not force:
        names = {m["sku"]: m["name"] for m in menu_all()}
        shown = ", ".join(names.get(s, s) for s in skus[:5])
        raise IngredientInUse(
            f"{len(skus)}개 메뉴가 쓰는 중입니다 ({shown}"
            f"{' 외' if len(skus) > 5 else ''}).", skus)
    for r in lines:
        sb.table("menu_recipes").delete().eq("id", r["id"]).execute()
    sb.table("ingredients").delete().eq("id", ing_id).execute()
    updated = recompute_costs(skus, force=True) if skus else {}
    return {"removed_lines": len(lines), "recomputed": updated, "skus": skus}


def recipe_upsert(sku, ingredient_id, qty):
    return (get_client().table("menu_recipes").upsert(
        {"sku": sku, "ingredient_id": ingredient_id, "qty": qty,
         "updated_at": datetime.utcnow().isoformat() + "Z"},
        on_conflict="sku,ingredient_id").execute().data or [None])[0]


def recipe_delete(rid):
    sb = get_client()
    rows = (sb.table("menu_recipes").select("sku")
            .eq("id", rid).execute().data)
    sb.table("menu_recipes").delete().eq("id", rid).execute()
    sku = rows[0]["sku"] if rows else None
    # 마지막 줄을 지우면 재계산할 재료가 없어 recompute 가 그냥 지나간다 —
    # 그러면 직전 계산값이 '자동계산' 도장인 채 유령으로 남는다(감사 확인).
    # 세트(component_delete)에 이미 있는 방어를 레시피에도 똑같이 둔다.
    if sku:
        left = (sb.table("menu_recipes").select("id")
                .eq("sku", sku).limit(1).execute().data)
        if not left:
            cur = (sb.table("menu_items").select("cost_source")
                   .eq("sku", sku).execute().data)
            if cur and (cur[0].get("cost_source") or "").startswith("레시피 자동계산"):
                sb.table("menu_items").update(
                    {"ingredient_cost": None, "cost_source": None}
                ).eq("sku", sku).execute()
    return sku


# ── 세트 구성 ────────────────────────────────────────────────
# 세트는 재료가 아니라 '메뉴의 묶음'이다. 010 마이그레이션 전이면 표가 없다 —
# 조용히 건너뛴다(세트 원가만 안 잡히고 나머지는 그대로 동작한다).

_COMPONENTS_MISSING = ("42P01", "PGRST205", "PGRST200")


def components_all():
    try:
        return get_client().table("menu_components").select("*").execute().data
    except Exception as e:  # noqa: BLE001
        if getattr(e, "code", None) in _COMPONENTS_MISSING:
            return []
        raise


def component_upsert(sku, component_sku, qty=1, choice_group=None):
    if not sku or not component_sku:
        raise ValueError("세트와 구성 메뉴를 모두 골라주세요")
    if sku == component_sku:
        raise ValueError("자기 자신은 구성으로 넣을 수 없습니다")
    sb = get_client()
    # choice_group 이 비면(고정 구성) unique 제약이 안 걸린다 — Postgres 에서
    # NULL 은 서로 다른 값이라 upsert 가 조용히 중복 행을 만든다(수량을 바꾸면
    # 같은 메뉴 줄이 하나 더 생기는 실사고, 2026-08-29). 직접 찾아 가른다.
    q = (sb.table("menu_components").select("id")
         .eq("sku", sku).eq("component_sku", component_sku))
    q = (q.is_("choice_group", "null") if not choice_group
         else q.eq("choice_group", choice_group))
    cur = q.execute().data
    payload = {"sku": sku, "component_sku": component_sku,
               "qty": float(qty or 1), "choice_group": choice_group or None,
               "updated_at": datetime.utcnow().isoformat() + "Z"}
    if cur:
        sb.table("menu_components").update(payload).eq("id", cur[0]["id"]).execute()
        # 같은 줄이 이미 여럿이면(과거 upsert 가 만든 중복) 나머지는 지운다
        for extra in cur[1:]:
            sb.table("menu_components").delete().eq("id", extra["id"]).execute()
    else:
        sb.table("menu_components").insert(payload).execute()
    return recompute_costs([sku], force=True)


def component_delete(row_id):
    sb = get_client()
    row = sb.table("menu_components").select("sku").eq("id", int(row_id)).execute().data
    sb.table("menu_components").delete().eq("id", int(row_id)).execute()
    if not row:
        return {}
    sku = row[0]["sku"]
    # 마지막 구성을 지우면 합산할 게 없어 recompute 가 그냥 지나간다. 그러면
    # 직전 계산값이 원가로 남아 '구성은 비었는데 원가는 있는' 상태가 된다.
    left = (sb.table("menu_components").select("id").eq("sku", sku)
            .limit(1).execute().data)
    if not left:
        cur = (sb.table("menu_items").select("cost_source")
               .eq("sku", sku).execute().data)
        src = (cur[0].get("cost_source") or "") if cur else ""
        if src.startswith("세트 구성"):
            sb.table("menu_items").update(
                {"ingredient_cost": None, "cost_source": None}).eq("sku", sku).execute()
        return {sku: None}
    return recompute_costs([sku], force=True)


def _set_cost(sku, comps, cost_of):
    """세트 원가 = 고정 구성 합 + 택1 자리마다 가장 비싼 것.

    cost_of(sku) 가 None(원가 미상)인 구성이 하나라도 끼면 세트 원가도 못 낸다 —
    빠뜨린 채 합치면 실제보다 싸 보여서 위험하다.
    """
    total, fixed = 0.0, [c for c in comps if not c.get("choice_group")]
    for c in fixed:
        v = cost_of(c["component_sku"])
        if v is None:
            return None
        total += float(c.get("qty") or 1) * v
    groups = {}
    for c in comps:
        if c.get("choice_group"):
            groups.setdefault(c["choice_group"], []).append(c)
    for _, rows in groups.items():
        vals = []
        for c in rows:
            v = cost_of(c["component_sku"])
            if v is None:
                return None
            vals.append(float(c.get("qty") or 1) * v)
        if not vals:
            return None
        total += max(vals)                 # 최악(가장 비싼 선택) 기준
    return round(total, 1)


def recompute_costs(skus=None, force=False, _seen=None):
    """레시피 기반으로 menu_items.ingredient_cost 재계산.

    Args:
        skus: 대상 SKU 목록(None=레시피가 있는 전 메뉴).
        force: True 면 '웹에서 직접 입력' 원가도 덮어쓴다. 레시피를 사람이
               직접 고친 직후에는 True 로 부른다(레시피가 더 최신 의사표시).
        _seen: 연쇄 재귀의 방문 기록(내부용) — 같은 메뉴를 두 번 돌지 않는다.
    Returns: 갱신된 {sku: cost} (연쇄로 따라 바뀐 세트·하위 메뉴 포함)
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

    # 세트는 구성 메뉴의 원가를 더해 낸다 — 구성품이 먼저 계산돼 있어야 하므로
    # 레시피 기반을 다 끝낸 뒤 2단계로 돈다.
    comps_by = {}
    for c in components_all():
        if not skus or c["sku"] in skus:
            comps_by.setdefault(c["sku"], []).append(c)

    if not by_sku and not comps_by:
        return {}
    targets = set(by_sku) | set(comps_by)
    items = (sb.table("menu_items").select("sku,ingredient_cost,cost_source")
             .in_("sku", list(targets)).execute().data)
    src_by = {i["sku"]: (i.get("cost_source") or "") for i in items}
    updated = {}
    stamp = f"레시피 자동계산({date.today().isoformat()})"
    for sku, lns in by_sku.items():
        if not force and src_by.get(sku, "").startswith("웹에서 직접 입력"):
            continue
        total, unknown = 0.0, False
        for ln in lns:
            ing = ings.get(ln["ingredient_id"])
            # 자재가 없거나 단가를 모르면(양·값이 비었거나 0) 그 줄을 0원으로
            # 더하면 안 된다 — 메뉴가 실제보다 싸 보인다(요거트 0원 사고 계열).
            # 세트와 같은 정책: 미상이 끼면 그 메뉴는 건드리지 않는다.
            if not ing or not ing.get("pack_qty") or not ing.get("pack_cost"):
                unknown = True
                break
            total += float(ln["qty"]) * float(ing["pack_cost"]) / float(ing["pack_qty"])
        if unknown:
            continue
        cost = round(total, 1)
        sb.table("menu_items").update(
            {"ingredient_cost": cost, "cost_source": stamp}).eq("sku", sku).execute()
        updated[sku] = cost

    if comps_by:
        # 구성품 원가는 방금 갱신한 값을 먼저 보고, 없으면 DB 의 현재 값을 쓴다.
        need = {c["component_sku"] for rows in comps_by.values() for c in rows}
        cur = {}
        if need:
            for r in (sb.table("menu_items").select("sku,ingredient_cost")
                      .in_("sku", list(need)).execute().data):
                cur[r["sku"]] = r.get("ingredient_cost")

        def cost_of(s):
            v = updated.get(s, cur.get(s))
            return float(v) if v is not None else None

        set_stamp = f"세트 구성 자동합산({date.today().isoformat()})"
        for sku, rows in comps_by.items():
            if not force and src_by.get(sku, "").startswith("웹에서 직접 입력"):
                continue
            cost = _set_cost(sku, rows, cost_of)
            if cost is None:
                continue          # 구성품 중 원가 미상이 있으면 건드리지 않는다
            sb.table("menu_items").update(
                {"ingredient_cost": cost, "cost_source": set_stamp}
            ).eq("sku", sku).execute()
            updated[sku] = cost

    # ── 꼬리 연쇄 — 어느 문으로 들어왔든 바뀐 원가를 쓰는 곳까지 흘려보낸다.
    # 예전엔 호출자마다 따로 챙겨야 해서 자재 합치기·삭제·시드 경로가 각각
    # 빠뜨렸다(감사 확인 누수 8건의 공통 뿌리). _seen 이 재방문을 막는다.
    if updated:
        seen = _seen if _seen is not None else set()
        fresh = set(updated) - seen
        seen.update(fresh)
        if fresh:
            # 방금 바뀐 메뉴를 품은 세트 — force=False: 수기 원가 세트는 보호
            parents = {c["sku"] for c in components_all()
                       if c["component_sku"] in fresh} - seen
            if parents:
                updated.update(recompute_costs(list(parents), _seen=seen))
            # 방금 바뀐 반제품 — 짝 자재로 흘려보내고 그 자재를 쓰는 메뉴까지
            prep_rows = (sb.table("menu_items").select("sku")
                         .in_("sku", list(fresh)).eq("category", PREP_CATEGORY)
                         .execute().data)
            preps = [r["sku"] for r in prep_rows]
            if preps:
                _, more = prep_sync(preps, _seen=seen)
                updated.update(more)
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
    # 반제품 레시피가 이 시드로 처음 채워지면 그 반제품의 원가가 잡힌다.
    # 그 값을 짝인 자재로 흘려보내지 않으면, 반제품을 쓰는 메뉴는 자재 단가가
    # 0원이라 원가 0원으로 남는다(실사고: 요거트 크림치즈 2026-08-24).
    synced, more = prep_sync()
    updated = {**updated, **more}
    return {"ingredients_added": len(new_rows), "lines_added": len(lines),
            "recomputed": len(updated), "preps_synced": synced}


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
