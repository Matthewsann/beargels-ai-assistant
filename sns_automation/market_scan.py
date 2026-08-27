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
    hooks = [h for s in data["hashtags"].values() for h in s.get("hooks", [])][:max_hooks]
    if hooks:
        lines.append("[실제 훅 문장들]")
        lines += [f"    · {h}" for h in hooks]
    lines.append("위는 참고용 사실이다. 베어글스 톤(친근한 해요체, 과장·금지표현 없음)으로 재해석할 것.")
    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    tags = sys.argv[1:] or None
    try:
        data = scan(tags)
    except MetaGraphError as e:
        print(f"\n[X] {e}\n")
        raise SystemExit(1)
    path = save(data)
    ok = sum(1 for s in data["hashtags"].values() if s.get("count"))
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
