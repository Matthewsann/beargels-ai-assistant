"""리뷰 답글 게시 — WriteAction 기반 (기본 dry-run, 실게시는 승인+비-dry-run).

⚠️⚠️ 실제 고객에게 노출되는 쓰기다. 안전 계약(write_guard.WriteAction):
  1. 기본 dry-run(.env WRITE_DRY_RUN=true) → preview() 만, 게시 안 함.
  2. 실게시는 run(confirm=True) + WRITE_DRY_RUN=false 둘 다 필요.
  3. 🚨 에스컬레이션(이물질·환불·법적 등) 리뷰는 confirm 이어도 게시 거부 —
     반드시 사장님이 직접 대응.
  4. 첫 실게시는 반드시 사장님(Matthew) 승인·감독 하에. 잘못된 답글이 실고객에
     그대로 노출되므로 자동 게시 금지.

플랫폼별 게시 경로(2026-07 정찰로 확인):
  쿠팡: POST /api/v1/merchant/reviews/reply  body={storeId, orderReviewId, comment}
        (Akamai 봇차단 → 리뷰 페이지 컨텍스트에서 요청)
  배민: 리뷰 카드의 'CEOCommentCreator' → '사장님 댓글 등록하기' 버튼 클릭 →
        나타나는 textarea 채우고 등록 버튼 클릭(DOM).

⚠️ _apply()(실게시 경로)는 실고객 write 라 이 프로젝트에서 실행 검증을 하지
   않았다(테스트 게시 금지). 첫 실행은 감독 하에 진행하고, 실패 시 각 메서드의
   대체 경로 주석을 따른다.
"""

import json
import logging

from assistant.beargels import (
    _clean_author, classify_review, generate_review_reply,
)
from crawler.browser import (
    BrowserSession, SessionExpiredError, human_pause, is_session_expired,
)
from crawler.coupang import STORE_ID
from crawler.write_guard import WriteAction

logger = logging.getLogger(__name__)

COUPANG_REPLY_API = "https://store.coupangeats.com/api/v1/merchant/reviews/reply"
COUPANG_REVIEWS_URL = "https://store.coupangeats.com/merchant/management/reviews"
COUPANG_REPLY_GLOB = "**/merchant/reviews/reply"

BAEMIN_REVIEWS_URL = "https://self.baemin.com/shops/reviews"
# 안정적 시맨틱: 사장님 댓글 작성 컴포넌트 접두사 + 버튼 텍스트(해시 클래스 금지).
BAEMIN_CREATOR_SEL = '[class*="CEOCommentCreator-module__"]'
BAEMIN_REPLY_BTN_TEXT = "사장님 댓글 등록하기"
BAEMIN_SUBMIT_TEXTS = ("등록", "댓글 등록", "답글 등록")

_PLAT_LABEL = {"baemin": "배민", "coupang": "쿠팡"}


class ReplyPostError(RuntimeError):
    """답글 게시가 확인되지 않았을 때(응답 실패/DOM 미확인 등) 발생."""


class ReplyToReviewAction(WriteAction):
    """리뷰 1건에 답글을 게시하는 쓰기 액션(dry-run 기본).

    Args:
        review: 크롤러가 정규화한 리뷰 dict(platform, review_no, author,
                rating, content, menus, order_count 등).
        reply_text: 게시할 답글 본문. None 이면 preview 시 생성기로 초안 생성.
        session: 재사용할 BrowserSession(있으면 그 page 사용). 없으면 _apply
                 에서 새로 연다. dry-run(preview)은 브라우저를 열지 않는다.
    """

    name = "reply-to-review"

    def __init__(self, review, reply_text=None, session=None):
        self.review = review or {}
        self.reply_text = reply_text
        self.session = session

    # -- 미리보기(부작용 없음) ---------------------------------------------

    def draft(self):
        """게시 후보 답글 텍스트를 반환한다(없으면 생성기로 초안 생성)."""
        if self.reply_text is None:
            self.reply_text = generate_review_reply(self.review)
        return self.reply_text

    def preview(self):
        plat = _PLAT_LABEL.get(self.review.get("platform"),
                               self.review.get("platform") or "?")
        rid = self.review.get("review_no")
        author = _clean_author(self.review.get("author"))
        rating = self.review.get("rating")
        content = (self.review.get("content") or "(사진/무텍스트)").strip()
        reply = self.draft()
        escalate = classify_review(self.review) == "escalate"
        lines = [
            f"[{plat}] 리뷰 답글 게시 미리보기",
            f"  리뷰: ★{rating} {author} (#{rid})",
            f"  원문: \"{content[:120]}\"",
            "  ────",
            f"  답글: {reply}",
        ]
        if escalate:
            lines.append("  🚨 에스컬레이션 리뷰 — 자동 게시 차단(사장님 직접 대응).")
        return "\n".join(lines)

    # -- 실제 게시(승인 + 비-dry-run 에서만 run() 이 호출) -------------------

    def _apply(self):
        """실제 답글을 게시한다. run(confirm=True) + WRITE_DRY_RUN=false 필요.

        🚨 에스컬레이션 리뷰는 여기서도 거부한다(2중 안전장치).
        """
        if classify_review(self.review) == "escalate":
            raise ReplyPostError(
                "에스컬레이션(민감) 리뷰는 자동 게시 불가 — 사장님이 직접 대응하세요.")

        reply = self.draft()
        if not reply or reply.strip().startswith("⚠️"):
            raise ReplyPostError("게시할 답글 초안이 유효하지 않습니다.")

        platform = self.review.get("platform")
        own_session = self.session is None
        session = self.session or BrowserSession()
        if own_session:
            session.__enter__()
        try:
            page = session.page
            if platform == "coupang":
                return self._apply_coupang(page, reply)
            if platform == "baemin":
                return self._apply_baemin(page, reply)
            raise ReplyPostError(f"알 수 없는 플랫폼: {platform!r}")
        finally:
            if own_session:
                session.__exit__(None, None, None)

    # -- 쿠팡: 리뷰 페이지 컨텍스트에서 reply API POST -----------------------

    def _apply_coupang(self, page, reply):
        """쿠팡 답글 게시.

        Akamai 봇차단 때문에 페이지 밖 호출은 403 이 나므로, 리뷰 페이지를 연
        뒤 그 페이지 컨텍스트(same-origin, 쿠키·센서 초기화됨)에서 reply API 로
        POST 한다.

        ⚠️ 대체 경로(이 경로가 403 이면): 리뷰 페이지에서 해당 리뷰의 답글
           입력창을 채우고 '등록' 버튼을 실제 클릭해 페이지가 스스로 요청을
           날리게 한다(사람과 동일 경로). 첫 실게시 감독 시 확인.
        """
        review_id = self.review.get("review_no")
        if not review_id:
            raise ReplyPostError("쿠팡 리뷰 orderReviewId(review_no)가 없습니다.")

        page.goto(COUPANG_REVIEWS_URL, wait_until="domcontentloaded")
        human_pause(2.0, 3.0)
        if is_session_expired(page):
            raise SessionExpiredError("[쿠팡] 세션 만료 — 재로그인 필요.")

        payload = {
            "storeId": int(STORE_ID),
            "orderReviewId": int(review_id),
            "comment": reply,
        }
        # 페이지 컨텍스트에서 fetch(쿠키·Akamai 센서 포함). 응답 JSON 반환.
        result = page.evaluate(
            """async ({url, body}) => {
                const r = await fetch(url, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json;charset=UTF-8'},
                    credentials: 'include',
                    body: JSON.stringify(body),
                });
                let data = null;
                try { data = await r.json(); } catch (e) {}
                return {status: r.status, data};
            }""",
            {"url": COUPANG_REPLY_API, "body": payload},
        )
        status = result.get("status")
        data = result.get("data") or {}
        code = data.get("code") if isinstance(data, dict) else None
        err = data.get("error") if isinstance(data, dict) else None
        if status == 200 and (code == "SUCCESS" or not err):
            logger.info("쿠팡 답글 게시 완료 (리뷰 #%s)", review_id)
            return {"platform": "coupang", "review_no": review_id,
                    "status": status, "code": code}
        raise ReplyPostError(
            f"쿠팡 답글 게시 실패 status={status} code={code} "
            f"error={json.dumps(err, ensure_ascii=False) if err else None}")

    # -- 배민: DOM(사장님 댓글 등록하기 → textarea → 등록) ------------------

    def _apply_baemin(self, page, reply):
        """배민 답글 게시(DOM 조작).

        1) 리뷰 페이지에서 대상 리뷰 카드를 찾는다(리뷰번호 우선, 없으면 작성자
           +본문 매칭).
        2) 그 카드의 '사장님 댓글 등록하기' 버튼을 눌러 작성기를 연다.
        3) textarea 를 답글로 채우고 '등록' 버튼을 클릭한다.
        4) 답글 텍스트가 카드에 반영됐는지 확인.

        ⚠️ 배민은 CSS-module 해시 클래스가 배포마다 바뀌므로 접두사/버튼 텍스트로만
           잡는다. 리뷰 카드는 'ReviewContent-module__' 이고 그 안에 '사장님 댓글
           등록하기' 버튼·textarea 가 있다. 제출 버튼은 '등록'(정확 매칭). 실계정
           게시로 검증됨(2026-07-24).
        """
        from playwright.sync_api import TimeoutError as PWTimeout

        page.goto(BAEMIN_REVIEWS_URL, wait_until="domcontentloaded")
        human_pause(2.0, 3.0)
        if is_session_expired(page):
            raise SessionExpiredError("[배민] 세션 만료 — 재로그인 필요.")
        # 지연 로딩 대비 스크롤
        for _ in range(4):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            human_pause(1.2, 2.0)

        card = self._find_baemin_card(page)
        if card is None:
            raise ReplyPostError("대상 배민 리뷰 카드를 찾지 못했습니다.")

        # 안전: 대상 리뷰가 정확히 1건인지 + 본문 일치 확인
        content = (self.review.get("content") or "").strip()
        card_txt = card.inner_text()
        if content and content[:15] not in card_txt:
            raise ReplyPostError("대상 리뷰 본문이 일치하지 않습니다 — 게시 중단.")

        # 작성기 열기
        open_btn = card.get_by_text(BAEMIN_REPLY_BTN_TEXT, exact=False)
        if open_btn.count() == 0:
            raise ReplyPostError(
                "이미 답글이 있거나 '사장님 댓글 등록하기' 버튼이 없습니다.")
        open_btn.first.click()
        human_pause(0.8, 1.5)

        # textarea 채우기 + 입력값 검증
        ta = card.locator("textarea")
        try:
            ta.wait_for(timeout=5000)
        except PWTimeout:
            raise ReplyPostError("답글 입력창(textarea)이 나타나지 않았습니다.")
        ta.fill(reply)
        human_pause(0.5, 1.0)
        if (ta.input_value() or "").strip() != reply.strip():
            raise ReplyPostError("입력값이 답글과 불일치 — 게시 중단.")

        # '등록' 버튼(정확 매칭 — '사장님 댓글 등록하기' 와 구분)
        submit = card.get_by_role("button", name="등록", exact=True)
        if submit.count() == 0:
            raise ReplyPostError("'등록' 버튼을 찾지 못했습니다(텍스트 확인 필요).")
        submit.first.click()
        human_pause(1.8, 2.8)

        logger.info("배민 답글 게시 완료 (리뷰 #%s)",
                    self.review.get("review_no"))
        return {"platform": "baemin",
                "review_no": self.review.get("review_no")}

    def _find_baemin_card(self, page):
        """대상 배민 리뷰 카드(ReviewContent) Locator 를 반환한다(없으면 None).

        리뷰번호 우선, 없으면 작성자+본문으로 매칭. 검증됨(2026-07).
        """
        rid = self.review.get("review_no")
        if rid:
            hit = page.locator('[class*="ReviewContent-module__"]').filter(
                has_text=str(rid))
            if hit.count() == 1:
                return hit.first
        cards = page.locator('[class*="ReviewContent-module__"]')
        author = self.review.get("author")
        content = (self.review.get("content") or "").strip()[:20]
        for i in range(cards.count()):
            c = cards.nth(i)
            txt = c.inner_text()
            if rid and f"리뷰번호 {rid}" in txt.replace("\n", " "):
                return c
            if author and content and author in txt and content in txt:
                return c
        return None


# ---------------------------------------------------------------------------
# 승인 게이트 오케스트레이션 (미답변 리뷰 → 초안 제시)
# ---------------------------------------------------------------------------

def propose_replies(reviews, limit=10):
    """미답변 리뷰에 대한 답글 초안 제안 목록을 만든다(게시 안 함, dry-run).

    Args:
        reviews: 크롤링한 리뷰 dict 리스트.
        limit: 최대 제안 수.

    Returns: [{review, draft, escalate, preview, action}] 리스트.
        - action: 승인 시 .run(confirm=True) 로 게시할 ReplyToReviewAction.
        - escalate=True 는 자동 게시 대상이 아님(사장님 직접 대응).
    """
    proposals = []
    for r in reviews:
        if r.get("reply_status") not in (None, "none", ""):
            continue  # 이미 답변함
        action = ReplyToReviewAction(r)
        escalate = classify_review(r) == "escalate"
        proposals.append({
            "review": r,
            "draft": action.draft(),
            "escalate": escalate,
            "preview": action.preview(),
            "action": action,
        })
        if len(proposals) >= limit:
            break
    return proposals


if __name__ == "__main__":
    # 단독 실행: 실크롤 → 미답변 리뷰 초안 제안까지 (dry-run, 게시 없음).
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(levelname)s: %(message)s")
    from crawler.coupang import CoupangCrawler

    with CoupangCrawler() as c:
        revs = c.fetch_reviews(days=30, max_pages=3)
    props = propose_replies(revs, limit=5)
    print(f"\n미답변 리뷰 답글 초안 {len(props)}건 (dry-run, 게시 안 함):\n")
    for p in props:
        print(p["preview"])
        # dry-run 계약 확인: WRITE_DRY_RUN=true 면 게시 안 하고 미리보기만.
        res = p["action"].run()  # confirm 없음 → dry-run
        assert res["applied"] is False and res["dry_run"] is True
        print("  → dry-run OK (applied=False)\n")
