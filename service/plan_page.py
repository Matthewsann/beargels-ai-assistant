"""콘텐츠 기획 화면(/mkt) 조립 — 사장님 확정 2026-09-17.

이 화면은 **매니저**다. 어떤 주제를 어떤 콘텐츠로 올리면 좋을지 제안하고
(콘텐츠 브리프), 고른 주제를 블로그·인스타 프로그램에 넘긴다. 예전 마케팅
캘린더 자리에 들어왔고, 캘린더 기능은 전부 걷어냈다.

데이터는 새로 만들지 않는다 — 집 PC 가 올려 둔 브리프 사본(`state/briefs.json`,
`briefs.to_card`) 하나만 읽어 세 구역으로 나눈다:

    ① 매니저 제안   상태 '제안' (접은 것 제외)      → [📸 이거 찍을게요] [이건 안 할래요]
    ② 진행 중       촬영중 · 소재도착 · 제작중       → 주제마다 다음 할 일 하나
    ③ 끝난 것       발행 · 종료                     → 성과와 판정(다음 제안에 되먹임)

전부 규칙이다(AI 비용 0). 제안을 새로 짜는 것만 AI 이고, 그건 사장님이
[💡 새 제안 받기]를 눌렀을 때만 돈다(자동 주기 없음 — 사장님 지시).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

PROPOSED, SHOOTING, ARRIVED, MAKING, PUBLISHED, CLOSED = (
    "제안", "촬영중", "소재도착", "제작중", "발행", "종료")

#: 진행 중 구역의 정렬 — 사장님 손이 필요한 것부터
_LIVE_ORDER = {ARRIVED: 0, SHOOTING: 1, MAKING: 2}

#: 네이버 실측 등급 → 화면 말 (naver_search 의 판정과 같은 말)
_TIER = {"green": "✅ 지금 쓰면 이긴다", "yellow": "🟡 각도를 좁혀서",
         "mine": "🏠 우리 글이 이미 상위", "tiny": "검색량 적음", "red": "경쟁 셈"}

#: 제안은 이만큼만 펼쳐 보이고 나머지는 접는다(한눈에 — 사장님: 복잡하면 안 쓴다)
SHOW_PROPOSALS = 3
SHOW_DONE = 8

#: 이 종류의 잡이 돌고 있으면 화면이 '매니저가 일하는 중'을 보여주고 새로 고친다
_BUSY_KINDS = {"reel_ideas": "매니저가 새 제안을 짜는 중이에요 — 1~2분 걸려요.",
               "reel_ref": "매니저가 주제에 가이드를 붙이는 중이에요 — 1분쯤 걸려요.",
               "brief_dismiss": "제안을 접는 중이에요.",
               "reel_shoot": "촬영 폴더와 가이드를 만드는 중이에요.",
               "content_intake": "올린 소재를 확인하는 중이에요."}


def _day(ts) -> str:
    try:
        d = datetime.fromtimestamp(int(ts), KST)
        return f"{d.month}/{d.day}"
    except (TypeError, ValueError, OSError):
        return ""


def _secs(shots) -> int:
    n = 0
    for s in shots or []:
        try:
            n += int(float(s.get("secs") or 0))
        except (TypeError, ValueError):
            pass
    return n


def _card(c: dict) -> dict:
    """브리프 카드 → 화면용. 없는 값은 빈 채로 둔다(지어내지 않는다)."""
    intake = c.get("intake") or {}
    status = c.get("status")
    out = {
        "id": c.get("id") or "", "topic": c.get("topic") or "", "why": c.get("why") or "",
        "status": status, "folder": c.get("folder") or "", "day": _day(c.get("created")),
        "owner_topic": c.get("source") == "owner", "from_ref": c.get("source") == "ref",
        "hook": c.get("hook_angle") or "", "shots": c.get("shots") or [],
        "shot_secs": _secs(c.get("shots")),
        "keyword": c.get("keyword") or "", "angle": c.get("blog_angle") or "",
        "tier": _TIER.get(c.get("keyword_tier") or "", ""),
        "post_id": c.get("post_id"), "project_id": c.get("project_id"),
        "likes": c.get("likes"), "comments": c.get("comments"), "rank": c.get("rank"),
        "insta_published": bool(c.get("insta_published")),
        "blog_published": bool(c.get("blog_published")),
        "verdict": c.get("verdict") or "", "verdict_next": c.get("verdict_next") or [],
        "bad": intake.get("bad") or [], "missing": intake.get("missing") or [],
        "intake_ok": intake.get("ok"),
    }
    # ② 진행 중 — 주제마다 다음 할 일. 버튼은 기존 라우트를 그대로 부른다.
    acts, note = [], ""
    if status == SHOOTING:
        note = "찍어서 폴더에 올려주세요" + (f" (📁 {out['folder']})" if out["folder"] else "")
        acts.append(("intake", "📥 올린 소재 확인", False))
    elif status == ARRIVED:
        note = "소재가 도착했어요"
        acts.append(("reel", "🎬 릴스 만들기", True))
    elif status == MAKING:
        note = "인스타·블로그 화면에서 확인하고 올리면 돼요"
    if status in (ARRIVED, MAKING) and out["keyword"] and not out["post_id"]:
        acts.append(("blog", "📝 블로그 초안", status == MAKING))
    out["acts"], out["note"] = acts, note
    return out


def _now_line(props, live) -> dict:
    """맨 위 '👉 지금 할 일' — 다음에 누를 것 하나만 말한다."""
    for c in live:
        if c["status"] == ARRIVED:
            return {"text": f"「{c['topic']}」 소재가 도착했어요. 아래에서 [🎬 릴스 만들기]를 눌러주세요."}
    for c in live:
        if c["status"] == SHOOTING:
            return {"text": f"「{c['topic']}」 찍어서 폴더에 올려주세요. 올리면 10분 안에 검수 결과를 알려드려요."}
    for c in live:
        if c["status"] == MAKING:
            return {"text": f"「{c['topic']}」 완성본·초안을 확인하고 직접 올려주세요."}
    if props:
        return {"text": f"제안 {len(props)}개 중에 찍을 주제를 골라 [📸 이거 찍을게요]를 눌러주세요. "
                        "촬영 폴더와 가이드가 바로 생겨요."}
    return {"text": "아직 제안이 없어요. [💡 새 제안 받기]를 누르면 매니저가 주제를 골라 드려요."}


def build_view(cards, job=None) -> dict:
    cards = [_card(c) for c in (cards or []) if not c.get("dismissed")]
    props = sorted((c for c in cards if c["status"] == PROPOSED),
                   key=lambda c: c["id"], reverse=True)
    # id 는 b<만든 시각>-… 이라 문자열 정렬이 곧 최신순이다.
    live = sorted((c for c in cards if c["status"] in _LIVE_ORDER),
                  key=lambda c: (_LIVE_ORDER[c["status"]], c["id"]))
    done = sorted((c for c in cards if c["status"] in (PUBLISHED, CLOSED)),
                  key=lambda c: c["id"], reverse=True)[:SHOW_DONE]

    busy, last = "", ""
    if job:
        kind, st = job.get("kind"), job.get("status")
        if kind in _BUSY_KINDS and st in ("pending", "running"):
            busy = _BUSY_KINDS[kind]
        elif kind in _BUSY_KINDS and st == "error":
            last = "⚠️ " + (job.get("message") or "요청이 실패했어요.")
    return {
        "now": _now_line(props, live),
        "props": props[:SHOW_PROPOSALS], "props_more": props[SHOW_PROPOSALS:],
        "n_props": len(props), "live": live, "done": done,
        "busy": busy, "last": last,
    }
