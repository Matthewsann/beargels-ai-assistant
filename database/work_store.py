"""업무 보드 데이터 계층 (2026-08-31).

관리자 업무를 한자리에서 보는 화면(/work)이 쓴다. 두 곳에 나뉘어 저장된 것을
**읽을 때만** 합친다 — 저장은 각자 자리에 둔다(회의 할 일은 회의를 지우면
같이 지워져야 하고, 그때그때 생긴 업무는 그러면 안 된다).

    work_tasks     — 회의와 무관하게 등록한 업무 (schema_v10.sql, 이 파일이 주인)
    meeting_tasks  — 회의에서 나온 할 일 (meeting_store.py 가 주인, 여기선 읽기만)

역할 분담(사장님 확정 2026-08-31):
    비서  = 우선순위 매기기 · 오늘 할 것 알려주기 · 방치된 업무 리마인드
    담당자 = 업무 등록 · 담당자/기한 정하기 · 진행 기록 · 완료 체크

그래서 **우선순위는 컬럼이 아니라 규칙 계산**이다(`priority_of`). AI 를 쓰지
않아 비용이 0 이고, 왜 그 순서인지 항상 한 줄로 설명된다. 기한이 지나거나
날짜가 흐르면 저절로 순위가 바뀐다.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .supabase_client import get_client, touch_version


def _touch():
    """쓰기 뒤 '업무가 바뀌었다' 표식 — 열려 있는 화면들이 이걸 보고 다시 그린다."""
    touch_version("work")

TABLE = "work_tasks"

KST = timezone(timedelta(hours=9))

# 등록만 해두고 아무도 손대지 않은 지 이만큼 지나면 '오래됨'으로 본다.
STALE_DAYS = 7

# 화면·알림이 다루는 최대 건수. 관리자 업무는 수십 건 규모라 넉넉하다.
MAX_OPEN = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def today() -> date:
    """매장 기준 오늘. 서버(PythonAnywhere)가 UTC 라 그냥 쓰면 하루 어긋난다."""
    return datetime.now(KST).date()


# ---------------------------------------------------------------------------
# 등록·수정 (담당자가 하는 일)
# ---------------------------------------------------------------------------

def add_task(content, owner=None, due_date=None, memo=None):
    """업무 하나 등록. content 만 있으면 되고 나머지는 나중에 채워도 된다."""
    content = (content or "").strip()
    if not content:
        raise ValueError("업무 내용이 비었습니다")
    row = {
        "content": content[:300],
        "owner": (owner or "").strip()[:30] or None,
        "due_date": due_date or None,
        "memo": (memo or "").strip()[:500] or None,
    }
    dup = _recent_same(row)
    if dup:                       # 같은 업무가 방금 들어왔다 — 두 번 세지 않는다
        return dup
    res = get_client().table(TABLE).insert(row).execute()
    _touch()
    return res.data[0] if res.data else None


# 같은 내용이 이만큼 안에 또 들어오면 '두 번 눌린 것'으로 본다.
# 폰에서 저장이 느릴 때 한 번 더 누르거나, 네트워크가 재시도하면 2건이 생겼다
# (사장님 2026-09-10). 화면에도 잠금을 걸었지만 여기서 한 번 더 막는다 —
# 화면 잠금은 새로고침·다른 기기·재전송을 못 막는다.
DUP_SECONDS = 90


def _recent_same(row: dict) -> dict | None:
    """방금 등록된 똑같은 업무가 있으면 그 행을 돌려준다(없으면 None).

    실패해도 등록은 되어야 하므로 조회가 깨지면 조용히 넘어간다.
    """
    try:
        cut = (datetime.now(timezone.utc) - timedelta(seconds=DUP_SECONDS)).isoformat()
        rows = (get_client().table(TABLE).select("*")
                .eq("content", row["content"]).eq("done", False)
                .gte("created_at", cut).execute().data) or []
    except Exception:  # noqa: BLE001
        return None
    for r in rows:                 # 담당자까지 같아야 같은 업무로 본다
        if (r.get("owner") or None) == row["owner"]:
            return r
    return None


def update_task(task_id, **fields):
    """담당자·기한·내용·메모를 고친다. 빈 문자열은 '지움'(null)으로 본다."""
    payload = {}
    for k in ("content", "owner", "memo"):
        if k in fields:
            v = (fields[k] or "").strip()
            payload[k] = v or None
    if "due_date" in fields:
        payload["due_date"] = fields["due_date"] or None
    if not payload:
        return None
    payload["updated_at"] = _now()
    out = (get_client().table(TABLE).update(payload)
           .eq("id", task_id).execute().data)
    _touch()
    return out


def set_done(task_id, done=True):
    """완료 체크. 되돌리면 완료 시각도 지운다."""
    out = (get_client().table(TABLE).update({
        "done": bool(done),
        "done_at": _now() if done else None,
        "updated_at": _now(),
    }).eq("id", task_id).execute().data)
    _touch()
    return out


def delete_task(task_id):
    out = get_client().table(TABLE).delete().eq("id", task_id).execute().data
    _touch()
    return out


# ---------------------------------------------------------------------------
# 우선순위 — 비서가 하는 일 (규칙 계산, 저장하지 않는다)
# ---------------------------------------------------------------------------
# 점수가 작을수록 위로 온다. 같은 점수면 기한 빠른 순 → 오래된 순.
#
# ⚠️ 순위를 바꾸려면 여기만 고친다. 화면·알림·홈이 모두 이 함수를 쓴다.

def priority_of(task, ref=None) -> dict:
    """업무 하나의 우선순위와 '왜 지금인지' 한 줄.

    Returns: {"rank": int, "level": "hi|mid|low", "label": str, "why": str}
    """
    ref = ref or today()
    due = _as_date(task.get("due_date"))
    owner = (task.get("owner") or "").strip()
    age = _age_days(task.get("created_at"), ref)

    if due and due < ref:
        n = (ref - due).days
        return {"rank": 0, "level": "hi", "label": "🔴 급함",
                "why": f"기한 {n}일 지났어요" if n > 1 else "어제까지였어요"}
    if due and due == ref:
        return {"rank": 1, "level": "hi", "label": "🔴 급함", "why": "오늘까지예요"}
    if due and (due - ref).days == 1:
        return {"rank": 2, "level": "hi", "label": "🔴 급함", "why": "내일까지예요"}
    if due and (due - ref).days <= 7:
        return {"rank": 3, "level": "mid", "label": "🟡 이번 주",
                "why": f"{(due - ref).days}일 남았어요"}
    # 기한이 없는 업무는 '얼마나 오래 방치됐나'로 본다 — 기한을 넣게 만드는 장치.
    if not due and age is not None and age >= STALE_DAYS:
        return {"rank": 4, "level": "mid", "label": "🟡 오래됨",
                "why": f"{age}일째 그대로예요"}
    if not owner:
        return {"rank": 5, "level": "mid", "label": "🟡 담당 없음",
                "why": "아무도 안 맡았어요"}
    return {"rank": 6, "level": "low", "label": "⚪ 여유",
            "why": f"{(due - ref).days}일 남았어요" if due else ""}


def _as_date(v):
    if not v:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v)[:10]).date()
    except ValueError:
        return None


def _age_days(created, ref):
    """등록한 지 며칠 됐나. 시간대가 섞여 있어도 날짜만 본다."""
    d = _as_date(created)
    return (ref - d).days if d else None


def dday_label(due, ref=None) -> str:
    """기한을 사람 말로 — 'D+2 지남' / '오늘' / '내일' / '9/5'."""
    d = _as_date(due)
    if not d:
        return ""
    ref = ref or today()
    gap = (d - ref).days
    if gap < 0:
        return f"D+{-gap} 지남"
    if gap == 0:
        return "오늘"
    if gap == 1:
        return "내일"
    return f"{d.month}/{d.day}"


# ---------------------------------------------------------------------------
# 조회 — 두 곳을 읽어 합친다
# ---------------------------------------------------------------------------

def open_tasks() -> list[dict]:
    """안 끝난 관리자 업무 전부(우선순위순).

    각 행: id(문자열 "w:12"/"m:34") · source("work"|"meeting") · content ·
           owner · due_date · created_at · memo · 출처 표시용 meeting_* ·
           pri(우선순위 dict) · dday
    회의 할 일까지 합치므로, 보드 한 곳에서 담당자·기한을 다 볼 수 있다.
    """
    ref = today()
    out = []
    for r in _work_rows():
        out.append(_view(r, "work", ref))
    for r in _meeting_rows():
        out.append(_view(r, "meeting", ref))
    # 우선순위 → 기한 빠른 순 → 오래된 순. 기한 없는 건 뒤로.
    out.sort(key=lambda t: (
        t["pri"]["rank"],
        t["due_date"] or "9999-12-31",
        t["created_at"] or "",
    ))
    return out


def _work_rows() -> list[dict]:
    try:
        return (get_client().table(TABLE).select("*")
                .eq("done", False).limit(MAX_OPEN).execute().data) or []
    except Exception:  # noqa: BLE001 — 표가 아직 없어도 보드는 떠야 한다
        return []


def _meeting_rows() -> list[dict]:
    """회의 할 일 — meeting_store 를 거쳐 회의 제목까지 함께 받는다."""
    try:
        from . import meeting_store as mt
        return mt.open_tasks(limit=MAX_OPEN) or []
    except Exception:  # noqa: BLE001
        return []


def _view(r, source, ref) -> dict:
    # 두 표가 필드 이름이 다르다 — 여기서 한 번만 맞춘다.
    #   기한: work_tasks 는 due_date, meeting_store 는 due 로 준다.
    #   생긴 날: 직접 등록은 created_at, 회의 할 일은 **그 회의 날짜**가
    #           곧 업무가 생긴 날이다(meeting_store 가 created_at 을 안 준다).
    due = r.get("due_date") or r.get("due")
    born = r.get("created_at") or r.get("meeting_date")
    pri = priority_of(
        {"owner": r.get("owner"), "due_date": due, "created_at": born}, ref)
    return {
        "id": f"{'w' if source == 'work' else 'm'}:{r.get('id')}",
        "raw_id": r.get("id"),
        "source": source,
        "content": r.get("content") or "",
        "owner": (r.get("owner") or "").strip(),
        "due_date": due,
        "created_at": born,
        "memo": r.get("memo"),
        "meeting_id": r.get("meeting_id"),
        "meeting_title": r.get("meeting_title"),
        "pri": pri,
        "dday": dday_label(due, ref),
    }


def owner_counts(tasks) -> list[dict]:
    """담당자별 건수 — 많은 순, '담당자 없음'은 항상 맨 뒤."""
    box = {}
    for t in tasks:
        box.setdefault(t["owner"], 0)
        box[t["owner"]] += 1
    rows = [{"owner": k, "n": v} for k, v in box.items() if k]
    rows.sort(key=lambda x: -x["n"])
    if box.get(""):
        rows.append({"owner": "", "n": box[""]})
    return rows


def top_priorities(tasks, limit=3) -> list[dict]:
    """'오늘 이것부터' — 여유(low)는 재촉하지 않는다."""
    return [t for t in tasks if t["pri"]["level"] != "low"][:limit]


def parse_id(task_id: str):
    """화면이 돌려준 "w:12" / "m:34" 를 (source, id) 로. 이상하면 (None, None)."""
    s = str(task_id or "")
    if ":" not in s:
        return None, None
    head, _, num = s.partition(":")
    if head not in ("w", "m") or not num.isdigit():
        return None, None
    return ("work" if head == "w" else "meeting"), int(num)


# ---------------------------------------------------------------------------
# 주간 보기 — 누가 무엇을 끝냈고, 무엇을 못 끝냈나 (사장님 2026-09-09)
# ---------------------------------------------------------------------------
# 보드는 '지금 열린 것'만 보여서 끝낸 업무는 사라진다. 여기서는 done_at 을
# 살려 주(월~일, KST)마다 담당자별로 되짚어 본다. 규칙은 둘뿐이다:
#   완료   = 그 주에 끝낸 것(done_at 이 그 주)
#   미완료 = 그 주가 기한이었는데 그 주 안에 못 끝낸 것
# 기한 없는 열린 업무는 '미완료'로 세지 않는다 — 어느 주의 잘못도 아니다.

def week_bounds(d: date) -> tuple[date, date]:
    """d 가 속한 주의 (월요일, 일요일)."""
    start = d - timedelta(days=d.weekday())
    return start, start + timedelta(days=6)


def _done_date(v):
    """done_at(UTC timestamptz) → 매장 기준(KST) 날짜. 없거나 깨지면 None."""
    if not v:
        return None
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(KST).date()


def bucket_week(rows, start: date, end: date) -> dict:
    """한 주치 판정 — 순수 함수라 테스트가 날짜만 고정해 돌린다.

    rows: _norm() 이 만든 dict 들(done/done_at/due_date/owner/content…).
    """
    done, missed = [], []
    for r in rows:
        dd = _done_date(r.get("done_at")) if r.get("done") else None
        due = _as_date(r.get("due_date"))
        if dd and start <= dd <= end:
            done.append({**r, "done_date": dd})
        elif due and start <= due <= end and (dd is None or dd > end):
            missed.append({**r, "due": due})
    done.sort(key=lambda r: (r["done_date"], r["id"]))
    missed.sort(key=lambda r: (r["due"], r["id"]))

    box = {}
    for r in done:
        box.setdefault(r["owner"], {"done": [], "missed": []})["done"].append(r)
    for r in missed:
        box.setdefault(r["owner"], {"done": [], "missed": []})["missed"].append(r)
    owners = [{"owner": k, **v} for k, v in box.items()]
    # 일 많은 순, '담당 없음'은 맨 뒤
    owners.sort(key=lambda o: (o["owner"] == "", -(len(o["done"]) + len(o["missed"]))))
    return {"start": start, "end": end, "done": done, "missed": missed,
            "owners": owners}


def _norm(r, source) -> dict:
    return {
        "id": f"{'w' if source == 'work' else 'm'}:{r.get('id')}",
        "source": source,
        "content": r.get("content") or "",
        "owner": (r.get("owner") or "").strip(),
        "due_date": r.get("due_date"),
        "done": bool(r.get("done")),
        "done_at": r.get("done_at"),
        "meeting_id": r.get("meeting_id"),
        "meeting_title": r.get("meeting_title"),
    }


def _all_rows() -> list[dict]:
    """열린 것·끝낸 것 전부(두 표). 관리자 업무는 수십 건이라 다 읽어도 가볍다."""
    out = []
    try:
        for r in (get_client().table(TABLE).select("*")
                  .limit(1000).execute().data or []):
            out.append(_norm(r, "work"))
    except Exception:  # noqa: BLE001
        pass
    try:
        rows = (get_client().table("meeting_tasks")
                .select("id,content,owner,due_date,done,done_at,meeting_id,"
                        "meetings(title,meeting_date)")
                .limit(1000).execute().data or [])
        for r in rows:
            r["meeting_title"] = (r.get("meetings") or {}).get("title")
            out.append(_norm(r, "meeting"))
    except Exception:  # noqa: BLE001
        pass
    return out


def weekly_report(weeks: int = 8) -> list[dict]:
    """최근 weeks 주(이번 주부터 과거로). 화면이 한 번에 받아 그 자리에서 넘긴다."""
    rows = _all_rows()
    cur_start, _ = week_bounds(today())
    out = []
    for i in range(weeks):
        start = cur_start - timedelta(days=7 * i)
        end = start + timedelta(days=6)
        w = bucket_week(rows, start, end)
        w["label"] = f"{start.month}/{start.day} – {end.month}/{end.day}"
        w["is_current"] = i == 0
        for r in w["missed"]:
            r["dday"] = dday_label(r["due_date"])
        out.append(w)
    return out
