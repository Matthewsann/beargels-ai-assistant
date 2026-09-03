"""해시태그 시장 스캔 — 잘 되는 게시물이 뭘 어떻게 쓰는지 모아 기획 재료로 만든다.

`meta_graph` 로 해시태그 인기 게시물을 받아, 기획 에이전트가 바로 쓸 수 있게
정리한다: 훅(캡션 첫 줄) 모음, 캡션 길이 분포, 해시태그 개수, 상위 게시물 링크.

    python -m sns_automation.market_scan            # 기본 해시태그로 스캔
    python -m sns_automation.market_scan 송도카페 과일산도

결과는 data/market_scan.json 에 저장되고 planner 프롬프트에 주입된다.

⚠️ 해시태그 검색은 **7일에 고유 30개** 한도. 기본 목록을 작게 유지하고
주 1회만 돌리는 것을 전제로 한다.
"""

from __future__ import annotations

import json
import logging
import os
import re
import statistics
import sys
from datetime import datetime, timezone

from .meta_graph import MetaGraph, MetaGraphError, _rate_pause, from_env

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
OUT_PATH = os.path.join(_DATA_DIR, "market_scan.json")

#: 베어글스가 실제로 경쟁하는 자리. 한도(7일 30개)가 있어 좁게 유지한다.
DEFAULT_HASHTAGS = ["송도카페", "과일산도", "송도베이글", "인천카페", "베이글맛집"]

_HASHTAG_RE = re.compile(r"#[\w가-힣]+")


def _first_line(caption: str) -> str:
    """캡션 첫 줄 = 사실상의 훅. 해시태그만 있는 줄은 건너뛴다."""
    for raw in (caption or "").splitlines():
        line = _HASHTAG_RE.sub("", raw).strip(" ·-—|")
        if len(line) >= 4:
            return line[:80]
    return ""


def summarize(posts: list[dict]) -> dict:
    """게시물 목록 → 기획에 쓸 통계."""
    if not posts:
        return {"count": 0}
    caps = [p.get("caption") or "" for p in posts]
    lens = [len(c) for c in caps if c]
    tags = [len(_HASHTAG_RE.findall(c)) for c in caps if c]
    engaged = sorted(
        posts,
        key=lambda p: (p.get("like_count") or 0) + (p.get("comments_count") or 0) * 3,
        reverse=True,
    )
    return {
        "count": len(posts),
        "reels_ratio": round(
            sum(1 for p in posts if p.get("media_type") in ("VIDEO", "REELS")) / len(posts), 2),
        "caption_length_median": int(statistics.median(lens)) if lens else 0,
        "hashtag_count_median": int(statistics.median(tags)) if tags else 0,
        "hooks": [h for h in (_first_line(c) for c in caps) if h][:20],
        "top_posts": [
            {
                "hook": _first_line(p.get("caption") or ""),
                "likes": p.get("like_count"),
                "comments": p.get("comments_count"),
                "type": p.get("media_type"),
                "permalink": p.get("permalink"),
            }
            for p in engaged[:8]
        ],
    }


def scan(hashtags: list[str] | None = None, *, client: MetaGraph | None = None,
         limit: int = 25) -> dict:
    """해시태그별 인기 게시물을 모아 요약본을 만든다."""
    tags = hashtags or DEFAULT_HASHTAGS
    api = client or from_env()
    result: dict = {
        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hashtags": {},
        "errors": {},
    }
    for i, tag in enumerate(tags):
        _rate_pause(i)
        try:
            posts = api.hashtag_media(tag, top=True, limit=limit)
            result["hashtags"][tag] = summarize(posts)
            logger.info("#%s: 게시물 %d개", tag, len(posts))
        except MetaGraphError as e:
            result["errors"][tag] = str(e)
            logger.warning("#%s 스캔 실패: %s", tag, e)
    return result


def scan_web(page, hashtags: list[str] | None = None, *, limit: int = 24) -> dict:
    """해시태그 인기글을 **인스타 웹**에서 긁는다 — 앱 심사 없이 되는 길.

    공식 해시태그 API 는 앱 심사가 필요해 막혀 있다(2026-08). 대신 전용
    크롬(리뷰 수집과 같은 debug Chrome)으로 태그 페이지를 열어 격자의
    img alt(캡션 전문이 들어 있음)를 모은다. 좋아요 수는 격자에선 안 보여
    훅 문장·릴스 비중 위주의 요약이 된다.

    ⚠️ 그 크롬에 인스타 로그인이 되어 있어야 한다 — 로그인 벽을 만나면
    LoginRequired 예외를 던져 호출부가 사장님에게 알리게 한다.
    """
    tags = hashtags or DEFAULT_HASHTAGS
    result: dict = {
        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "web",
        "hashtags": {},
        "errors": {},
    }
    for tag in tags:
        try:
            page.goto(f"https://www.instagram.com/explore/tags/{tag}/",
                      wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(4000)
            if "/accounts/login" in page.url:
                raise LoginRequired("인스타 로그인이 필요합니다")
            rows = page.evaluate(
                """() => Array.from(
                     document.querySelectorAll('main a[href*="/p/"], main a[href*="/reel/"]')
                   ).slice(0, %d).map(a => ({
                     href: a.getAttribute('href') || '',
                     alt: (a.querySelector('img') || {}).alt || ''
                   }))""" % limit)
            posts = [{
                "caption": r["alt"],
                "media_type": "REELS" if "/reel/" in r["href"] else "IMAGE",
                "like_count": None, "comments_count": None,
                "permalink": "https://www.instagram.com" + r["href"],
            } for r in rows if r.get("alt")]
            if not posts:
                result["errors"][tag] = "게시물을 못 읽음(화면 구조 변경?)"
                continue
            result["hashtags"][tag] = summarize(posts)
            logger.info("#%s(웹): 게시물 %d개", tag, len(posts))
        except LoginRequired:
            raise
        except Exception as e:  # noqa: BLE001 — 태그 하나 실패로 전체를 죽이지 않는다
            result["errors"][tag] = str(e)
            logger.warning("#%s 웹 스캔 실패: %s", tag, e)
    return result


class LoginRequired(RuntimeError):
    """전용 크롬에 인스타 로그인이 안 되어 있음 — 사장님 조치 필요."""


def run_weekly() -> list[str]:
    """주간 시장조사 한 바퀴: 내 계정(API) + 해시태그(웹). 결과 요약 문장 반환.

    실패는 문장으로 남기고 계속 간다 — 절반이라도 갱신되는 게 낫다.
    전부 실패한 항목은 기존 파일을 보존한다(덮어쓰면 옛 데이터도 잃는다).
    """
    notes: list[str] = []
    try:
        own = scan_own()
        save(own, OWN_PATH)
        notes.append(f"내 계정 {own['count']}개(평균 ♥{own['avg_likes']})")
    except Exception as e:  # noqa: BLE001
        notes.append(f"내 계정 실패: {e}")

    try:
        import sys as _sys
        _sys.path.insert(0, os.path.dirname(_DATA_DIR))
        from crawler.browser import BrowserSession
        with BrowserSession() as sess:
            data = scan_web(sess.page)
        ok = sum(1 for s in data["hashtags"].values() if s.get("count"))
        if ok:
            save(data)
            notes.append(f"해시태그 {ok}개 갱신")
        else:
            notes.append("해시태그 0개 — 기존 데이터 보존")
    except LoginRequired:
        notes.append("⚠️ 전용 크롬에 인스타 로그인 필요(해시태그 조사 건너뜀)")
    except Exception as e:  # noqa: BLE001
        notes.append(f"해시태그 실패: {str(e)[:80]}")
    return notes


OWN_PATH = os.path.join(_DATA_DIR, "own_media.json")


def scan_own(*, client: MetaGraph | None = None, limit: int = 50) -> dict:
    """**내 계정**의 지난 게시물과 실제 반응을 모은다 — 셀프 피드백의 재료.

    남의 인기글보다 이게 낫다: 같은 가게·같은 팔로워에게 실제로 통한 문장이라,
    "무엇을 더 하고 무엇을 그만둘지"를 바로 말해준다.

    ⚠️ 도달·저장은 `instagram_manage_insights` 권한이 있어야 나온다. 없으면
    좋아요·댓글로만 판단한다(그것만으로도 순위는 매겨진다).
    """
    api = client or from_env()
    posts = api.my_media(limit=limit)
    reels = [p for p in posts if p.get("media_type") in ("VIDEO", "REELS")]

    def score(p):
        return (p.get("like_count") or 0) + (p.get("comments_count") or 0) * 3

    ranked = sorted(posts, key=score, reverse=True)
    likes = [p.get("like_count") or 0 for p in posts]
    avg = round(sum(likes) / len(likes), 1) if likes else 0

    def row(p):
        return {
            "hook": _first_line(p.get("caption") or ""),
            "likes": p.get("like_count"),
            "comments": p.get("comments_count"),
            "type": p.get("media_type"),
            "date": (p.get("timestamp") or "")[:10],
            "permalink": p.get("permalink"),
        }

    return {
        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(posts),
        "reels_ratio": round(len(reels) / len(posts), 2) if posts else 0,
        "avg_likes": avg,
        "best": [row(p) for p in ranked[:8]],
        "worst": [row(p) for p in ranked[-5:]] if len(ranked) > 8 else [],
    }


def own_as_prompt_context(data: dict | None = None) -> str:
    """내 계정 성과를 기획 프롬프트에 넣을 텍스트."""
    if data is None:
        data = load(OWN_PATH)
    if not data or not data.get("count"):
        return ""
    lines = [
        "[내 계정(@beargels_songdo)에서 실제로 통한 글 — 셀프 피드백]",
        f"· 최근 {data['count']}개, 평균 좋아요 {data['avg_likes']}개, "
        f"영상 비중 {int(data['reels_ratio'] * 100)}%",
        "· 반응이 좋았던 순:",
    ]
    for p in data.get("best", [])[:6]:
        if p.get("hook"):
            lines.append(f"    ♥{p.get('likes') or 0:>3} {p.get('type', '')[:5]:5s} \"{p['hook']}\"")
    if data.get("worst"):
        lines.append("· 반응이 약했던 글(같은 패턴을 반복하지 말 것):")
        for p in data["worst"][:3]:
            if p.get("hook"):
                lines.append(f"    ♥{p.get('likes') or 0:>3} \"{p['hook']}\"")
    lines.append("평균보다 잘 된 글의 각도를 변형해 재활용하고, 약했던 패턴은 피할 것.")
    return "\n".join(lines)


def save(data: dict, path: str = OUT_PATH) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def load(path: str = OUT_PATH) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def as_prompt_context(data: dict | None = None, *, max_hooks: int = 12) -> str:
    """기획 프롬프트에 넣을 텍스트. 데이터가 없으면 빈 문자열."""
    data = data or load()
    if not data or not data.get("hashtags"):
        return ""
    lines = ["[지금 이 시장에서 잘 되는 게시물 — 실제 수집 데이터]"]
    for tag, s in data["hashtags"].items():
        if not s.get("count"):
            continue
        lines.append(
            f"· #{tag}: 인기글 {s['count']}개, 릴스 비중 {int(s['reels_ratio']*100)}%, "
            f"캡션 중앙값 {s['caption_length_median']}자, 해시태그 중앙값 {s['hashtag_count_median']}개")
        for p in s.get("top_posts", [])[:3]:
            if p.get("hook"):
                lines.append(f"    - \"{p['hook']}\" (♥{p.get('likes') or 0})")
    # 태그별로 번갈아 뽑는다 — 첫 태그가 자리를 독식하지 않게
    pools = [list(s.get("hooks", [])) for s in data["hashtags"].values()]
    hooks: list[str] = []
    while len(hooks) < max_hooks and any(pools):
        for p in pools:
            if p and len(hooks) < max_hooks:
                hooks.append(p.pop(0))
    if hooks:
        lines.append("[실제 훅 문장들]")
        lines += [f"    · {h}" for h in hooks]
    lines.append("위는 참고용 사실이다. 베어글스 톤(친근한 해요체, 과장·금지표현 없음)으로 재해석할 것.")
    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    # `python -m sns_automation.market_scan` 로 직접 돌릴 때도 .env 를 읽는다
    # (웹앱 경로는 run_web.py 가 이미 로드한다)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    # ① 내 계정 성과 — 권한만 있으면 항상 되고, 셀프 피드백에 제일 중요하다
    try:
        own = scan_own()
        save(own, OWN_PATH)
        print(f"\n내 계정: 게시물 {own['count']}개 · 평균 좋아요 {own['avg_likes']}")
        for p in own["best"][:5]:
            print(f"   ♥{p['likes']:>3} {p['date']} {p['hook'][:44]}")
    except MetaGraphError as e:
        print(f"\n[!] 내 계정 조회 실패: {e}")

    # ② 해시태그 시장 스캔 — 앱 심사(Instagram Public Content Access)가 필요해
    #    승인 전에는 실패한다. 실패해도 ①은 이미 저장됐다.
    tags = sys.argv[1:] or None
    try:
        data = scan(tags)
    except MetaGraphError as e:
        print(f"\n[!] 해시태그 스캔 실패(앱 심사 필요할 수 있음): {e}")
        return
    ok = sum(1 for s in data["hashtags"].values() if s.get("count"))
    if not ok:
        # 전부 실패했는데 저장하면 **기존에 모아둔 데이터를 지운다**.
        # (앱 심사 전에는 해시태그가 늘 실패하므로 실제로 한 번 날렸다.)
        print("\n[!] 수집된 해시태그가 없어 기존 파일을 보존합니다.")
        return
    path = save(data)
    print(f"\n스캔 완료: 해시태그 {ok}개 → {path}")
    for tag, s in data["hashtags"].items():
        if s.get("count"):
            print(f"\n#{tag}  인기글 {s['count']}개 · 릴스 {int(s['reels_ratio']*100)}%")
            for p in s.get("top_posts", [])[:3]:
                print(f"   ♥{p.get('likes') or 0:>6}  {p.get('hook','')[:50]}")
    for tag, err in data.get("errors", {}).items():
        print(f"\n[!] #{tag}: {err}")


if __name__ == "__main__":
    main()
