"""회의 기록 데이터 계층 (schema_v8.sql).

service/app.py 의 /meeting 화면들이 쓴다. 연결은 supabase_client.get_client() 재사용.

구성:
  · meetings       — 회의 한 건 (날짜/제목/분류/참석자/논의 내용/결정한 것)
  · meeting_tasks  — 그 회의에서 나온 할 일 (담당·기한·메모·완료)

분류(category)는 **자유 입력**이다. 기본 5종을 보여주되 직원이 새 이름을
직접 넣을 수 있고, 한 번 쓰인 이름은 그다음부터 목록에 함께 뜬다
(사장님 지시 2026-08-27). 그래서 분류 표를 따로 두지 않는다.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone

from .supabase_client import get_client

logger = logging.getLogger(__name__)

MEETINGS = "meetings"
TASKS = "meeting_tasks"

KST = timezone(timedelta(hours=9))

# 처음 화면에 뜨는 기본 분류. 직원이 직접 입력해 늘릴 수 있다.
DEFAULT_CATEGORIES = ("주간회의", "메뉴", "마케팅", "인사", "운영")

MAX_TASKS = 30          # 회의 한 건에 붙일 수 있는 할 일 수 (실수 방지용 상한)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _d(v):
    """date | 'YYYY-MM-DD' → 'YYYY-MM-DD' (빈 값은 None)"""
    if not v:
        return None
    if isinstance(v, date):
        return str(v)
    s = str(v)[:10]
    return s if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) else None


def today_kst() -> date:
    return datetime.now(KST).date()


def dday_label(due, today=None):
    """기한 → 'D-3' / 'D-DAY' / 'D+2 지남' (기한 없으면 None).

    사장님 지시 2026-08-28: 할 일 기한은 화면에 **D-Day 로만** 보여준다
    ("2026-08-30" 을 보고 며칠 남았는지 매번 세는 게 번거롭다).
    적을 때는 그대로 달력에서 날짜를 고른다 — 표시만 바꾼다.
    기준일은 서버 시계(UTC)가 아니라 **매장 시간(KST)** 이다.
    """
    d = _d(due)
    if not d:
        return None
    left = (date.fromisoformat(d) - (today or today_kst())).days
    if left > 0:
        return f"D-{left}"
    if left == 0:
        return "D-DAY"
    return f"D+{-left} 지남"


# ---------------------------------------------------------------------------
# 회의
# ---------------------------------------------------------------------------

def _clean(v, limit=None):
    s = (v or "").strip()
    return s[:limit] if limit else s


def create_meeting(title, meeting_date=None, category=None, attendees=None,
                   body=None, decisions=None) -> int:
    row = {
        "title": _clean(title, 120) or "제목 없는 회의",
        "meeting_date": _d(meeting_date) or str(today_kst()),
        "category": _clean(category, 20) or None,
        "attendees": _clean(attendees, 200) or None,
        "body": _clean(body) or None,
        "decisions": _clean(decisions) or None,
        "updated_at": _now(),
    }
    data = get_client().table(MEETINGS).insert(row).execute().data or []
    return data[0]["id"] if data else 0


_PATCHABLE = ("title", "meeting_date", "category", "attendees", "body",
              "decisions")


def update_meeting(meeting_id, **patch):
    payload = {k: v for k, v in patch.items() if k in _PATCHABLE}
    if not payload:
        return None
    if "meeting_date" in payload:
        payload["meeting_date"] = _d(payload["meeting_date"]) or str(today_kst())
    for k in ("title", "category", "attendees", "body", "decisions"):
        if k in payload:
            limit = {"title": 120, "category": 20, "attendees": 200}.get(k)
            payload[k] = _clean(payload[k], limit) or None
    payload["title"] = payload.get("title") or None
    if payload.get("title") is None and "title" in patch:
        payload["title"] = "제목 없는 회의"
    payload["updated_at"] = _now()
    return (get_client().table(MEETINGS).update(payload)
            .eq("id", meeting_id).execute().data)


def delete_meeting(meeting_id):
    # meeting_tasks 는 on delete cascade 로 같이 지워진다.
    return (get_client().table(MEETINGS).delete()
            .eq("id", meeting_id).execute().data)


def get_meeting(meeting_id):
    rows = (get_client().table(MEETINGS).select("*")
            .eq("id", meeting_id).limit(1).execute().data)
    return rows[0] if rows else None


def _search_filter(q):
    """검색어 → PostgREST or_ 조건. or_ 문법을 깨는 쉼표·괄호는 뺀다."""
    base = re.sub(r"[,()]", " ", q or "").strip()
    terms = {base, base.replace(" ", "")} - {""}
    parts = []
    for t in terms:
        parts += [f"title.ilike.%{t}%", f"body.ilike.%{t}%",
                  f"decisions.ilike.%{t}%", f"attendees.ilike.%{t}%"]
    return ",".join(parts)


def list_meetings(q=None, category=None, limit=60, offset=0):
    """회의 목록 — 최신순. (rows, total)"""
    sel = (get_client().table(MEETINGS)
           .select("id,meeting_date,title,category,attendees,body,decisions",
                   count="exact"))
    if category:
        sel = sel.eq("category", category)
    if q and q.strip():
        cond = _search_filter(q)
        if cond:
            sel = sel.or_(cond)
    resp = (sel.order("meeting_date", desc=True).order("id", desc=True)
            .range(offset, offset + limit - 1).execute())
    return (resp.data or []), (resp.count or 0)


def categories() -> list[str]:
    """화면에 보여줄 분류 목록 — 기본 5종 + 실제로 쓰인 분류."""
    used = []
    try:
        rows = (get_client().table(MEETINGS).select("category")
                .order("meeting_date", desc=True).limit(300).execute().data or [])
        used = [r.get("category") for r in rows if (r.get("category") or "").strip()]
    except Exception:  # noqa: BLE001
        logger.debug("분류 목록 조회 실패", exc_info=True)
    out = list(DEFAULT_CATEGORIES)
    for c in used:
        if c not in out:
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# 할 일
# ---------------------------------------------------------------------------

def tasks_for(meeting_ids):
    """회의 id 여러 개의 할 일을 한 번에 → {meeting_id: [task, ...]}"""
    ids = [int(i) for i in (meeting_ids or [])]
    if not ids:
        return {}
    rows = (get_client().table(TASKS).select("*")
            .in_("meeting_id", ids)
            .order("sort").order("id").execute().data or [])
    out = {}
    for r in rows:
        out.setdefault(r["meeting_id"], []).append(r)
    return out


def get_tasks(meeting_id):
    return (get_client().table(TASKS).select("*")
            .eq("meeting_id", meeting_id)
            .order("sort").order("id").execute().data or [])


def save_tasks(meeting_id, items):
    """회의의 할 일 목록을 화면에서 온 그대로 맞춘다.

    items: [{id?, content, owner, due_date, memo, done}]
    - id 가 있으면 수정, 없으면 새로 추가
    - 화면에 없는 기존 항목은 삭제
    완료 여부(done)는 상세 화면 체크박스가 따로 관리하지만, 수정 화면에서도
    넘어오면 존중한다. 체크 이력(done_at)은 상태가 바뀔 때만 손댄다.
    """
    items = list(items or [])[:MAX_TASKS]
    client = get_client()
    old = {r["id"]: r for r in get_tasks(meeting_id)}
    keep, inserts = set(), []

    for i, it in enumerate(items):
        content = _clean(it.get("content"), 300)
        if not content:
            continue
        row = {
            "content": content,
            "owner": _clean(it.get("owner"), 30) or None,
            "due_date": _d(it.get("due_date")),
            "memo": _clean(it.get("memo"), 500) or None,
            "sort": i,
        }
        tid = it.get("id")
        try:
            tid = int(tid) if tid not in (None, "", "new") else None
        except (TypeError, ValueError):
            tid = None
        if tid and tid in old:
            done = bool(it.get("done"))
            if done != bool(old[tid].get("done")):
                row["done"] = done
                row["done_at"] = _now() if done else None
            client.table(TASKS).update(row).eq("id", tid).execute()
            keep.add(tid)
        else:
            row["meeting_id"] = meeting_id
            row["done"] = bool(it.get("done"))
            if row["done"]:
                row["done_at"] = _now()
            inserts.append(row)

    if inserts:
        client.table(TASKS).insert(inserts).execute()
    gone = [i for i in old if i not in keep]
    if gone:
        client.table(TASKS).delete().in_("id", gone).execute()


def set_task_done(task_id, done=True):
    return (get_client().table(TASKS).update({
        "done": bool(done),
        "done_at": _now() if done else None,
    }).eq("id", task_id).execute().data)


def open_tasks(limit=20):
    """안 끝난 할 일 — 홈 화면용. 기한 있는 것부터(빠른 순), 그다음 기한 없는 것.

    PostgREST 는 정렬에서 null 위치를 지정할 수 있다(nullsfirst=False).
    회의 제목·날짜는 관계 조회로 같이 가져온다.
    """
    rows = (get_client().table(TASKS)
            .select("id,content,owner,due_date,memo,meeting_id,"
                    "meetings(title,meeting_date)")
            .eq("done", False)
            .order("due_date", desc=False, nullsfirst=False)
            .order("id", desc=False)
            .limit(limit).execute().data or [])
    today = today_kst()
    out = []
    for r in rows:
        m = r.get("meetings") or {}
        due = _d(r.get("due_date"))
        overdue = bool(due and date.fromisoformat(due) < today)
        out.append({
            "id": r["id"],
            "content": r.get("content") or "",
            "owner": r.get("owner") or "",
            "memo": r.get("memo") or "",
            "due": due,
            "dday": dday_label(due, today),
            "overdue": overdue,
            "meeting_id": r.get("meeting_id"),
            "meeting_title": m.get("title") or "회의",
            "meeting_date": m.get("meeting_date"),
        })
    return out


def open_task_count() -> int:
    resp = (get_client().table(TASKS).select("id", count="exact")
            .eq("done", False).limit(1).execute())
    return resp.count or 0
