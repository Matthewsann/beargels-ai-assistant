"""릴스 발행 기록 + 인스타 자동 감지 — 5단계(사람이 올림)와 6단계(성과)를 잇는 고리.

왜 필요한가(설계 검토 2026-09-04):
    발행은 사람이 한다(확정). 그런데 "올렸다"는 사실을 시스템에 알리는 길이
    직원 웹에 없어서 훅 라이브러리 4건이 전부 미발행·성과 빈칸이었고,
    6단계 피드백(성과 → 다음 기획)이 한 번도 돌지 못했다.
    → 원칙: **발행은 사람이, 발행 사실은 시스템이 반드시 안다.**

두 입구, 한 함수:
    · 직원 웹 [📤 인스타에 올렸어요] → 잡(reel_published) → mark_reel_published()
      (게시물 ID 는 아직 모른다 — 다음 동기화가 캡션으로 찾아 붙인다)
    · 집 PC 가 6시간마다 내 계정 게시물을 읽어 캡션으로 완성본과 맞춘다
      (sync_published_reels) → 버튼을 안 눌러도 알아챈다. 블로그가 RSS 로
      발행을 자동 감지하는 것과 같은 원리.

기록되는 곳(네 군데가 한 번에):
    · projects/<id>/project.json  published / published_at / ig_permalink / ig_media_id
    · data/hook_library.json      published + 성과(좋아요·댓글, 권한 있으면 도달·저장·공유)
    · Supabase mkt_campaigns      auto_record("reel#<id>") — 마케팅 캘린더
    · reels/index.json(버킷)      완성본 카드에 ✅ 표시·숫자 (직원 웹이 읽는다)

규칙 하나 — **다시 만들면 새 판이다.** '틀린 말 고치기'나 같은 폴더로 다시
만들어 완성본이 새로 올라가면(new_version) 발행 기록은 published_history 로
내려가고 카드엔 [올렸어요]가 다시 뜬다. 옛 게시물은 새 판에 붙지 않는다.

잘못 눌렀으면 되돌린다(unmark_reel_published) — 직원 웹은 공유 키 하나로
열리므로 누구나 누를 수 있고, 되돌리는 길이 없으면 오기록이 영구히 남는다.

PA(직원 웹)에서는 이 모듈을 import 하지 않는다 — 로컬 프로젝트 파일과
FastAPI 앱(webapp)을 끌어오기 때문에 집 PC 일꾼 전용이다.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

#: 캡션이 이 정도 닮았으면 같은 릴스로 본다. AI 캡션은 100~300자라 사장님이
#: Edits 앱에서 몇 단어 고쳐도 0.6 은 넘고, 다른 릴스끼리는 0.3 안팎이다
#: (2026-09-04 실측: 실제 게시물 30개 vs 완성본 2편, 최고 유사도 0.27).
MIN_RATIO = 0.6
#: 발행 뒤 이 기간 안의 게시물만 성과를 갱신한다(오래된 건 숫자가 굳었다).
REFRESH_DAYS = 30
#: 시간 창의 여유 — 완성본보다 이만큼 이전 게시물까지는 후보로 본다.
SLACK = 86400

_HASHTAG_RE = re.compile(r"#\S+")
_MENTION_RE = re.compile(r"@\S+")
_NONWORD_RE = re.compile(r"[^\w가-힣]+")
#: 프로젝트 id 는 webapp._new_project 의 `<epoch>-<슬러그>` 꼴. 그 밖의 문자
#: (경로 구분자·꺾쇠·따옴표…)는 받지 않는다 — 잡 메시지·오류 문구·파일 경로에
#: 그대로 들어가기 때문이다.
_PID_RE = re.compile(r"[\w가-힣][\w가-힣.\-]{0,119}")
_PUBLISH_KEYS = ("published", "published_at", "published_source",
                 "ig_media_id", "ig_permalink")


class PublishError(RuntimeError):
    """사람이 읽고 조치할 수 있는 실패 메시지."""


def check_pid(pid: str) -> str:
    """릴스(프로젝트) id 검증 — 폴더 이름으로 쓰이므로 경로 조작을 막는다."""
    pid = (pid or "").strip()
    if (not pid or pid in (".", "..") or not _PID_RE.fullmatch(pid)
            or os.path.basename(pid) != pid):
        raise PublishError("릴스 id 가 올바르지 않아요. 화면을 새로고침한 뒤 다시 눌러주세요.")
    return pid


# ── 캡션 맞추기(순수 함수 — 테스트 대상) ────────────────────────

def normalize_caption(text: str) -> str:
    """해시태그·멘션·문장부호·이모지를 빼고 글자만 남긴다(비교용)."""
    t = _HASHTAG_RE.sub(" ", text or "")
    t = _MENTION_RE.sub(" ", t)
    t = _NONWORD_RE.sub(" ", t)
    return " ".join(t.lower().split())


def first_line(text: str) -> str:
    """캡션 첫 줄(=훅). 해시태그만 있는 줄은 건너뛴다."""
    for raw in (text or "").splitlines():
        line = normalize_caption(raw)
        if len(line) >= 4:
            return line
    return ""


def caption_similarity(a: str, b: str) -> float:
    """0~1. 전체 유사도와 '첫 줄이 같다'를 함께 본다."""
    na, nb = normalize_caption(a), normalize_caption(b)
    if not na or not nb:
        return 0.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    fa, fb = first_line(a), first_line(b)
    if fa and fb and len(fa) >= 8 and fa == fb:
        ratio = max(ratio, 0.95)
    return ratio


def parse_ig_time(ts: str | None) -> int:
    """그래프 API 시각('2026-09-03T13:55:53+0000') → epoch. 못 읽으면 0."""
    if not ts:
        return 0
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return int(datetime.strptime(ts, fmt).timestamp())
        except ValueError:
            continue
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def norm_url(url: str | None) -> str:
    """게시물 주소 비교용 — 쿼리·끝 슬래시·프로토콜 차이를 무시한다."""
    u = (url or "").strip().split("?", 1)[0].split("#", 1)[0].rstrip("/").lower()
    return re.sub(r"^https?://(www\.)?", "", u)


def project_caption(p: dict) -> str:
    """프로젝트가 인스타에 올렸을 캡션 — 최종 캡션이 우선, 없으면 훅."""
    cap = (p.get("script_caption") or p.get("caption") or "").strip()
    if cap:
        return cap
    hook = p.get("hook") or ((p.get("shot_plan") or {}).get("hook") or {}).get("text") or ""
    return str(hook).strip()


def anchor_time(p: dict) -> int:
    """이 완성본이 '만들어진' 시각 — 다시 만든 판은 rendered_at, 아니면 created.

    (`updated` 는 발행 기록 등 저장할 때마다 바뀌어 기준으로 못 쓴다.)
    """
    return int(p.get("rendered_at") or p.get("created") or 0)


def _in_window(p: dict, when: int, slack: int) -> bool:
    """게시 시각이 이 완성본에 붙을 수 있는 시간 창 안인가.

    아래: 완성본보다 하루 넘게 오래된 게시물은 이 릴스가 아니다(예전에 올린 같은 메뉴).
    위: 이미 [올렸어요]를 누른 완성본이면 누른 시각보다 하루 넘게 뒤의 게시물도 아니다.
    """
    if not when:
        return True
    lo = anchor_time(p)
    if lo and when < lo - slack:
        return False
    hi = int(p.get("published_at") or 0)
    if hi and when > hi + slack:
        return False
    return True


def match_post(p: dict, posts: list[dict], *, min_ratio: float = MIN_RATIO,
               slack_seconds: int = SLACK) -> tuple[dict, float] | None:
    """완성본 프로젝트 하나에 가장 닮은 내 게시물. 없으면 None."""
    got = assign_posts([p], posts, min_ratio=min_ratio, slack_seconds=slack_seconds)
    return (got[0][1], got[0][2]) if got else None


def assign_posts(projects: list[dict], posts: list[dict], *, min_ratio: float = MIN_RATIO,
                 slack_seconds: int = SLACK) -> list[tuple[dict, dict, float]]:
    """여러 완성본 ↔ 여러 게시물을 1:1 로 배정한다. [(project, post, ratio)].

    한 프로젝트씩 탐욕적으로 고르면 같은 메뉴를 두 번 만든 경우(폴백 캡션이
    똑같다) 새 프로젝트가 옛 릴스의 게시물을 가져간다. 그래서 (프로젝트, 게시물)
    쌍 전부를 유사도순 → 시각이 가까운 순으로 늘어놓고 위에서부터 짝을 짓는다.
    """
    pairs = []
    for p in projects:
        cap = project_caption(p)
        if not cap:
            continue
        a = anchor_time(p)
        for post in posts:
            when = parse_ig_time(post.get("timestamp"))
            if not _in_window(p, when, slack_seconds):
                continue
            r = caption_similarity(cap, post.get("caption") or "")
            if r < min_ratio:
                continue
            closeness = abs(when - a) if (when and a) else 10 ** 9
            pairs.append((round(r, 2), closeness, p, post, r))
    pairs.sort(key=lambda t: (-t[0], t[1]))
    taken_p, taken_post, out = set(), set(), []
    for _, _, p, post, r in pairs:
        if id(p) in taken_p or post.get("id") in taken_post:
            continue
        taken_p.add(id(p))
        taken_post.add(post.get("id"))
        out.append((p, post, r))
    return out


# ── 기록 (세 곳 + 카드) ───────────────────────────────────────

def _completed_projects() -> list[dict]:
    """완성본이 있는 프로젝트 전부(최신순). 로컬 projects/ 를 읽는다."""
    from . import webapp as wa
    out = []
    root = wa.PROJECTS_DIR
    if not os.path.isdir(root):
        return out
    for name in os.listdir(root):
        p = wa._load_project(name)
        if p and (p.get("final_path") or p.get("status") == wa.ST_DONE):
            out.append(p)
    out.sort(key=lambda x: x.get("created") or 0, reverse=True)
    return out


def _load(pid: str) -> dict:
    from . import webapp as wa
    p = wa._load_project(check_pid(pid))
    if not p:
        logger.warning("발행 기록 — 프로젝트 없음: %s", pid)
        raise PublishError("그 릴스의 프로젝트를 집 PC 에서 찾을 수 없어요. "
                           "완성본 목록을 새로고침한 뒤 다시 눌러주세요.")
    return p


def new_version(p: dict, now: int | None = None) -> dict:
    """완성본을 다시 만들 때(재렌더·재제작) 부른다 — 새 판은 아직 안 올린 것이다.

    이전 발행 기록은 published_history 로 내려 옛 게시물이 새 판에 붙지 않게
    하고(sync 가 history 의 media_id 를 제외한다), rendered_at 을 찍어 시간 창의
    기준으로 삼는다. 프로젝트 dict 를 고치기만 한다 — 저장은 호출부가.
    훅 라이브러리까지 함께 봉인하려면 start_new_version() 을 쓴다.
    """
    now = int(now or time.time())
    if p.get("published") or p.get("ig_media_id"):
        p.setdefault("published_history", []).append(
            {**{k: p.get(k) for k in _PUBLISH_KEYS[1:]}, "archived_at": now})
    for k in _PUBLISH_KEYS:
        p.pop(k, None)
    p["rendered_at"] = now
    return p


def start_new_version(p: dict, now: int | None = None) -> dict:
    """new_version + 훅 라이브러리의 지금 판 기록을 옛 판으로 봉인(archive_hooks).

    재렌더 경로(auto_make.make_video/make_reel, 로컬 웹 finalize)가 record_hook
    으로 새 판의 훅을 적기 **전에** 부른다. 그래야 새 판의 발행·성과·되돌리기가
    옛 판 훅의 게시물·숫자를 덮지 않는다.
    """
    from . import planner
    new_version(p, now)
    planner.archive_hooks(p["id"], version=p["rendered_at"])
    return p


def _calendar_ref(p: dict) -> str:
    """MKT 캘린더 자동 기록 마커 — 판마다 다르게(두 번째 발행도 기록되고, 취소가 옛 판을 안 지운다)."""
    ref = f"reel#{p['id']}"
    if p.get("rendered_at"):
        ref += f"@{int(p['rendered_at'])}"
    return ref


def mark_reel_published(pid: str, *, url: str | None = None, at: int | None = None,
                        media_id: str | None = None, source: str = "manual",
                        likes: int | None = None, comments: int | None = None) -> dict:
    """릴스 하나를 '발행됨'으로 기록한다. 몇 번 불러도 안전(멱등).

    source: 'manual'(직원 웹·로컬 웹 버튼) | 'auto'(인스타 자동 감지)
    at: 발행(또는 버튼을 누른) 시각. 이미 기록돼 있으면 처음 시각을 지킨다.
    반환: {pid, title, at, already} — already 는 이미 발행 기록이 있었다는 뜻.
    """
    from . import cloud_sync, planner
    from . import webapp as wa

    p = _load(pid)
    at = int(at or time.time())
    title = (p.get("title") or p.get("menu") or "").strip()
    already = bool(p.get("published"))

    p["published"] = True
    p["published_at"] = int(p.get("published_at") or at)
    if not already:
        p["published_source"] = source
    if url:
        p["ig_permalink"] = url
    if media_id:
        p["ig_media_id"] = media_id
    wa._save_project(p)

    planner.mark_published(pid, at=p["published_at"], url=url, media_id=media_id)
    if likes is not None or comments is not None:
        planner.record_project_result(pid, likes=likes, comments=comments)

    # 마케팅 캘린더 — 릴스 발행 = 마케팅 실행 (사장님 지시 2026-08-30).
    # auto_record 는 스스로 예외를 삼키고 중복(reel#pid)도 막는다.
    try:
        from database import mkt_store
        day = datetime.fromtimestamp(p["published_at"], KST).strftime("%Y-%m-%d")
        mkt_store.auto_record(
            title=f"릴스: {title}" if title else "릴스 발행",
            source_ref=_calendar_ref(p), day=day,
            memo="릴스 발행 " + ("자동 감지" if source == "auto" else "기록"))
    except Exception as e:  # noqa: BLE001 — 기록 실패가 발행 표시를 막으면 안 된다
        logger.warning("캘린더 자동 기록 실패(%s): %s", pid, str(e)[:120])

    # 직원 웹 완성본 카드에 ✅ — 실패해도 로컬 기록은 이미 끝났다.
    try:
        cloud_sync.mark_published(pid, p["published_at"], url=url or p.get("ig_permalink"),
                                  likes=likes, comments=comments,
                                  source=p.get("published_source"))
    except Exception as e:  # noqa: BLE001
        logger.warning("완성본 카드 발행 표시 실패(%s): %s", pid, str(e)[:120])

    # 콘텐츠 브리프에도 — 6단계(성과 → 다음 기획)가 채널을 갈라 판정하는 자리.
    try:
        from . import briefs
        b = briefs.by_project(pid) or briefs.by_folder(p.get("source_dir") or "")
        if b:
            briefs.record_insta(b["id"], project_id=pid, published_at=p["published_at"],
                                permalink=url or p.get("ig_permalink"),
                                likes=likes, comments=comments)
            briefs.push()
    except Exception as e:  # noqa: BLE001 — 브리프가 없어도 발행 기록은 남는다
        logger.warning("브리프 성과 기록 실패(%s): %s", pid, str(e)[:120])

    logger.info("릴스 발행 기록: %s (%s%s)", title or pid, source,
                ", 이미 있음" if already else "")
    return {"pid": pid, "title": title, "at": p["published_at"], "already": already}


def unmark_reel_published(pid: str) -> dict:
    """[잘못 눌렀어요] — 발행 기록을 네 곳에서 되돌린다. 성과 숫자도 지운다."""
    from . import cloud_sync, planner
    from . import webapp as wa

    p = _load(pid)
    title = (p.get("title") or p.get("menu") or "").strip()
    was = bool(p.get("published"))
    ref = _calendar_ref(p)
    if p.get("ig_media_id"):
        # 떼어낸 게시물은 history 에 남겨 다음 동기화가 같은 게시물을 다시 붙이지
        # 않게 한다(자동 감지가 엉뚱한 게시물을 잡았을 때 사람이 고치는 길).
        p.setdefault("published_history", []).append(
            {**{k: p.get(k) for k in _PUBLISH_KEYS[1:]},
             "archived_at": int(time.time()), "reason": "unmarked"})
    for k in _PUBLISH_KEYS:
        p.pop(k, None)
    wa._save_project(p)
    planner.unmark_published(pid)
    try:
        from database import mkt_store
        mkt_store.delete_auto_record(ref)
    except Exception as e:  # noqa: BLE001
        logger.warning("캘린더 기록 삭제 실패(%s): %s", pid, str(e)[:120])
    try:
        cloud_sync.unmark_published(pid)
    except Exception as e:  # noqa: BLE001
        logger.warning("완성본 카드 발행 표시 해제 실패(%s): %s", pid, str(e)[:120])
    logger.info("릴스 발행 기록 취소: %s%s", title or pid, "" if was else " (기록 없었음)")
    return {"pid": pid, "title": title, "was": was}


# ── 자동 감지 + 성과 갱신 (집 PC 정기 점검) ─────────────────────

def sync_published_reels(*, client=None, limit: int = 30, now: int | None = None) -> list[str]:
    """내 계정 게시물을 읽어 ①게시물이 안 붙은 완성본에 게시물을 붙이고 ②성과를 갱신.

    ① 의 대상은 '아직 게시물 ID 가 없는' 완성본 전부 — 버튼을 안 누른 것(발행
    감지)과 버튼만 누른 것(게시물 연결) 둘 다. 주소를 적어 뒀으면 주소로, 아니면
    캡션 유사도로 1:1 배정한다. 옛 판의 게시물(published_history)은 후보에서 뺀다.
    그래프 API 호출: 게시물 목록 1회 + (권한 있을 때만) 게시물별 인사이트.
    반환: 사람이 읽을 요약 문장들. 실패는 예외로 올린다(호출부가 기록).
    """
    from . import cloud_sync, planner
    from .meta_graph import MetaGraphError, from_env

    api = client or from_env()
    posts = [x for x in api.my_media(limit=limit) if x.get("id")]
    now = int(now or time.time())
    notes: list[str] = []

    projects = _completed_projects()
    used = {p.get("ig_media_id") for p in projects if p.get("ig_media_id")}
    for p in projects:
        used.update(h.get("ig_media_id") for h in p.get("published_history") or []
                    if h.get("ig_media_id"))

    def attach(p: dict, post: dict, ratio: float | None) -> None:
        was = bool(p.get("published"))
        mark_reel_published(
            p["id"], url=post.get("permalink"),
            at=parse_ig_time(post.get("timestamp")) or now, media_id=post["id"],
            source=("auto" if not was else (p.get("published_source") or "manual")),
            likes=post.get("like_count"), comments=post.get("comments_count"))
        used.add(post["id"])
        how = f"유사도 {ratio:.2f}" if ratio is not None else "주소 일치"
        notes.append(f"'{p.get('title') or p['id']}' {'게시물 연결' if was else '발행 감지'} ({how})")

    # ① 게시물 ID 가 없는 완성본 ↔ 게시물
    pending = [p for p in projects if not p.get("ig_media_id")]
    by_link = {norm_url(x.get("permalink")): x for x in posts if x.get("permalink")}
    for p in list(pending):
        post = by_link.get(norm_url(p.get("ig_permalink"))) if p.get("ig_permalink") else None
        if post and post["id"] not in used:
            attach(p, post, None)
            pending.remove(p)
    found = 0
    for p, post, ratio in assign_posts(pending, [x for x in posts if x["id"] not in used]):
        attach(p, post, ratio)
        found += 1

    # ② 발행된 릴스의 성과 갱신 — 좋아요·댓글은 목록에 이미 있고,
    #    도달·저장·공유는 instagram_manage_insights 권한이 있을 때만.
    by_id = {x["id"]: x for x in posts}
    can_insights: bool | None = None
    refreshed = 0
    for p in _completed_projects():                      # ① 의 기록을 반영한 최신본
        mid = p.get("ig_media_id")
        if not (p.get("published") and mid and mid in by_id):
            continue
        if now - int(p.get("published_at") or 0) > REFRESH_DAYS * 86400:
            continue
        post = by_id[mid]
        metrics = {"likes": post.get("like_count"), "comments": post.get("comments_count")}
        if can_insights is None:
            try:
                can_insights = not api.missing_optional_scopes()
            except Exception:  # noqa: BLE001 — 권한 조회 실패 = 없다고 본다
                can_insights = False
        if can_insights:
            try:
                ins = api.media_insights(mid)
                metrics.update(reach=ins.get("reach"), saves=ins.get("saved"),
                               shares=ins.get("shares"))
            except MetaGraphError as e:
                logger.debug("인사이트 조회 실패(%s): %s", mid, e)
        planner.record_project_result(p["id"], **metrics)
        try:
            from . import briefs
            b = briefs.by_project(p["id"])
            if b:
                briefs.record_insta(b["id"], **metrics)
        except Exception as e:  # noqa: BLE001
            logger.debug("브리프 성과 갱신 실패(%s): %s", p["id"], e)
        try:
            cloud_sync.mark_published(p["id"], int(p.get("published_at") or now),
                                      url=p.get("ig_permalink"),
                                      likes=metrics.get("likes"),
                                      comments=metrics.get("comments"),
                                      reach=metrics.get("reach"))
        except Exception as e:  # noqa: BLE001
            logger.debug("카드 성과 갱신 실패(%s): %s", p["id"], e)
        refreshed += 1

    if refreshed:
        try:
            from . import briefs
            briefs.push()
        except Exception as e:  # noqa: BLE001
            logger.debug("브리프 업로드 실패: %s", e)
    if not notes:
        notes.append("새로 감지된 발행 없음")
    notes.append(f"성과 갱신 {refreshed}건"
                 + ("" if can_insights else " (도달·저장은 권한 없어 좋아요·댓글만)"))
    return notes
