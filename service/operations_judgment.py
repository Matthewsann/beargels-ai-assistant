"""오늘의 운영 판단 — "베어글스가 지금 뭘 처리해야 하나"를 몇 줄로 (Phase 3-C-1, 2026-09-12).

대시보드를 하나 더 만드는 게 아니다. 이미 있는 데이터(업무 보드·회의 할 일·
고객 요청·문제 리뷰)를 **규칙으로** 네 칸에 나눠 담는다:

    urgent(1) 가장 먼저 처리 → today(2) 오늘 처리 → check(3) 확인 필요 → watch(4) 지켜보기

원칙:
  · 순위 규칙은 새로 만들지 않는다 — 업무는 work_store.priority_of 의 rank 를 그대로
    읽는다(rank 0 기한 지남 = urgent, 1 오늘 = today, 5 담당 없음 = check, 4 오래됨 = watch).
    고객 요청은 일꾼의 잔소리 규칙(3일 넘게 미전파)과 같은 기준.
  · 알림을 만들지 않는다. 알림은 일꾼(worker/agent.py)이 notifications 표에 적고,
    여기서는 그 알림의 열쇠(dedupe_key)를 **같은 문자열로** 가리키기만 한다 —
    work.overdue:w:39 가 그 업무의 유일한 알림 정체성이어야 하니까.
  · 같은 업무가 두 칸에 들어가지 않는다(기한 지난 업무는 urgent 에만).
  · AI 없음, DB 쓰기 없음, 새 표 없음. 몇 번을 불러도 같은 입력이면 같은 답.
  · 빈 칸은 화면에 안 그린다(group 이 빈 칸을 뺀다).

입력은 화면(app.py)이 이미 읽어 둔 것을 넘긴다 — 여기서 supabase 를 새로 두드리지
않는다. 업무만 안 넘기면 work_store.open_tasks() 로 직접 읽는다.
"""

from datetime import date, datetime, timedelta

#: 칸 → 우선순위·표시. 순서 자체가 화면 순서.
CATEGORIES = (
    ("urgent", 1, "🔴", "가장 먼저 처리"),
    ("today", 2, "🟠", "오늘 처리"),
    ("check", 3, "🟡", "확인 필요"),
    ("watch", 4, "⚪", "지켜보기"),
)
PRIORITY = {c: p for c, p, _, _ in CATEGORIES}

#: 고객 요청이 이만큼 단톡방에 안 갔으면 '확인 필요' — 일꾼 maybe_request_nag 와 같은 값.
REQUEST_STALE_DAYS = 3

#: 한 칸에 몇 줄까지 그리나 — 사장님이 10초 안에 읽어야 한다. 나머지는 "+N건" 링크.
SHOW_PER_CATEGORY = 4


def _today():
    from database.work_store import today
    return today()


def _as_date(v):
    if not v:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v)[:10]).date()
    except ValueError:
        return None


def _anchor(task_id: str) -> str:
    # 업무 보드는 줄마다 id="w-39" 를 단다(work.html) — 눌러서 바로 그 줄로.
    return str(task_id or "").replace(":", "-")


def _item(category, title, reason, source_type, source_id, link, dedupe_key,
          has_notification=False, sort=()):
    return {
        "category": category,
        "priority": PRIORITY[category],
        "title": (title or "").strip()[:120],
        "reason": (reason or "").strip(),
        "source_type": source_type,
        "source_id": source_id,
        "link": link,
        "dedupe_key": dedupe_key,
        # 일꾼이 같은 열쇠로 notifications 표에 줄을 적는 종류인가. True 면 알림함의
        # 그 줄과 같은 문제다 — 여기서 또 만들지 않는다.
        "has_notification": has_notification,
        "_sort": tuple(sort),
    }


# ── 원천별 규칙 ──────────────────────────────────────────────────────────────

def _from_tasks(tasks, ref):
    """업무 보드 줄(work_store.open_tasks 의 view) → 판단. 하위 업무는 상위에 묻어간다."""
    out = []
    for i, t in enumerate(tasks or []):
        if t.get("done") or not t.get("id"):
            continue
        rank = (t.get("pri") or {}).get("rank")
        why = (t.get("pri") or {}).get("why") or ""
        owner = (t.get("owner") or "").strip()
        who = f"담당 {owner}" if owner else "담당자 없음"
        due = _as_date(t.get("due_date"))
        link = f"/work#{_anchor(t['id'])}"
        if rank == 0:
            late = (ref - due).days if due else 0
            out.append(_item("urgent", t.get("content"), f"{why} · {who}",
                             "work", t["id"], link, f"work.overdue:{t['id']}",
                             has_notification=True, sort=(-late, i)))
        elif rank == 1:
            out.append(_item("today", t.get("content"), f"{why} · {who}",
                             "work", t["id"], link, f"work.today:{t['id']}",
                             sort=(0, i)))
        elif rank == 5:
            out.append(_item("check", t.get("content"), "아무도 안 맡았어요 — 담당자를 정해 주세요",
                             "work", t["id"], link, f"work.unassigned:{t['id']}",
                             sort=(3, i)))
        elif rank == 4:
            out.append(_item("watch", t.get("content"), f"{why} · {who} — 기한을 정하거나 정리",
                             "work", t["id"], link, f"work.stale:{t['id']}",
                             sort=(0, i)))
        # rank 2(내일)·3(이번 주)·6(여유)은 판단에 안 올린다 — 보드에서 본다.
    return out


def _from_requests(requests, ref):
    """전파 안 된 고객 요청(customer_requests.find_requests 항목) → 3일 넘게 묵은 것만."""
    out = []
    cut = (ref - timedelta(days=REQUEST_STALE_DAYS)).isoformat()
    for r in requests or []:
        if not r.get("id"):
            continue
        seen = max(r.get("date") or "", r.get("collected") or "")
        if not seen or seen > cut:
            continue
        d = _as_date(seen)
        age = (ref - d).days if d else REQUEST_STALE_DAYS
        quote = (r.get("quote") or "")[:28]
        out.append(_item(
            "check", f"고객 요청 미전파 — [{r.get('topic') or '요청'}] {quote}…",
            f"{age}일째 단톡방에 안 갔어요 — 리뷰 현황에서 [복사]→[공유 완료]",
            "review", r["id"], "/review#requests",
            f"request.unshared:review:{r['id']}", has_notification=True,
            sort=(1, -age, r["id"])))
    return out


def _from_reviews(counts):
    """이미 있는 문제 리뷰 감지(건수) → 확인 필요. 새 규칙 없음."""
    out = []
    n = int((counts or {}).get("escalate") or 0)
    if n > 0:
        out.append(_item("check", f"사장님이 직접 답해야 할 리뷰 {n}건",
                         "민감 리뷰(이물질·환불·법적) — 자동 등록 안 됨, 직접 확인 후 등록",
                         "review", "escalate", "/todo", "review.escalate", sort=(0, 0)))
    n = int((counts or {}).get("attention") or 0)
    if n > 0:
        out.append(_item("check", f"답글 안 단 문제 리뷰 {n}건",
                         "별점 낮음·불만 리뷰 — 🚨 문제 탭에서 먼저 처리",
                         "review", "attention", "/care", "review.attention", sort=(2, 0)))
    return out


# ── 조립 ─────────────────────────────────────────────────────────────────────

def get_daily_judgments(tasks=None, requests=None, review_counts=None, today=None):
    """오늘의 운영 판단 목록 — 우선순위(1→4) 순, 같은 칸 안에서는 결정적 순서.

    Args:
        tasks: work_store.open_tasks() 결과(안 주면 직접 읽는다).
        requests: 전파 안 된 고객 요청 항목들(app 의 _customer_requests 가 준 것).
        review_counts: {"escalate": n, "attention": n} — 이미 세어 둔 건수.
        today: 기준일(KST). 테스트용.
    """
    ref = today or _today()
    if tasks is None:
        try:
            from database import work_store as wk
            tasks = wk.open_tasks()
        except Exception:  # noqa: BLE001 — 업무를 못 읽어도 나머지 판단은 낸다
            tasks = []
    items = _from_tasks(tasks, ref) + _from_requests(requests, ref) + _from_reviews(review_counts)
    # 같은 원천이 두 칸에 들어가지 않게 — 먼저 온(더 급한) 칸이 이긴다.
    seen, out = set(), []
    for it in sorted(items, key=lambda x: (x["priority"], x["_sort"])):
        sid = (it["source_type"], it["source_id"])
        if sid in seen:
            continue
        seen.add(sid)
        out.append({k: v for k, v in it.items() if k != "_sort"})
    return out


# ── 원천 상태 — '없음'과 '못 읽음'을 구분한다 (Phase 3-C-2, 2026-09-12) ──────────
#
# 원천 함수들은 조회가 실패해도 조용히 빈 목록·0 을 돌려준다(화면이 죽지 않게).
# 판단 엔진이 그걸 그대로 믿으면 "할 일 없음"이라는 거짓 판단이 된다. 그래서
# 판단에 넣기 전에 원천마다 ok/실패를 붙이고, 하나라도 실패하면 화면에 말한다.

#: 원천 이름 → 화면에 보일 말. 판단 원천이 늘면 여기에 한 줄.
SOURCE_LABELS = {
    "tasks": "업무 보드", "requests": "고객 요청",
    "escalate": "민감 리뷰 건수", "attention": "문제 리뷰 건수",
}
WARNING_TEXT = "일부 운영 데이터를 불러오지 못했습니다. 확인이 필요합니다."


def source(name, data=None, ok=True, error="", detail=False):
    """원천 결과 하나 — {"name", "ok", "data", "error"}. ok=False 면 data 는 부분값이거나 None.

    error: 로그용 짧은 문구. detail=True 면 사람이 읽을 꼬리표라 화면에도 붙는다
           (예: "회의 할 일" — 업무 보드 중 그 부분만 실패).
    """
    return {"name": name, "ok": bool(ok), "data": data,
            "error": str(error or "")[:120], "detail": bool(detail)}


def judge_sources(sources: dict, today=None) -> dict:
    """원천 결과 묶음 → {"judgments", "failed", "warning", "complete"}.

    sources: {"tasks": source(...), "requests": source(...), "escalate": source(...),
              "attention": source(...)} — 빠진 열쇠는 '못 읽음'으로 본다.
    실패한 원천의 data 가 부분값(예: 업무는 읽고 회의 할 일만 실패)이면 그 부분은
    판단에 쓴다 — 건강한 쪽의 판단까지 버리지 않는다. 단 warning 은 반드시 붙는다.
    """
    def get(k):
        s = (sources or {}).get(k)
        return s if s else source(k, None, ok=False, error="missing")
    t, r, e, a = get("tasks"), get("requests"), get("escalate"), get("attention")
    judgments = get_daily_judgments(
        tasks=t["data"] or [], requests=r["data"] or [],
        review_counts={"escalate": e["data"] or 0, "attention": a["data"] or 0},
        today=today)
    failed = []
    for k, s in (("tasks", t), ("requests", r), ("escalate", e), ("attention", a)):
        if not s["ok"]:
            label = SOURCE_LABELS.get(k, k)
            # error 는 사람 말로 된 짧은 꼬리표만(예: 업무 보드 중 "회의 할 일"만 실패).
            # 예외 문구는 로그에 있다 — 홈에는 안 싣는다.
            failed.append(f"{label}: {s['error']}" if s.get("detail") else label)
    warning = f"{WARNING_TEXT} — {', '.join(failed)}" if failed else ""
    return {"judgments": judgments, "failed": failed, "warning": warning,
            "complete": not failed}


def group(judgments, per=SHOW_PER_CATEGORY):
    """화면용 — 칸별로 묶고 빈 칸은 뺀다. 각 칸은 per 줄까지, 나머지는 more 로."""
    box = {c: [] for c, _, _, _ in CATEGORIES}
    for j in judgments or []:
        box.setdefault(j["category"], []).append(j)
    out = []
    for cat, pri, icon, label in CATEGORIES:
        rows = box.get(cat) or []
        if not rows:
            continue
        out.append({"category": cat, "priority": pri, "icon": icon, "label": label,
                    "items": rows[:per], "more": max(0, len(rows) - per),
                    "more_link": rows[per]["link"].split("#")[0] if len(rows) > per else ""})
    return out
