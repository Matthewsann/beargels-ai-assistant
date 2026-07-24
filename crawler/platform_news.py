"""플랫폼 공지·정책·혜택 소식 수집 (쿠팡이츠 파트너스 / 배민외식업광장).

매월 1·15일 스케줄러가 이 모듈로 공지 목록을 긁어 신규만 골라 텔레그램으로
보고한다(정책 개정·이벤트·혜택 놓치지 않게). 로그인 불필요한 공개 소식 페이지.

출처:
  쿠팡: partners.coupangeats.com — 공지사항/쿠팡이츠소식/약관·정책
  배민: ceo.baemin.com/notice — 공지사항(카테고리·날짜 포함)

각 글의 URL 을 고유키로 dedup 한다(제목은 바뀔 수 있어도 URL 은 안정적).
"""

import logging
import re

from crawler.browser import BrowserSession, human_pause

logger = logging.getLogger(__name__)

COUPANG_NEWS = {
    "공지사항": "https://partners.coupangeats.com/category/news/notice/",
    "쿠팡이츠소식": "https://partners.coupangeats.com/category/news/partners/",
    "약관·정책": "https://partners.coupangeats.com/category/news/policies/",
}
BAEMIN_NOTICE = "https://ceo.baemin.com/notice?page=1"

_DATE_RE = re.compile(r"20\d{2}\.\s*\d{1,2}\.\s*\d{1,2}")


def _extract(page, js):
    try:
        return page.evaluate(js) or []
    except Exception:  # noqa: BLE001
        logger.warning("공지 추출 실패")
        return []


def fetch_coupang(page):
    """쿠팡 파트너스 공지/소식/정책 글 목록."""
    items = []
    for cat, url in COUPANG_NEWS.items():
        page.goto(url, wait_until="domcontentloaded")
        human_pause(2.0, 3.0)
        rows = _extract(page, r"""() => {
            const out=[]; const seen=new Set();
            document.querySelectorAll('a[href]').forEach(a=>{
                const t=(a.innerText||'').trim().replace(/\s+/g,' ');
                const h=a.href;
                if(t && t.length>4
                   && /partners\.coupangeats\.com\/20|\/[0-9]{4,}/.test(h)
                   && !seen.has(h)){
                    seen.add(h); out.push({title:t.slice(0,90), url:h});
                }
            });
            return out.slice(0,20);
        }""")
        for r in rows:
            items.append({"platform": "coupang", "category": cat,
                          "title": r["title"], "url": r["url"], "date": None})
    return items


def fetch_baemin(page):
    """배민외식업광장 공지 글 목록(카테고리·날짜 포함)."""
    page.goto(BAEMIN_NOTICE, wait_until="domcontentloaded")
    human_pause(2.0, 3.0)
    rows = _extract(page, r"""() => {
        const out=[]; const seen=new Set();
        document.querySelectorAll('a[href]').forEach(a=>{
            const t=(a.innerText||'').trim().replace(/\s+/g,' ');
            const h=a.href;
            if(t && t.length>6 && /notice\/[0-9]/.test(h) && !seen.has(h)){
                seen.add(h); out.push({title:t.slice(0,110), url:h});
            }
        });
        return out.slice(0,20);
    }""")
    items = []
    for r in rows:
        m = _DATE_RE.search(r["title"])
        items.append({"platform": "baemin", "category": "공지사항",
                      "title": r["title"], "url": r["url"],
                      "date": m.group(0) if m else None})
    return items


def fetch_all():
    """양 플랫폼 공지·소식을 한 세션에서 수집해 리스트로 반환한다."""
    out = []
    with BrowserSession() as s:
        page = s.page
        for fn, name in ((fetch_coupang, "쿠팡"), (fetch_baemin, "배민")):
            try:
                got = fn(page)
                out += got
                logger.info("%s 공지 %d건", name, len(got))
            except Exception:
                logger.exception("%s 공지 수집 실패", name)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    for it in fetch_all():
        d = f" ({it['date']})" if it.get("date") else ""
        print(f"[{it['platform']}·{it['category']}] {it['title']}{d}")
