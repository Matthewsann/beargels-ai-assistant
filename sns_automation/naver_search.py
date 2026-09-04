"""네이버 검색 실측 — 블로그 주제를 '이길 수 있는 키워드' 위에서 고른다.

왜 필요한가(설계 검토 2026-09-04):
    송도 손님은 네이버 검색에서 온다. 그런데 블로그 글감의 검색량·경쟁도는
    지금까지 AI 추정값이었다(webapp/planner.py 의 competition 필드). 추정은
    "송도 베이글"처럼 이미 상위가 굳은 키워드를 계속 고르게 만든다.

무엇을 재는가 — 두 가지, 둘 다 로그인·API 키 없이 공개 페이지로:
    ① 수요  : 자동완성(ac.search.naver.com) — 사람들이 실제로 치는 조합.
              여기 없는 말은 아무도 검색하지 않는다.
    ② 경쟁  : 블로그탭 상위 30(search.naver.com) — 그 자리를 지금 누가
              차지하고 있는지. 제목이 키워드를 정면으로 맞춘 글이 몇이나 되는지.

    판정(green/yellow/red)은 아래 `verdict()` 규칙 함수 하나로 끝난다 — AI 비용 0.

    py -m sns_automation.naver_search              기본 씨앗 키워드로 조사
    py -m sns_automation.naver_search 송도 베이글    한 키워드만

결과는 data/naver_search.json 에 쌓이고, 기획 프롬프트에 `as_prompt_context()`
로 주입된다. 주 목적은 '어느 키워드로 써야 하는가'지만, 같은 페이지에 우리 글
순위도 들어 있어 `rank_of()` 로 **브라우저 없이** 순위 확인까지 해준다
(webapp/rank_checker.py 가 이걸 먼저 쓴다 — 그쪽 크로미움이 집 PC 에 없다).

⚠️ 예의: 호출 사이에 쉬고(PAUSE), 한 번에 조사하는 키워드 수를 제한한다.
"""

from __future__ import annotations

import html as _html
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from functools import lru_cache

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.getenv("PIPELINE_DATA_DIR") or os.path.join(_ROOT, "data")
PATH = os.path.join(DATA_DIR, "naver_search.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
PAUSE = 1.2                 # 호출 사이 쉼(초)
TOP_N = 30                  # 블로그탭에서 보는 상위 글 수
MAX_KEYWORDS = 12           # 한 번에 조사할 키워드 수 상한

@lru_cache(maxsize=1)
def our_blog_id() -> str:
    """우리 블로그 아이디 — 순위 확인(rank_checker)과 **같은 출처**를 쓴다.

    ⚠️ 손으로 적어 두면 틀린다: 실제 아이디는 `beargels_songdo`(밑줄)인데
    브랜드 문서에는 `beargelssongdo` 로 적혀 있어, 상위에 우리 글이 있어도
    영영 못 알아볼 뻔했다(2026-09-04 실측에서 발견).
    """
    env = os.getenv("NAVER_BLOG_ID", "").strip()
    if env:
        return env
    try:
        import yaml
        path = os.path.join(_ROOT, "automation", "config.yaml")
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return ((cfg.get("naver") or {}).get("blog_id") or "").strip()
    except Exception:  # noqa: BLE001 — 설정이 없으면 '우리 글 여부'만 못 본다
        return ""

#: 씨앗 키워드. 여기서 자동완성으로 가지를 뻗는다.
#: 씨앗은 '지역+카테고리'로 넓게 잡지 말고 **우리가 실제로 파는 것**으로 좁게.
#: (2026-09-04 실측: '송도 카페'·'송도 브런치' 계열은 정면 경쟁글이 9~23개로
#:  전부 red 였다. 넓은 말은 이미 굳었고, 좁은 말에 빈자리가 있다.)
SEEDS = ("송도 베이글", "송도 크림치즈", "송도 샌드위치", "송도 베이글 샌드위치",
         "인천 베이글", "송도 아침 식사")

_POST_RE = re.compile(r'href="(https://blog\.naver\.com/([A-Za-z0-9_\-]+)/(\d{6,}))"'
                      r'[^>]*>(.{0,400}?)</a>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


class SearchError(RuntimeError):
    """네이버가 응답하지 않음 — 사람이 읽을 한글 메시지."""


def _get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 — 네트워크·차단 전부
        raise SearchError(f"네이버 검색을 읽지 못했어요: {str(e)[:120]}") from e


# ── 순수 함수(테스트 대상) ────────────────────────────────────

def norm(text: str) -> str:
    """비교용 정규화 — 공백·문장부호를 지운 소문자."""
    return _SPACE_RE.sub("", re.sub(r"[^\w가-힣]+", "", (text or "").lower()))


def parse_autocomplete(body: str) -> list[str]:
    """자동완성 JSON → 제안어 목록(입력어 포함, 순서 유지)."""
    try:
        data = json.loads(body)
    except ValueError:
        return []
    out, seen = [], set()
    for group in data.get("items") or []:
        for item in group or []:
            word = (item[0] if isinstance(item, list) and item else "").strip()
            if word and word not in seen:
                seen.add(word)
                out.append(word)
    return out


def parse_blog_results(html: str, limit: int = TOP_N) -> list[dict]:
    """블로그탭 HTML → 상위 글 [{blog_id, log_no, title}] (순서 = 노출 순위).

    같은 글이 썸네일·제목으로 두 번 나오므로 첫 등장만 남긴다. 제목이 없는
    카드형 결과도 순위에는 들어가므로 title 은 빈 문자열일 수 있다.
    """
    out, seen = [], set()
    for _url, blog_id, log_no, inner in _POST_RE.findall(html):
        key = f"{blog_id}/{log_no}"
        if key in seen:
            continue
        seen.add(key)
        title = _html.unescape(_TAG_RE.sub("", inner)).strip()
        title = title.replace("새 창 열림", "").strip()
        out.append({"blog_id": blog_id, "log_no": log_no,
                    "title": _SPACE_RE.sub(" ", title)[:120]})
        if len(out) >= limit:
            break
    return out


def exact_hits(keyword: str, posts: list[dict]) -> int:
    """제목이 이 키워드를 **정면으로** 맞춘 상위 글 수 — 정면 경쟁 강도."""
    k = norm(keyword)
    return sum(1 for p in posts if k and k in norm(p.get("title", "")))


def our_rank(posts: list[dict], blog_id: str = "") -> int | None:
    """상위 목록에서 우리 블로그가 몇 번째인가. 없으면 None."""
    want = (blog_id or our_blog_id()).lower()
    if not want:
        return None
    for i, p in enumerate(posts, start=1):
        if p.get("blog_id", "").lower() == want:
            return i
    return None


#: 순위 표기 — 10위를 1페이지로 환산(rank_checker 와 같은 규칙).
PER_PAGE = 10


def rank_of(keyword: str, blog_id: str = "") -> dict:
    """키워드 하나의 우리 블로그 순위 — **브라우저 없이** 공개 검색 페이지로.

    예전 경로(webapp/rank_checker.py)는 Playwright 크로미움이 있어야 했는데
    집 PC 에 설치돼 있지 않아 순위 확인이 통째로 실패하고 있었다
    (2026-09-04 발견: '키워드 0개 확인'). 같은 데이터를 HTTP 로 읽으면 된다.

    반환 모양은 rank_checker.check_keyword 와 같다(저장·화면이 그대로 쓴다).
    """
    posts = blog_top(keyword)
    rank = our_rank(posts, blog_id)
    return {
        "keyword": keyword, "found": rank is not None, "rank": rank,
        "page": ((rank - 1) // PER_PAGE + 1) if rank else None,
        "pos_in_page": ((rank - 1) % PER_PAGE + 1) if rank else None,
        "scanned": len(posts),
    }


def verdict(row: dict) -> dict:
    """이 키워드로 써야 하는가 — 규칙 판정(AI 비용 0).

    green  : 검색되는 말인데 그 자리를 정면으로 맞춘 글이 적다 → 지금 쓰면 이긴다
    yellow : 수요는 있고 경쟁도 있다 → 각도를 좁혀서(메뉴·상황) 쓴다
    red    : 아무도 안 치거나(수요 0) 상위가 굳었다 → 이번엔 피한다
    mine   : 이미 우리 글이 상위에 있다 → 새로 쓰지 말고 그 글을 고친다
    """
    demand = bool(row.get("in_autocomplete"))
    hits = int(row.get("exact_hits") or 0)
    rank = row.get("our_rank")
    if rank and rank <= 10:
        return {"tier": "mine", "why": f"이미 우리 글이 {rank}위 — 새로 쓰지 말고 그 글을 보강"}
    if not demand:
        return {"tier": "red", "why": "자동완성에 없는 말 — 검색하는 사람이 거의 없다"}
    if hits <= 3:
        return {"tier": "green", "why": f"검색되는 말인데 정면 경쟁글 {hits}개뿐 — 지금 쓰면 이긴다"}
    if hits <= 7:
        return {"tier": "yellow", "why": f"정면 경쟁글 {hits}개 — 메뉴·상황으로 각도를 좁혀서"}
    return {"tier": "red", "why": f"정면 경쟁글 {hits}개 — 상위가 굳었다"}


def pick_winnable(rows: list[dict], limit: int = 5) -> list[dict]:
    """이길 수 있는 키워드 순으로. green → yellow, 같은 등급이면 경쟁 적은 순."""
    order = {"green": 0, "yellow": 1, "mine": 2, "red": 3}
    ranked = sorted(rows, key=lambda r: (order.get((r.get("verdict") or {}).get("tier"), 9),
                                         r.get("exact_hits", 99)))
    return [r for r in ranked if (r.get("verdict") or {}).get("tier") in ("green", "yellow")][:limit]


# ── 수집 ──────────────────────────────────────────────────────

def autocomplete(keyword: str) -> list[str]:
    """자동완성 제안어 — 사람들이 실제로 치는 조합(수요 신호)."""
    q = urllib.parse.quote(keyword)
    url = (f"https://ac.search.naver.com/nx/ac?q={q}&con=1&frm=nv&ans=2"
           f"&r_format=json&r_enc=UTF-8&r_unicode=0&t_koreng=1&run=2&rev=4"
           f"&q_enc=UTF-8&st=100")
    return parse_autocomplete(_get(url))


def blog_top(keyword: str, limit: int = TOP_N) -> list[dict]:
    """블로그탭 상위 글 목록(경쟁 신호). 로그인 불필요."""
    q = urllib.parse.quote(keyword)
    url = f"https://search.naver.com/search.naver?ssc=tab.blog.all&query={q}"
    return parse_blog_results(_get(url), limit)


def study(keyword: str, *, suggestions: list[str] | None = None) -> dict:
    """키워드 하나 실측 — 수요·경쟁·판정."""
    sug = suggestions if suggestions is not None else autocomplete(keyword)
    time.sleep(PAUSE)
    posts = blog_top(keyword)
    row = {
        "keyword": keyword,
        "in_autocomplete": any(norm(s) == norm(keyword) for s in sug),
        "suggestions": sug[:10],
        "top_count": len(posts),
        "exact_hits": exact_hits(keyword, posts),
        "our_rank": our_rank(posts),
        "top_blogs": [p["blog_id"] for p in posts[:10]],
        "top_titles": [p["title"] for p in posts[:5] if p["title"]],
    }
    row["verdict"] = verdict(row)
    return row


def research(seeds: tuple[str, ...] | list[str] = SEEDS, *,
             per_seed: int = 3, max_keywords: int = MAX_KEYWORDS) -> dict:
    """씨앗 → 자동완성으로 가지를 뻗고 → 각각 경쟁을 실측한다.

    한 씨앗당 자동완성 상위 `per_seed` 개까지만 본다(예의·시간). 실패한
    키워드는 건너뛰고 나머지는 저장한다 — 절반이라도 갱신되는 게 낫다.
    """
    rows: list[dict] = []
    tried: set[str] = set()
    for seed in seeds:
        try:
            sug = autocomplete(seed)
        except SearchError as e:
            logger.warning("자동완성 실패(%s): %s", seed, e)
            sug = []
        time.sleep(PAUSE)
        # 씨앗 자신 + 자동완성 가지(우리 지역과 무관한 것은 뺀다)
        branch = [seed] + [s for s in sug
                           if norm(s) != norm(seed) and is_useful(s)][:per_seed]
        for kw in branch:
            if norm(kw) in tried or len(rows) >= max_keywords:
                continue
            tried.add(norm(kw))
            try:
                rows.append(study(kw, suggestions=sug if kw == seed else None))
            except SearchError as e:
                logger.warning("경쟁 조사 실패(%s): %s", kw, e)
            time.sleep(PAUSE)
    data = {
        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rows": rows,
        "winnable": [r["keyword"] for r in pick_winnable(rows)],
    }
    if rows:
        save(data)
    return data


#: '송도'라는 이름의 다른 동네 — 부산 송도해수욕장·포항 송도가 자동완성에 섞인다
#: (2026-09-04 실측에서 '포항 송도 카페'가 후보로 올라왔다).
_OTHER_PLACE = ("부산", "울산", "대구", "속초", "제주", "여수", "포항", "창원")

#: 경쟁 가게 상호 — **이길 수 있어도 쓰지 않는다.**
#: 그 말을 검색하는 사람은 그 가게를 찾는 손님이라, 우리 글이 1위여도 방문으로
#: 이어지지 않는다(2026-09-04 실측에서 '송도 베이글리스트'가 ✅ 로 추천됐다).
#: 새 경쟁 상호가 보이면 여기 추가하거나 .env NAVER_EXCLUDE 에 쉼표로 적는다.
_RIVALS = ("베이글리스트", "베이글로그", "로로베이글", "라크루뚜", "라크루뜨",
           "브런치빈", "빵집투어")


def _rivals() -> tuple[str, ...]:
    extra = tuple(x.strip() for x in os.getenv("NAVER_EXCLUDE", "").split(",") if x.strip())
    return _RIVALS + extra


def is_useful(word: str) -> bool:
    """이 키워드를 조사할 가치가 있나 — 우리 동네이고, 남의 가게 이름이 아니다."""
    w = norm(word)
    if any(norm(x) in w for x in _OTHER_PLACE):
        return False
    if any(norm(x) in w for x in _rivals()):
        return False
    return any(norm(x) in w for x in ("송도", "연수구", "인천"))


#: 예전 이름 — 다른 모듈이 부를 수 있어 남겨 둔다.
_is_local = is_useful


# ── 저장·주입 ─────────────────────────────────────────────────

def save(data: dict, path: str = PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load(path: str = PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def as_prompt_context(data: dict | None = None) -> str:
    """기획 프롬프트에 넣을 글. 데이터가 없으면 빈 문자열."""
    data = data or load()
    rows = data.get("rows") or []
    if not rows:
        return ""
    lines = ["[네이버 검색 실측 — 블로그 주제는 이 위에서 고른다]"]
    for r in rows:
        v = r.get("verdict") or {}
        mark = {"green": "✅", "yellow": "△", "mine": "🅾", "red": "✕"}.get(v.get("tier"), "·")
        lines.append(f"{mark} 「{r['keyword']}」 — {v.get('why', '')}")
    win = data.get("winnable") or []
    if win:
        lines.append("→ 이번에 쓸 만한 키워드: " + ", ".join(win))
    lines.append("(✅ 지금 쓰면 이긴다 / △ 각도를 좁혀서 / 🅾 우리 글이 이미 상위 "
                 "— 새로 쓰지 말고 보강 / ✕ 피한다)")
    return "\n".join(lines)


def main() -> int:
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = [a for a in sys.argv[1:] if a.strip()]
    if args:
        row = study(" ".join(args))
        print(json.dumps(row, ensure_ascii=False, indent=2))
        return 0
    data = research()
    print(as_prompt_context(data))
    print(f"\n→ {PATH} 에 {len(data['rows'])}개 저장")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
