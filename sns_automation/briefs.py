"""콘텐츠 브리프 — 주제 하나가 촬영·릴스·블로그·성과를 관통하는 한 줄.

왜 필요한가(설계 검토 2026-09-04):
    인스타 아이디어·주간계획·블로그 글감·채널 배분안이 서로 다른 곳에 따로
    저장되고 **공통 번호가 없었다.** 유일한 연결 고리가 '폴더 이름 문자열'이라
    사장님이 오타를 내거나 제목을 바꾸면 흐름이 끊겼고, "이 주제가 블로그에선
    됐는데 릴스에선 안 됐다" 같은 판단을 아무도 할 수 없었다.

    → 브리프 하나 = 주제 하나. 여기에 채널별 지시(릴스 훅·샷 / 블로그 키워드)와
      결과(발행·성과)가 같이 붙고, 이 id 가 폴더·프로젝트·글·캘린더를 따라간다.

상태(하나뿐인 흐름):
    제안 → 촬영중 → 소재도착 → 제작중 → 발행 → 종료
      │       │         │          │        └ 성과가 붙고 판정 문장이 생긴다
      │       │         │          └ 릴스/글이 만들어지는 중
      │       │         └ 폴더에 파일이 들어와 입고 검수를 통과
      │       └ [찍을게요] — 폴더와 촬영가이드가 생겼다
      └ AI 가 제안했고 아직 아무도 안 골랐다

저장(집 PC 가 단일 필자):
    data/briefs.json        원본. worker 만 쓴다.
    state/briefs.json(버킷) 직원 웹이 읽는 사본 — push() 로 올린다.

새 Supabase 테이블을 만들지 않는다 — SQL 마이그레이션은 사장님이 직접 실행해야
하는 블로커라, 이미 있는 공개 버킷(cloud_sync)만으로 끝낸다.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.getenv("PIPELINE_DATA_DIR") or os.path.join(_ROOT, "data")
PATH = os.path.join(DATA_DIR, "briefs.json")
CLOUD_KEY = "state/briefs.json"

#: 상태 — 순서가 곧 진행도다.
PROPOSED, SHOOTING, ARRIVED, MAKING, PUBLISHED, CLOSED = (
    "제안", "촬영중", "소재도착", "제작중", "발행", "종료")
FLOW = (PROPOSED, SHOOTING, ARRIVED, MAKING, PUBLISHED, CLOSED)

#: 이 상태들이 '이번 주 할 일' 이다(종료·제안 제외).
LIVE = (SHOOTING, ARRIVED, MAKING, PUBLISHED)

MAX_KEEP = 40                       # 오래된 브리프는 이만큼만 보관


def _slug(text: str) -> str:
    s = re.sub(r"[^\w가-힣]+", "-", (text or "").strip()).strip("-")
    return s[:40] or "topic"


def new_id(topic: str, now: int | None = None) -> str:
    return f"b{int(now or time.time())}-{_slug(topic)}"


# ── 저장소 ────────────────────────────────────────────────────

def load() -> list[dict]:
    try:
        with open(PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save(items: list[dict]) -> None:
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    items = sorted(items, key=lambda b: b.get("created") or 0, reverse=True)[:MAX_KEEP]
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PATH)


def get(bid: str) -> dict | None:
    return next((b for b in load() if b.get("id") == bid), None)


def upsert(brief: dict) -> dict:
    """브리프 하나를 저장(있으면 갱신). 반환: 저장된 브리프."""
    items = load()
    brief["updated"] = int(time.time())
    for i, b in enumerate(items):
        if b.get("id") == brief.get("id"):
            items[i] = brief
            break
    else:
        brief.setdefault("created", int(time.time()))
        items.append(brief)
    save(items)
    return brief


def patch(bid: str, **fields) -> dict | None:
    """브리프의 일부 칸만 바꾼다. 중첩 dict(insta/blog/intake)는 병합한다."""
    b = get(bid)
    if not b:
        return None
    for k, v in fields.items():
        if isinstance(v, dict) and isinstance(b.get(k), dict):
            b[k].update({kk: vv for kk, vv in v.items() if vv is not None})
        else:
            b[k] = v
    return upsert(b)


def create(topic: str, *, why: str = "", insta: dict | None = None,
           blog: dict | None = None, source: str = "weekly",
           now: int | None = None) -> dict:
    """새 브리프 — AI 제안(run_ideas)이나 레퍼런스에서 만들어진다."""
    now = int(now or time.time())
    return upsert({
        "id": new_id(topic, now), "topic": topic.strip(), "why": why.strip(),
        "status": PROPOSED, "source": source, "created": now,
        "folder": "", "insta": insta or {}, "blog": blog or {},
        "intake": {}, "verdict": {},
    })


# ── 찾기 ─────────────────────────────────────────────────────

def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "")).casefold()


def by_folder(folder: str, items: list[dict] | None = None) -> dict | None:
    """소재 폴더 이름으로 찾는다. 폴더칸 → 주제명 순으로 본다."""
    name = _norm(os.path.basename(folder.rstrip("/\\")))
    if not name:
        return None
    items = items if items is not None else load()
    for b in items:
        if b.get("folder") and _norm(b["folder"]) == name:
            return b
    for b in items:
        if _norm(b.get("topic", "")) == name:
            return b
    return None


def by_project(project_id: str, items: list[dict] | None = None) -> dict | None:
    """릴스 프로젝트 id 로 찾는다."""
    items = items if items is not None else load()
    return next((b for b in items
                 if (b.get("insta") or {}).get("project_id") == project_id), None)


def by_post(post_id, items: list[dict] | None = None) -> dict | None:
    """블로그 글 번호로 찾는다."""
    items = items if items is not None else load()
    return next((b for b in items
                 if (b.get("blog") or {}).get("post_id") == post_id), None)


def live(items: list[dict] | None = None) -> list[dict]:
    """진행 중인 브리프 — 홈 '이번 주 콘텐츠' 가 읽는다(오래된 순)."""
    items = items if items is not None else load()
    return sorted([b for b in items if b.get("status") in LIVE],
                  key=lambda b: b.get("created") or 0)


def set_status(bid: str, status: str) -> dict | None:
    """상태를 앞으로만 옮긴다 — 늦게 도착한 잡이 진행을 되돌리지 않게."""
    b = get(bid)
    if not b:
        return None
    try:
        if FLOW.index(status) <= FLOW.index(b.get("status", PROPOSED)):
            return b
    except ValueError:
        return b
    b["status"] = status
    return upsert(b)


# ── 성과 (6단계) ─────────────────────────────────────────────

def record_insta(bid: str, **metrics) -> dict | None:
    """릴스 발행·성과를 브리프에 모은다(publish_sync 가 부른다)."""
    b = patch(bid, insta={k: v for k, v in metrics.items() if v is not None})
    if b and metrics.get("published_at"):
        set_status(bid, PUBLISHED)
        b = get(bid)
    return refresh_verdict(bid) if b else None


def record_blog(bid: str, **metrics) -> dict | None:
    """블로그 발행·순위·반응을 브리프에 모은다(blog_perf 가 부른다)."""
    b = patch(bid, blog={k: v for k, v in metrics.items() if v is not None})
    if b and metrics.get("published_at"):
        set_status(bid, PUBLISHED)
    return refresh_verdict(bid) if b else None


def _account_avg_likes() -> float:
    """내 계정 평균 좋아요 — 잘됐나 못됐나의 기준선."""
    try:
        from .market_scan import OWN_PATH, load as _load_scan
        return float((_load_scan(OWN_PATH) or {}).get("avg_likes") or 0)
    except Exception:  # noqa: BLE001 — 기준선이 없어도 판정은 나와야 한다
        return 0.0


def verdict_line(brief: dict, avg_likes: float | None = None) -> dict:
    """규칙 판정 — 숫자 나열 대신 다음 기획이 읽을 한 문장(AI 비용 0).

    "이 주제는 블로그에선 됐고 릴스에선 안 됐다" 처럼, 채널별로 갈라서 말한다.
    성과가 아직 없으면 빈 판정({})을 돌려준다.
    """
    avg = _account_avg_likes() if avg_likes is None else float(avg_likes)
    insta, blog = brief.get("insta") or {}, brief.get("blog") or {}
    parts, actions = [], []

    likes = insta.get("likes")
    saves = insta.get("saves")
    if insta.get("published_at") and likes is not None:
        if avg > 0 and likes >= avg * 1.3:
            parts.append(f"릴스는 잘 됐다(♥{likes} · 평균 {avg:.0f}의 "
                         f"{likes / avg:.1f}배)")
            actions.append(f"「{insta.get('hook_angle') or brief.get('topic')}」 각도를 한 번 더")
        elif avg > 0 and likes <= avg * 0.7:
            parts.append(f"릴스는 안 됐다(♥{likes} · 평균 {avg:.0f}에 못 미침)")
            actions.append("이 각도는 쉬고 다른 훅으로")
        else:
            parts.append(f"릴스는 보통(♥{likes})")
        if saves:
            parts[-1] += f", 저장 {saves}"

    rank = blog.get("rank")
    if blog.get("published_at") or rank is not None:
        kw = blog.get("keyword") or ""
        if rank and rank <= 10:
            parts.append(f"블로그는 됐다(「{kw}」 {rank}위)")
            actions.append(f"「{kw}」 결이 통한다 — 같은 계열 키워드로 이어서")
        elif rank:
            parts.append(f"블로그는 아직(「{kw}」 {rank}위)")
        else:
            parts.append(f"블로그는 순위권 밖(「{kw}」)")
            actions.append(f"「{kw}」 는 경쟁이 세다 — 더 좁은 말로")

    if not parts:
        return {}
    return {"line": " / ".join(parts), "next": actions[:2], "at": int(time.time())}


def refresh_verdict(bid: str) -> dict | None:
    b = get(bid)
    if not b:
        return None
    v = verdict_line(b)
    if v:
        b["verdict"] = v
        return upsert(b)
    return b


def as_prompt_context(items: list[dict] | None = None, limit: int = 6) -> str:
    """다음 기획 프롬프트에 넣을 되먹임 — 숫자가 아니라 판정 문장."""
    items = items if items is not None else load()
    done = [b for b in items if (b.get("verdict") or {}).get("line")]
    if not done:
        return ""
    done.sort(key=lambda b: b["verdict"].get("at") or 0, reverse=True)
    lines = ["[지난 주제가 어떻게 됐나 — 이 판정을 근거로 다음을 고른다]"]
    for b in done[:limit]:
        lines.append(f"· 「{b['topic']}」 — {b['verdict']['line']}")
        for a in b["verdict"].get("next") or []:
            lines.append(f"    → {a}")
    return "\n".join(lines)


# ── 직원 웹으로 올리기 ────────────────────────────────────────

def to_card(b: dict) -> dict:
    """화면이 쓰는 만큼만 추린 카드(영상·본문 같은 무거운 건 안 올린다)."""
    insta, blog, intake = b.get("insta") or {}, b.get("blog") or {}, b.get("intake") or {}
    return {
        "id": b.get("id"), "topic": b.get("topic"), "why": b.get("why"),
        "status": b.get("status"), "folder": b.get("folder"),
        "created": b.get("created"), "source": b.get("source"),
        "hook_angle": insta.get("hook_angle"), "shots": insta.get("shots") or [],
        "project_id": insta.get("project_id"),
        "insta_published": insta.get("published_at"), "likes": insta.get("likes"),
        "keyword": blog.get("keyword"), "keyword_tier": blog.get("tier"),
        "blog_angle": blog.get("angle"), "post_id": blog.get("post_id"),
        "blog_published": blog.get("published_at"), "rank": blog.get("rank"),
        "intake": {"checked_at": intake.get("checked_at"), "ok": intake.get("ok"),
                   "bad": (intake.get("bad") or [])[:4],
                   "missing": (intake.get("missing") or [])[:4]},
        "verdict": (b.get("verdict") or {}).get("line"),
    }


def push(items: list[dict] | None = None) -> None:
    """직원 웹이 읽을 사본을 버킷에 올린다. 실패해도 로컬 원본은 그대로."""
    from . import cloud_sync
    items = items if items is not None else load()
    body = json.dumps({"updated": int(time.time()),
                       "briefs": [to_card(b) for b in items[:MAX_KEEP]]},
                      ensure_ascii=False).encode("utf-8")
    cloud_sync._bucket().upload(
        CLOUD_KEY, body,
        {"content-type": "application/json; charset=utf-8", "upsert": "true"})


def load_cloud(c=None) -> dict:
    """직원 웹(PA)이 부른다 — 버킷의 사본만 읽는다(로컬 파일 없음)."""
    from . import cloud_sync
    try:
        raw = cloud_sync._bucket(c).download(CLOUD_KEY)
        return json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001 — 없으면 빈 목록(첫 실행)
        return {}
