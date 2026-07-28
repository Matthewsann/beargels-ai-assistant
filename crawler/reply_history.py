"""리뷰·답글 내역 수집 — 답글 생성용 백데이터(사실/맥락) 축적.

목적: 지금까지의 (리뷰 → 사장님이 실제로 단 답글) 쌍을 모아 문서/데이터로
남긴다. 답글 생성 시 참고해 ① 실제 메뉴명·서비스(러스크 등)·상황을 알고
② 없는 사실을 지어내지 않게 한다.

⚠️ 말투 주의: 과거 답글 상당수는 이전(AI/템플릿) 방식이라 '~바라요/정성껏
   준비' 같은 딱딱한 말투가 섞여 있다. 이 내역은 '사실·맥락' 참고용이지
   '말투' 학습용이 아니다. 말투는 REPLY_PERSONA(자연스러운 해요체)를 따른다.

수집:
  쿠팡: reviews/search 의 replies[].content (사장님 답글).
  배민: 리뷰 카드의 CeoCMSContentViewer(답변완료 사장님 댓글) 텍스트.

산출물(reference/):
  reply_examples.json      — 구조화된 (리뷰, 답글) 쌍
  review_reply_history.md  — 사람이 읽는 내역 문서
"""

import json
import logging
import re
from datetime import date
from pathlib import Path

from crawler.baemin import BaeminCrawler
from crawler.browser import human_pause, is_session_expired
from crawler.coupang import CoupangCrawler

logger = logging.getLogger(__name__)

REF_DIR = Path(__file__).resolve().parent.parent / "reference"
BAEMIN_REVIEWS_URL = "https://self.baemin.com/shops/reviews"


def collect_coupang(days=90, max_pages=8):
    """쿠팡 (리뷰, 사장님 답글) 쌍을 수집한다."""
    out = []
    with CoupangCrawler() as c:
        revs = c.fetch_reviews(days=days, max_pages=max_pages)
    for r in revs:
        raw = json.loads(r["raw"])
        replies = raw.get("replies") or []
        owner = replies[0].get("content") if replies else None
        out.append({
            "platform": "coupang",
            "review_no": r.get("review_no"),
            "author": r.get("author"),
            "rating": r.get("rating"),
            "content": r.get("content"),
            "menus": r.get("menus"),
            "order_count": r.get("order_count"),
            "written_date": r.get("written_date"),
            "owner_reply": (owner or "").strip() or None,
        })
    logger.info("쿠팡 수집 %d건(답변 %d)", len(out),
                sum(1 for x in out if x["owner_reply"]))
    return out


def _clean_baemin_reply(text):
    """배민 답글박스 innerText 에서 헤더('사장님'/날짜)와 버튼('삭제'/'수정'/
    '신고')을 제거해 실제 답글 본문만 남긴다. 본문 없으면 None.

    예: '사장님\\n2026년 7월 10일\\n폼키님,\\n...감사합니다!\\n삭제'
        → '폼키님,\\n...감사합니다!'
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    drop = {"사장님", "삭제", "수정", "신고", "답글",
            "사장님 댓글 등록하기", "사장님 댓글 추가하기"}
    body = [ln for ln in lines
            if ln not in drop
            and not re.fullmatch(r"\d{4}년\s*\d{1,2}월\s*\d{1,2}일", ln)]
    return "\n".join(body).strip() or None


def collect_baemin(max_scroll=8):
    """배민 (리뷰, 사장님 답글) 쌍을 수집한다.

    리뷰 본문(ReviewContent)과 사장님 댓글(ReviewCommentBox)이 형제 컨테이너라
    문서 순서로 페어링한다. 리뷰는 baemin.py 파서로 정확히 파싱하고, 답글은
    박스 innerText 에서 헤더/버튼을 벗겨 본문만 남긴다.
    """
    out = []
    with BaeminCrawler() as c:
        page = c.page
        page.goto(BAEMIN_REVIEWS_URL, wait_until="domcontentloaded")
        human_pause(2, 3)
        if is_session_expired(page):
            logger.warning("배민 세션 만료 — 수집 건너뜀")
            return out
        prev = 0
        for _ in range(max_scroll):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            human_pause(1.5, 2.5)
            cur = len(page.query_selector_all(
                '[class*="ReviewContent-module__"]'))
            if cur == prev:
                break
            prev = cur
        # 문서 순서로 리뷰(HTML) ↔ 답글(innerText) 페어링.
        pairs = page.evaluate(r"""() => {
            const nodes=document.querySelectorAll(
              '[class*="ReviewContent-module__"], [class*="ReviewCommentBox-module__"]');
            const out=[]; let cur=null;
            nodes.forEach(n=>{
                const cls=n.className.toString();
                if(/ReviewContent-module__/.test(cls)){
                    cur={html:n.outerHTML, reply:null}; out.push(cur);
                } else if(/ReviewCommentBox-module__/.test(cls) && cur
                          && cur.reply===null){
                    cur.reply=(n.innerText||'');
                }
            });
            return out;
        }""")

    from bs4 import BeautifulSoup
    for pr in pairs:
        soup = BeautifulSoup(pr["html"], "html.parser")
        item = soup.select_one('[class*="ReviewContent-module__"]') or soup
        rev = BaeminCrawler._parse_review_item(item) or {}
        out.append({
            "platform": "baemin",
            "review_no": rev.get("review_no"),
            "author": rev.get("author"),
            "rating": rev.get("rating"),
            "content": rev.get("content"),
            "menus": rev.get("menus"),
            "order_count": rev.get("order_count"),
            "written_date": rev.get("written_at"),
            "owner_reply": _clean_baemin_reply(pr["reply"]),
        })
    logger.info("배민 수집 %d건(답변 %d)", len(out),
                sum(1 for x in out if x["owner_reply"]))
    return out


def build(days=90):
    """양 플랫폼 내역을 수집해 reference/ 에 저장한다."""
    REF_DIR.mkdir(exist_ok=True)
    data = []
    try:
        data += collect_coupang(days=days)
    except Exception:
        logger.exception("쿠팡 내역 수집 실패")
    try:
        data += collect_baemin()
    except Exception:
        logger.exception("배민 내역 수집 실패")

    (REF_DIR / "reply_examples.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown(data)
    _write_context(data)
    logger.info("내역 저장: %s (총 %d건)", REF_DIR, len(data))
    return data


# 답글 생성 시 프롬프트에 넣는 '사실/맥락' 백데이터(말투 아님).
_CONTEXT_HEADER = """# 베어글스 답글 백데이터 (사실·맥락)

답글 생성 시 이 사실들을 참고한다. **없는 내용은 지어내지 않는다.**

## 매장 기본
- 인천 송도 베이글카페 '베어글스'. 채널: 배달의민족·쿠팡이츠(둘 다 **배달**),
  매장. 배달 리뷰엔 '방문/오세요'가 아니라 '주문 주세요/시켜주세요'로.
- 취급: 베이글, 베이글 샌드위치, 치아바타 샌드위치, 샐러드, 디저트(테디 케이크
  등), 음료. 세트 구성 다양.

## 실제 메뉴명 (리뷰/주문에서 확인된 것 — 답글에 언급 시 이 표기 사용)
{menus}

## 알아둘 것
- 러스크를 서비스(무료)로 챙겨드리는 경우가 있음(리뷰에 '서비스 러스크' 언급).
- 단골(재주문) 고객이 많음 — order_count 로 몇 번째인지 알 수 있으면 짚어준다.

## ⚠️ 말투는 여기서 배우지 않는다
과거 답글엔 '~바라요/정성껏 준비하겠습니다' 같은 딱딱한 말투가 섞여 있다.
말투는 REPLY_PERSONA(자연스러운 해요체)를 따르고, 여기선 **사실만** 가져온다.
"""


def _write_context(data):
    """생성기가 참고할 사실/맥락(reply_context.md)을 코퍼스에서 뽑아 저장."""
    from collections import Counter
    mc = Counter()
    for d in data:
        for m in (d.get("menus") or []):
            mc[m] += 1
    menu_lines = "\n".join(f"- {m}" for m, _ in mc.most_common(40)) or "- (수집된 메뉴 없음)"
    (REF_DIR / "reply_context.md").write_text(
        _CONTEXT_HEADER.format(menus=menu_lines), encoding="utf-8")


def _write_markdown(data):
    lines = ["# 베어글스 리뷰·답글 내역 (자동 생성)", "",
             f"수집일: {date.today().isoformat()} · 총 {len(data)}건", "",
             "> ⚠️ 이 내역은 **사실·맥락 참고용**입니다. 과거 답글엔 딱딱한 말투가",
             "> 섞여 있으니 **말투는 따라 하지 말고** 자연스러운 해요체로 씁니다.", ""]
    for plat in ("coupang", "baemin"):
        rows = [d for d in data if d["platform"] == plat]
        ko = {"coupang": "쿠팡이츠", "baemin": "배민"}[plat]
        lines.append(f"## {ko} ({len(rows)}건)\n")
        for d in rows:
            head = (f"- ★{d.get('rating') or '?'} "
                    f"{d.get('author') or ''} "
                    f"({d.get('written_date') or ''})").strip()
            lines.append(head)
            if d.get("content"):
                lines.append(f"  - 리뷰: {d['content']}")
            if d.get("menus"):
                lines.append(f"  - 메뉴: {', '.join(d['menus'])}")
            if d.get("owner_reply"):
                lines.append(f"  - 사장님 답글: {d['owner_reply']}")
            lines.append("")
    (REF_DIR / "review_reply_history.md").write_text(
        "\n".join(lines), encoding="utf-8")


def _parse_date(s):
    """'2026-07-24' / '2026년 7월 17일' 등 혼재 포맷을 date로(실패 시 None)."""
    s = (s or "").strip()
    m = re.search(r"(\d{4})[-.년]\s*(\d{1,2})[-.월]\s*(\d{1,2})", s)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


PROGRAM_LAUNCH_DATE = "2026-07-20"  # 답글 프로그램 도입일(이후 답글만 학습)


def import_recent_approved(since=PROGRAM_LAUNCH_DATE):
    """도입일 이후 실제 게시된 답글을 '승인 예시'로 편입한다(#3 재크롤링 학습).

    reference/reply_examples.json(build() 산출물)에서 owner_reply가 있고
    리뷰 날짜가 since 이후인 것만 골라 학습 예시로 저장한다.

    ⚠️ since 이전 답글은 이전(템플릿/딱딱한 톤) 방식이라 말투 학습에 넣지
       않는다(모듈 상단 주의 참고). 이미 게시/수정으로 저장된 예시(초안 diff
       포함)는 덮어쓰지 않는다.
    """
    from assistant.reply_learning import _load_examples, record_example

    since_d = _parse_date(since)
    try:
        data = json.loads(
            (REF_DIR / "reply_examples.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logger.warning("reply_examples.json 없음 — 먼저 build()를 실행하세요.")
        return 0

    have = {(e.get("platform"), str(e.get("review_no")))
            for e in _load_examples()}
    n = 0
    for r in data:
        reply = (r.get("owner_reply") or "").strip()
        d = _parse_date(r.get("written_date"))
        if not reply or not d or (since_d and d < since_d):
            continue
        if (r.get("platform"), str(r.get("review_no"))) in have:
            continue  # 웹 게시로 이미 학습됨(초안 diff 보존)
        record_example(r, reply, source="crawled")
        n += 1
    logger.info("재크롤링 답글 %d건을 승인 예시로 편입(%s 이후)", n, since)
    return n


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    if "--import-approved" in sys.argv:
        # 사용: python -m crawler.reply_history --import-approved [YYYY-MM-DD]
        i = sys.argv.index("--import-approved")
        since = sys.argv[i + 1] if len(sys.argv) > i + 1 else \
            PROGRAM_LAUNCH_DATE
        n = import_recent_approved(since)
        print(f"승인 예시 편입: {n}건 ({since} 이후)")
    else:
        d = build(days=90)
        print(f"수집 완료: {len(d)}건 → reference/")
        n = import_recent_approved()
        print(f"승인 예시 편입: {n}건 ({PROGRAM_LAUNCH_DATE} 이후)")
