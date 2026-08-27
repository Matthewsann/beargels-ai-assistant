"""발행 후 반응 수집 + 성과 요약 — 반응이 다음 글의 기획을 끌고 간다.

수집 경로(전부 로그인 불필요, 브라우저 불필요 — 2026-08-28 실측):
    · RSS(rss.blog.naver.com/{id}.xml)  → 발행된 글 목록·URL·발행일
    · 공감 수: blog.like.naver.com 공개 API
    · 댓글 수: m.blog.naver.com 모바일 페이지의 commentCount
    · 키워드 순위: 기존 blog_ranks 테이블(rank_checker) 재사용

하는 일:
    sync_published()  RSS 제목을 창고(blog_posts)와 맞춰 naver_url 자동 연결
                      + '발행 완료' 표시 (사장님이 버튼 안 눌러도 됨)
    collect()         발행 글마다 공감·댓글 스냅샷을 data/blog_reactions.json 에 누적
    perf_context()    품질 점수·반응·순위를 묶은 요약 텍스트
                      → planner 가 글감 추천·초안 프롬프트에 주입(성과 피드백 루프)
"""
from __future__ import annotations

import json
import logging
import pathlib
import re
import sys
from datetime import datetime, timezone

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "worker"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

logger = logging.getLogger(__name__)

BLOG_ID = "beargels_songdo"
STORE = ROOT / "data" / "blog_reactions.json"
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile Safari/604.1")


def _load() -> dict:
    if not STORE.exists():
        return {}
    try:
        return json.loads(STORE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save(d: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def _norm(t: str) -> str:
    """제목 매칭용 정규화 — 공백·문장부호 차이로 못 알아보는 일이 없게."""
    return re.sub(r"[^\w가-힣]", "", t or "").lower()


# ---------------------------------------------------------------------------
# 발행 감지 (RSS)
# ---------------------------------------------------------------------------

def rss_posts() -> list[dict]:
    """블로그 RSS 의 글 목록. [{title, url, log_no, pub}]"""
    r = requests.get(f"https://rss.blog.naver.com/{BLOG_ID}.xml", timeout=15)
    r.raise_for_status()
    out = []
    for item in re.findall(r"<item>(.*?)</item>", r.text, re.DOTALL):
        t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item)
        l = re.search(r"<link>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>", item)
        d = re.search(r"<pubDate>(.*?)</pubDate>", item)
        if not (t and l):
            continue
        url = l.group(1).split("?")[0]
        m = re.search(r"/(\d+)$", url)
        out.append({"title": t.group(1).strip(), "url": url,
                    "log_no": m.group(1) if m else None,
                    "pub": d.group(1) if d else ""})
    return out


def sync_published() -> int:
    """RSS 에 뜬 글을 창고와 제목으로 맞춰 naver_url·발행 상태를 채운다."""
    from database import blog_store as store
    feed = rss_posts()
    by_title = {_norm(f["title"]): f for f in feed}
    linked = 0
    for p in store.list_posts(limit=100):
        if p.get("naver_url"):
            continue
        f = by_title.get(_norm(p.get("title")))
        if not f:
            continue
        store.update_post(p["id"], naver_url=f["url"])
        if p.get("status") != "published":
            store.set_status(p["id"], "published")
        linked += 1
        logger.info("발행 감지: #%s ← %s", p["id"], f["url"])
    return linked


# ---------------------------------------------------------------------------
# 반응 수집 (공감·댓글)
# ---------------------------------------------------------------------------

def fetch_reaction(log_no: str) -> dict:
    """글 하나의 공감·댓글 수. 실패한 값은 None."""
    likes = comments = None
    try:
        r = requests.get(
            "https://blog.like.naver.com/v1/search/contents",
            params={"suppressResponse": "true", "q": f"BLOG[{BLOG_ID}_{log_no}]"},
            headers={"Referer": "https://blog.naver.com"}, timeout=10)
        c = (r.json().get("contents") or [{}])[0]
        likes = sum(int(x.get("count") or 0) for x in (c.get("reactions") or []))
    except Exception as e:  # noqa: BLE001
        logger.debug("공감 수 실패(%s): %s", log_no, str(e)[:80])
    try:
        r = requests.get(f"https://m.blog.naver.com/{BLOG_ID}/{log_no}",
                         headers={"User-Agent": UA}, timeout=10)
        m = re.search(r"commentCount[\"\']?\s*[:=]\s*[\"\']?(\d+)", r.text)
        if m:
            comments = int(m.group(1))
    except Exception as e:  # noqa: BLE001
        logger.debug("댓글 수 실패(%s): %s", log_no, str(e)[:80])
    return {"likes": likes, "comments": comments}


def collect() -> tuple[int, int, int]:
    """RSS 의 모든 글에 대해 반응 스냅샷을 쌓는다. (글수, 공감합, 댓글합)"""
    data = _load()
    today = datetime.now(timezone.utc).date().isoformat()
    n = tl = tc = 0
    for f in rss_posts():
        if not f["log_no"]:
            continue
        rec = data.setdefault(f["log_no"], {"title": f["title"], "url": f["url"],
                                            "pub": f["pub"], "history": []})
        rec["title"] = f["title"]
        rx = fetch_reaction(f["log_no"])
        hist = rec["history"]
        # 하루 한 번만 남긴다(여러 번 눌러도 기록이 불지 않게)
        if hist and hist[-1].get("date") == today:
            hist[-1].update({"likes": rx["likes"], "comments": rx["comments"]})
        else:
            hist.append({"date": today, **rx})
        n += 1
        tl += rx["likes"] or 0
        tc += rx["comments"] or 0
    _save(data)
    return n, tl, tc


# ---------------------------------------------------------------------------
# 성과 요약 → 다음 글 기획에 주입
# ---------------------------------------------------------------------------

def _latest_ranks() -> dict:
    """키워드별 최신 순위 {keyword: '1페이지 3위' | '30위 밖'}."""
    try:
        from database import blog_store as store
        out = {}
        for r in store.latest_ranks():
            k = r.get("keyword")
            if not k:
                continue
            if r.get("found"):
                out[k] = f"{r.get('page')}페이지 {r.get('pos_in_page')}위"
            else:
                out[k] = f"{r.get('scanned') or 30}위 밖"
        return out
    except Exception:  # noqa: BLE001
        return {}


def perf_context(max_posts: int = 10) -> str:
    """발행 글들의 품질·반응·순위를 묶은 요약. 프롬프트에 그대로 들어간다.

    데이터가 없으면 빈 문자열 — 부르는 쪽(planner)이 알아서 생략한다.
    """
    import blog_quality
    reactions = _load()
    if not reactions:
        return ""
    ranks = _latest_ranks()

    # 창고와 연결된 글이면 품질 점수·키워드를 붙인다
    meta = {}
    try:
        from database import blog_store as store
        for p in store.list_posts(limit=100):
            url = p.get("naver_url") or ""
            m = re.search(r"/(\d+)$", url)
            if m:
                q = blog_quality.get(p["id"]) or {}
                meta[m.group(1)] = {"keyword": p.get("main_keyword"),
                                    "quality": q.get("score")}
    except Exception:  # noqa: BLE001
        pass

    lines = []
    items = sorted(reactions.items(),
                   key=lambda kv: (kv[1].get("history") or [{}])[-1].get("likes") or 0,
                   reverse=True)[:max_posts]
    for log_no, rec in items:
        h = (rec.get("history") or [{}])[-1]
        mt = meta.get(log_no, {})
        bits = [f"공감 {h.get('likes', '?')}", f"댓글 {h.get('comments', '?')}"]
        if mt.get("quality") is not None:
            bits.append(f"품질 {mt['quality']}점")
        kw = mt.get("keyword")
        if kw and kw in ranks:
            bits.append(f"'{kw}' {ranks[kw]}")
        lines.append(f"- 「{rec.get('title', '')[:30]}」 {' · '.join(bits)}")
    if not lines:
        return ""
    return ("지금까지 발행한 글들의 실제 반응이다(공감 많은 순). 반응이 좋은 글의 "
            "주제·구성·제목 패턴은 따라가고, 반응 없는 패턴은 피하라:\n"
            + "\n".join(lines))
