"""순위 확인 일꾼 — 네이버 블로그 검색에서 우리 블로그(blog_id)의 순위를 찾는다.

로그인 불필요(공개 검색). Playwright 번들 크로미움(headless)으로 검색 결과를 훑어
blog.naver.com/<blog_id> 링크가 몇 번째로 나오는지 계산한다.

    python rank_checker.py "송도 베이글"        # 한 키워드 확인
    python rank_checker.py                       # config 의 blog_id 로, 라이브러리 키워드 전체

결과는 webapp/rank_history.json 에 날짜별로 누적된다(웹 분석 화면이 이걸 읽음).
"""
from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime
from urllib.parse import quote

import yaml
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
HISTORY = pathlib.Path(__file__).resolve().parent / "rank_history.json"
PER_PAGE = 10  # 표기용: 10위를 1페이지로 환산 (네이버 노출은 유동적)
MAX_RESULTS = 50


def get_blog_id() -> str:
    cfg_path = ROOT / "automation" / "config.yaml"
    if cfg_path.exists():
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        return (cfg.get("naver", {}) or {}).get("blog_id", "")
    return ""


def check_keyword(keyword: str, blog_id: str) -> dict:
    """키워드 검색 결과에서 blog_id 의 첫 등장 순위를 찾는다.

    먼저 **브라우저 없이** 공개 검색 페이지를 그대로 읽는다(sns_automation/
    naver_search). 집 PC 에 Playwright 크로미움이 깔려 있지 않아 순위 확인이
    통째로 실패하고 있었기 때문이다(2026-09-04: '키워드 0개 확인'). HTTP 가
    막히면 예전 경로(크로미움)로 넘어간다.
    """
    try:
        import pathlib as _pl
        import sys as _sys
        _root = str(_pl.Path(__file__).resolve().parent.parent)
        if _root not in _sys.path:
            _sys.path.insert(0, _root)
        from sns_automation import naver_search
        got = naver_search.rank_of(keyword, blog_id)
        if got.get("scanned"):
            return got
    except Exception as e:  # noqa: BLE001 — 막히면 브라우저 경로로
        print(f"  (HTTP 순위 확인 실패, 브라우저로 재시도: {str(e)[:80]})")

    url = f"https://search.naver.com/search.naver?ssc=tab.blog.all&query={quote(keyword)}"
    ordered: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2000)
            hrefs = page.eval_on_selector_all(
                "a",
                "els => els.map(e => e.href).filter(h => h && h.includes('blog.naver.com'))",
            )
            seen = set()
            for h in hrefs:
                # blog.naver.com/<id>/<글번호(숫자)> 형태의 실제 글만, 첫 등장 순서 유지
                part = h.split("blog.naver.com/")[-1].split("?")[0].strip("/")
                bits = part.split("/")
                if len(bits) < 2:
                    continue
                bid, postno = bits[0], bits[1]
                if not postno.isdigit() or "naver" in bid.lower():
                    continue
                key = f"{bid}/{postno}"  # 같은 글은 한 번만(썸네일+제목 중복 제거)
                if key in seen:
                    continue
                seen.add(key)
                ordered.append(bid)
                if len(ordered) >= MAX_RESULTS:
                    break
        finally:
            browser.close()

    rank = None
    for i, bid in enumerate(ordered, start=1):
        if bid.lower() == blog_id.lower():
            rank = i
            break
    return {
        "keyword": keyword,
        "found": rank is not None,
        "rank": rank,
        "page": ((rank - 1) // PER_PAGE + 1) if rank else None,
        "pos_in_page": ((rank - 1) % PER_PAGE + 1) if rank else None,
        "scanned": len(ordered),
    }


def load_history() -> dict:
    if HISTORY.exists():
        try:
            return json.loads(HISTORY.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_result(date_str: str, results: list[dict]) -> None:
    hist = load_history()
    hist.setdefault("checks", {})[date_str] = results
    HISTORY.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")


def run_and_save(keywords: list[str], blog_id: str) -> list[dict]:
    results = []
    for kw in keywords:
        r = check_keyword(kw, blog_id)
        results.append(r)
        if r["found"]:
            print(f"  '{kw}' → {r['page']}페이지 {r['pos_in_page']}위 (전체 {r['rank']}위)")
        else:
            print(f"  '{kw}' → 상위 {r['scanned']}개 안에 없음")
    save_result(datetime.now().strftime("%Y-%m-%d %H:%M"), results)
    return results


if __name__ == "__main__":
    bid = get_blog_id()
    if not bid:
        sys.exit("config.yaml 에서 blog_id 를 찾지 못했습니다.")
    kws = sys.argv[1:] if len(sys.argv) > 1 else ["송도 베이글", "송도 카페"]
    print(f"블로그: {bid}  |  키워드 {len(kws)}개 확인")
    run_and_save(kws, bid)
    print("→ webapp/rank_history.json 에 저장 완료")
