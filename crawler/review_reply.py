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
import os
import random
import re

from assistant.beargels import (
    _clean_author, classify_review, generate_review_reply,
)
from crawler.browser import (
    BrowserSession, SessionExpiredError, human_pause, is_session_expired,
)
from crawler.coupang import STORE_ID
from crawler.write_guard import WriteAction

logger = logging.getLogger(__name__)


def _coupang_already_replied(code, err):
    """쿠팡 응답이 '이미 답글이 있다'는 거절인지 판별한다.

    코드가 50001 로 오지만 문자열/숫자·필드 위치가 배포마다 흔들려, 코드와
    메시지를 함께 느슨하게 본다(오탐해도 '수정'으로 한 번 더 시도할 뿐이다).
    """
    blob = f"{code} {err}"
    return "50001" in blob or "이미" in blob


def _coupang_ok(status, code, err):
    """쿠팡 답글 API 응답이 성공인지 판별한다.

    ⚠️ 예전엔 error 필드가 비면 성공으로 봤는데, 거절 코드(50001)만 오고
    error 가 없는 응답을 '게시 완료'로 오인할 수 있었다 — 거절 코드는 명시적
    으로 실패로 본다.
    """
    if status != 200 or err:
        return False
    return code == "SUCCESS" or not _coupang_already_replied(code, err)


COUPANG_REPLY_API = "https://store.coupangeats.com/api/v1/merchant/reviews/reply"
# 이미 답글이 있는 리뷰는 reply 가 50001 로 거절된다 — 수정은 별도 엔드포인트
# (JS 번들 정적 분석으로 확인, 2026-08-12).
COUPANG_REPLY_MODIFY_API = COUPANG_REPLY_API + "/modify"
COUPANG_REVIEWS_URL = "https://store.coupangeats.com/merchant/management/reviews"
COUPANG_REPLY_GLOB = "**/merchant/reviews/reply"

BAEMIN_REVIEWS_URL = "https://self.baemin.com/shops/reviews"
# 안정적 시맨틱: 사장님 댓글 작성 컴포넌트 접두사 + 버튼 텍스트(해시 클래스 금지).
BAEMIN_CREATOR_SEL = '[class*="CEOCommentCreator-module__"]'
# ⚠️ 배민이 문구를 바꾼다 — 2026-08-27 실측 화면은 '추가하기' 다
#    (예전엔 '등록하기'). 등록기는 옛 문구만 찾다가 "이미 답글이 있다"고
#    오판해 게시가 막혔다. 수집기는 진작 둘 다 알고 있었다(baemin.py).
BAEMIN_REPLY_BTN_TEXTS = ("사장님 댓글 등록하기", "사장님 댓글 추가하기")
BAEMIN_REPLY_BTN_RE = re.compile(r"사장님 댓글 (?:등록|추가)하기")
BAEMIN_REPLY_BTN_TEXT = BAEMIN_REPLY_BTN_TEXTS[0]   # 메시지용
BAEMIN_SUBMIT_TEXTS = ("등록", "댓글 등록", "답글 등록")

_PLAT_LABEL = {"baemin": "배민", "coupang": "쿠팡"}


# 배민 리뷰 목록을 넓혀보는 최대 횟수 — 한 묶음이 10건쯤이라 30회면 300건
# 안팎까지 닿는다(오래된 리뷰 수정까지 커버).
# 한 라운드가 한 화면이라, 오래된 리뷰일수록 라운드가 많이 필요하다.
# 실측(2026-08-27): 30라운드로 9일치(63건)까지 내려갔다 — 30은 부족했다.
BAEMIN_LOAD_ROUNDS = int(os.getenv("BAEMIN_LOAD_ROUNDS", "120"))


def _click_baemin_more(page):
    """리뷰 목록의 '더보기'만 눌러 다음 묶음을 불러온다(눌렀으면 True).

    ⚠️ 페이지 아래쪽 **도움말(자주 묻는 질문)에도 '더보기'가 있다.** 문서
       전체에서 찾으면 그게 눌려 ceo.baemin.com/qna 로 튕기고, 그 뒤로는
       리뷰가 없는 화면만 계속 뒤진다(사장님 제보 2026-08-16 — 좌표로
       거르던 조건을 없앴다가 재발시켰다).
       그래서 **리뷰 카드를 담고 있는 컨테이너 안에서만** 찾는다: 마지막
       카드에서 부모로 몇 단계만 올라가며 뒤지므로, 문서 끝의 도움말
       버튼에는 애초에 닿지 않는다.
    """
    try:
        return page.evaluate(
            """() => {
                const cards = document.querySelectorAll(
                    '[class*="ReviewContent-module__"]');
                if (!cards.length) return false;
                const bad = /footer|nav|gnb|header|help|faq|qna/i;
                const ok = (b) => {
                    if ((b.textContent || '').trim() !== '더보기') return false;
                    if (b.closest('a, footer, nav, header')) return false;
                    for (let e = b; e; e = e.parentElement) {
                        if (typeof e.className === 'string' && bad.test(e.className))
                            return false;
                    }
                    return true;
                };
                // 후보 중 '리뷰 목록에 속한' 버튼만 고른다 — 버튼에서 위로
                // 몇 단계만 올라가 **리뷰 카드를 품은 조상**이 있으면 그게
                // 목록의 더보기다. 도움말(FAQ) 쪽 버튼은 가까운 조상에
                // 리뷰 카드가 없어 걸러진다(문서 끝까지 올라가지 않는다).
                const inList = (b) => {
                    let e = b.parentElement;
                    for (let up = 0; up < 6 && e && e !== document.body; up++) {
                        if (e.querySelector('[class*="ReviewContent-module__"]'))
                            return true;
                        e = e.parentElement;
                    }
                    return false;
                };
                const btn = [...document.querySelectorAll('button')]
                              .filter(ok).find(inList);
                if (!btn) return false;
                btn.click();
                return true;
            }""")
    except Exception:  # noqa: BLE001 — 더보기 실패가 게시를 막지 않게
        logger.debug("배민 '더보기' 클릭 실패(무시)")
        return False


def _squash(text):
    """공백·줄바꿈을 모두 없앤 비교용 문자열.

    화면 카드 텍스트와 DB 본문은 공백 처리가 달라서, 그대로 비교하면 같은
    리뷰도 다르다고 나온다(2026-08-27).
    """
    return re.sub(r"\s+", "", text or "")


def _baemin_seen_review_nos(page):
    """지금 화면에 로드된 리뷰 카드들의 '리뷰번호'를 모은다(진단용).

    카드가 몇 개 열렸는지만으로는 원인을 못 가른다 — 실제로 어떤 번호들이
    보였는지 남겨야 '목록에 없음' 과 '매칭 실패' 를 구분할 수 있다.
    """
    try:
        return page.evaluate(
            r"""() => [...document.querySelectorAll(
                    '[class*="ReviewContent-module__"]')]
                .map(c => ((c.innerText || '').match(/리뷰번호\s*(\d+)/) || [])[1])
                .filter(Boolean)""") or []
    except Exception:  # noqa: BLE001 — 진단 실패가 본 오류를 가리지 않게
        return []


def _baemin_click(locator, what="버튼"):
    """배민 화면의 버튼을 누른다 — 일반 클릭이 막히면 DOM click 으로 재시도.

    상단 고정 헤더가 클릭을 가로채 Playwright 클릭이 조용히 실패하는 경우가
    있다(수집기 '더보기'에서도 같은 이유로 DOM click 을 쓴다). 그러면 작성기가
    안 열려 뒤에서 'textarea 가 나타나지 않았습니다'로 끝난다
    (사장님 제보 2026-08-16).
    """
    try:
        locator.scroll_into_view_if_needed(timeout=3000)
    except Exception:  # noqa: BLE001 — 스크롤 실패는 치명적이지 않다
        pass
    try:
        locator.click(timeout=5000)
        return
    except Exception as e:  # noqa: BLE001 — 가려짐/인터셉트 → DOM click 으로
        logger.info("배민 %s 일반 클릭 실패(%s) — DOM click 재시도",
                    what, str(e)[:80])
    try:
        # ⚠️ 텍스트로 찾은 요소는 <button> 이 아니라 그 안의 span 인 경우가
        #    많다. span 을 눌러도 리액트 핸들러가 안 걸려 '조용히 아무 일도
        #    안 일어나고', 뒤에서 'textarea 가 안 나타났다'로 끝난다
        #    (수집기 '더보기'와 같은 유형, 2026-08-16 실측). 실제 버튼으로
        #    올라가서 누른다.
        locator.evaluate(
            "el => (el.closest('button, [role=button]') || el).click()")
    except Exception as e:  # noqa: BLE001
        raise ReplyPostError(f"{what}을(를) 누르지 못했습니다: {str(e)[:120]}")


# 배민 답글 입력칸의 후보 — textarea 만 보다가 '입력창이 없다'로 끝났다
# (사장님 제보 2026-08-16). 요즘 화면은 contenteditable/role=textbox 로 만드는
# 경우가 흔해 셋 다 인정한다.
BAEMIN_EDITOR_SELECTORS = ("textarea", '[contenteditable="true"]',
                           '[role="textbox"]')


def _baemin_find_editor(page, card, timeout_ms=8000):
    """열린 답글 입력칸을 찾는다. (입력칸 Locator, 종류) — 없으면 (None, None).

    ⚠️ 배민 작성기는 리뷰 카드(ReviewContent) **밖**(형제 CEOCommentCreator)에
    열리기도 하고, 입력칸이 textarea 가 아닐 수도 있다. 카드 → 다음 작성기 →
    답글박스 → 화면 전체(딱 하나일 때만) 순으로, 셀렉터 후보를 모두 훑는다.
    """
    import time as _t
    scopes = [
        card,
        card.locator('xpath=following::*[contains(@class,'
                     '"CEOCommentCreator-module__")][1]'),
        card.locator('xpath=following::*[contains(@class,'
                     '"ReviewCommentBox-module__")][1]'),
    ]
    deadline = _t.monotonic() + timeout_ms / 1000
    while _t.monotonic() < deadline:
        for sc in scopes:
            for sel in BAEMIN_EDITOR_SELECTORS:
                try:
                    if sc.count() and sc.locator(sel).count():
                        return sc.locator(sel).first, sel
                except Exception:  # noqa: BLE001 — 렌더 중일 수 있다
                    pass
        for sel in BAEMIN_EDITOR_SELECTORS:   # 화면에 딱 하나면 그게 작성기다
            try:
                if page.locator(sel).count() == 1:
                    return page.locator(sel).first, sel
            except Exception:  # noqa: BLE001
                pass
        # 화면 하단에는 '의견을 남겨주시면…' 피드백 입력칸이 **늘** 떠 있어서
        # 위의 '딱 하나일 때만' 조건이 영영 성립하지 않는다 — 작성기가 멀쩡히
        # 열렸는데도 '입력창이 나타나지 않았습니다'로 끝났다(2026-08-16 실측).
        # 안내문구(placeholder)가 붙은 피드백 칸은 빼고 다시 본다.
        try:
            reply_ta = page.locator(
                'textarea:not([placeholder]), textarea[placeholder=""]')
            if reply_ta.count() == 1:
                return reply_ta.first, "textarea"
        except Exception:  # noqa: BLE001
            pass
        _t.sleep(0.4)
    return None, None


def _baemin_editor_report(page):
    """입력칸을 못 찾았을 때 화면에 뭐가 있었는지 남긴다(진단용)."""
    bits = []
    for sel in BAEMIN_EDITOR_SELECTORS:
        try:
            bits.append(f"{sel}={page.locator(sel).count()}개")
        except Exception:  # noqa: BLE001
            bits.append(f"{sel}=?")
    return " / ".join(bits)


def _baemin_fill_editor(editor, kind, text):
    """입력칸 종류에 맞게 답글을 채우고, 실제로 들어갔는지 확인한다."""
    if kind == "textarea":
        editor.fill(text)
        got = editor.input_value() or ""
    else:
        # contenteditable 은 fill 이 안 먹는 경우가 있어 값을 직접 넣고
        # input 이벤트를 쏴 리액트 상태까지 갱신되게 한다.
        editor.click()
        editor.evaluate(
            """(el, t) => {
                el.focus();
                el.innerText = t;
                el.dispatchEvent(new InputEvent('input', {bubbles: true}));
            }""", text)
        got = editor.inner_text() or ""
    if " ".join(got.split()) != " ".join(text.split()):
        raise ReplyPostError("입력값이 답글과 불일치 — 게시 중단.")


class ReplyPostError(RuntimeError):
    """답글 게시가 확인되지 않았을 때(응답 실패/DOM 미확인 등) 발생."""


class ReplyDeadlineError(ReplyPostError):
    """플랫폼의 답글 작성 기한이 지나 영영 등록할 수 없는 리뷰.

    재시도해도 절대 성공하지 않으므로, 상위(일꾼)에서 '넘어가기'로 정리해
    직원 화면에 계속 뜨지 않게 한다(2026-08-13).
    """


def _coupang_deadline_over(code, err):
    """쿠팡 응답이 '답글 기한 만료'(20051) 거절인지."""
    blob = f"{code} {err}"
    return "20051" in blob or "기한이 지났" in blob


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

    def __init__(self, review, reply_text=None, session=None, allow_edit=False):
        self.review = review or {}
        self.reply_text = reply_text
        self.session = session
        # True 면 이미 답글이 달린 리뷰의 '수정' 경로도 허용(답글 수정 기능).
        self.allow_edit = allow_edit
        # 신규 등록인 줄 알았는데 이미 답글이 있어 '수정'으로 덮어썼는지.
        # (시간차 등록 — 보고에 남겨 사장님이 알 수 있게 한다)
        self.replaced_existing = False

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
        # 수정 모드면 modify 엔드포인트(신규 reply 는 이미 답글 존재 시 50001).
        # modify 는 대상 답글 id(orderReviewReplyId)를 요구한다 — 리뷰
        # 검색 API 를 페이지 컨텍스트에서 호출해 현재 답글 id 를 얻는다.
        url = COUPANG_REPLY_API
        if self.allow_edit:
            url = COUPANG_REPLY_MODIFY_API
            reply_id = self._coupang_reply_id(page, review_id)
            if not reply_id:
                raise ReplyPostError(
                    "수정할 기존 답글 정보가 아직 없어요. 다음 자동 수집(최대 "
                    "2시간) 뒤 다시 시도해 주세요.")
            payload["orderReviewReplyId"] = int(reply_id)
        status, code, err = self._coupang_fetch(page, url, payload)
        if _coupang_ok(status, code, err):
            logger.info("쿠팡 답글 게시 완료 (리뷰 #%s)", review_id)
            return {"platform": "coupang", "review_no": review_id,
                    "status": status, "code": code,
                    "replaced": self.replaced_existing}

        # 답글 기한 만료(20051) — 재시도해도 영영 안 되므로 전용 예외로 올려
        # 상위에서 '넘어가기' 처리한다(직원 화면에 계속 뜨는 것 방지).
        if _coupang_deadline_over(code, err):
            raise ReplyDeadlineError(
                "쿠팡 답글 작성 기한이 지난 리뷰예요 — 등록할 수 없습니다.")

        # 시간차로 이미 답글이 달린 경우(웹 화면엔 미답변인데 앱/다른 경로에서
        # 먼저 등록됨) — 신규 등록은 50001 로 거절된다. 직원이 등록을 누른
        # 내용이 최종본이므로 '수정'으로 자동 전환해 그 내용으로 맞춘다.
        if not self.allow_edit and _coupang_already_replied(code, err):
            reply_id = self._coupang_reply_id(page, review_id)
            if not reply_id:
                raise ReplyPostError(
                    "이미 답글이 달려 있는 리뷰예요. 기존 답글 정보가 아직 "
                    "없어 지금은 수정할 수 없습니다 — 다음 자동 수집(최대 "
                    "2시간) 뒤 자동으로 이 내용으로 맞춰집니다.")
            payload["orderReviewReplyId"] = int(reply_id)
            logger.info("쿠팡 리뷰 #%s 에 이미 답글이 있어 '수정'으로 전환합니다",
                        review_id)
            status, code, err = self._coupang_fetch(
                page, COUPANG_REPLY_MODIFY_API, payload)
            if _coupang_ok(status, code, err):
                self.replaced_existing = True
                logger.info("쿠팡 답글 수정 완료 (리뷰 #%s)", review_id)
                return {"platform": "coupang", "review_no": review_id,
                        "status": status, "code": code, "replaced": True}

        raise ReplyPostError(
            f"쿠팡 답글 게시 실패 status={status} code={code} "
            f"error={json.dumps(err, ensure_ascii=False) if err else None}")

    def _coupang_fetch(self, page, url, payload):
        """페이지 컨텍스트에서 답글 API 를 호출한다(쿠키·Akamai 센서 포함).

        Returns: (status, code, error) — 응답 JSON 에서 뽑은 값.
        """
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
            {"url": url, "body": payload},
        )
        data = result.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        return result.get("status"), data.get("code"), data.get("error")

    def _coupang_reply_id(self, page, review_id):
        """대상 리뷰의 현재 답글 id(orderReviewReplyId)를 찾는다.

        쿠팡은 페이지 밖 '읽기' fetch 가 Akamai 로 막히므로(쓰기만 통과),
        저장해 둔 raw 의 replies[0].orderReviewReplyId 를 쓴다. raw 에 답글이
        아직 없으면(답글 단 뒤 재수집 전) None → 상위에서 '다음 수집 후
        재시도' 안내. (page 인자는 시그니처 호환용, 미사용)
        """
        raw = self.review.get("raw")
        if not raw:
            return None
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:  # noqa: BLE001
            return None
        reps = data.get("replies") or []
        return reps[0].get("orderReviewReplyId") if reps else None

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
        page.goto(BAEMIN_REVIEWS_URL, wait_until="domcontentloaded")
        human_pause(2.0, 3.0)
        if is_session_expired(page):
            raise SessionExpiredError("[배민] 세션 만료 — 재로그인 필요.")
        # 공지 팝업이 떠 있으면 '더보기'·버튼 클릭을 가로챈다 — 수집기와
        # 똑같이 먼저 닫는다. 이걸 안 해서 목록이 안 펼쳐졌다(2026-08-16).
        try:
            page.keyboard.press("Escape")
            human_pause(0.8, 1.2)
        except Exception:  # noqa: BLE001
            pass
        # 지연 로딩 대비: 카드를 찾을 때까지 목록을 넓혀간다.
        #
        # ⚠️ 이 목록은 두 가지 성질을 동시에 갖는다.
        #    ① **가상 목록** — 화면 밖 카드는 DOM 에서 지워진다. 그래서 맨
        #       아래로 한 번에 점프하면 중간 카드를 지나쳐 버린다. 조금씩
        #       내리면서 매번 찾아야 한다.
        #    ② **'더보기' 버튼이 있을 때도, 없을 때도 있다.** 2026-08-27 실측
        #       기준 배민 화면엔 버튼이 아예 없고 무한 스크롤만 있다.
        #       예전 코드는 '더보기가 두 번 연속 없으면 끝'으로 판단해서,
        #       버튼이 사라진 지금은 카드 13개만 보고 포기했다
        #       (사장님 제보 2026-08-27: 8/8 자 리뷰 등록 반복 실패).
        #
        #    그래서 끝 판정을 **'새 리뷰번호가 더 나오는가'** 로 바꾼다.
        #    카드 개수로는 판단할 수 없다(가상 목록이라 늘지 않는다).
        card = self._find_baemin_card(page)
        stale = 0
        seen_nos = set(_baemin_seen_review_nos(page))
        for _ in range(BAEMIN_LOAD_ROUNDS):
            if card is not None:
                break
            # 한 번에 바닥까지 가지 않고 한 화면씩 — 지나친 카드는 DOM 에서
            # 지워져 다시 못 찾는다.
            page.evaluate(
                "window.scrollBy(0, Math.round(window.innerHeight * 0.9))")
            # 스크롤은 사람처럼 뜸들일 필요가 없다(클릭·입력과 달리 탐지
            # 대상이 아니다). 예전엔 라운드마다 2.5초씩 쉬어 30라운드에
            # 51초가 걸렸고, 그 사이 화면은 3분 만에 포기했다.
            page.wait_for_timeout(random.randint(280, 480))
            card = self._find_baemin_card(page)
            if card is not None:
                break
            clicked = _click_baemin_more(page)   # 버튼이 있으면 누른다
            page.wait_for_timeout(random.randint(200, 350))
            # 안전망: 엉뚱한 '더보기'로 목록을 벗어나면 되돌아온다. 벗어난 채
            # 계속 뒤지면 영영 못 찾는다(Q&A 화면만 열리던 사고, 2026-08-16).
            url = page.url or ""
            if "self.baemin.com" not in url or "reviews" not in url:
                logger.warning("리뷰 목록을 벗어남(%s) — 돌아갑니다", url[:60])
                page.goto(BAEMIN_REVIEWS_URL, wait_until="domcontentloaded")
                human_pause(2.0, 3.0)
                try:
                    page.keyboard.press("Escape")
                except Exception:  # noqa: BLE001
                    pass
                card = self._find_baemin_card(page)
                break
            # 끝 판정: 새 리뷰번호가 더 안 나오면 바닥에 닿은 것이다.
            # (카드 개수는 가상 목록이라 늘지 않으므로 쓸 수 없다.)
            now_nos = set(_baemin_seen_review_nos(page))
            fresh = now_nos - seen_nos
            seen_nos |= now_nos
            at_bottom = page.evaluate(
                "() => window.innerHeight + window.scrollY"
                " >= document.body.scrollHeight - 4")
            if not fresh and not clicked and at_bottom:
                stale += 1
                if stale >= 3:      # 세 번 연속 새 리뷰가 없으면 끝까지 본 것
                    card = self._find_baemin_card(page)
                    break
            else:
                stale = 0
            card = self._find_baemin_card(page)
        if card is None:
            # 추측하지 않고 '무엇을 봤는지' 남긴다 — 카드가 아예 안 열린
            # 것인지, 열렸는데 번호가 안 맞는 것인지 이 기록으로 갈린다
            # (사장님 제보 2026-08-16: 같은 사유가 계속 반복).
            seen = _baemin_seen_review_nos(page)
            n = page.locator('[class*="ReviewContent-module__"]').count()
            want = str(self.review.get("review_no") or "")
            if not n:
                hint = "리뷰 목록이 아예 안 열렸어요(로그인·화면 구조 확인)."
            elif want in seen:
                hint = "목록엔 있는데 매칭에 실패했어요(코드 확인 필요)."
            else:
                hint = ("목록을 끝까지 넘겼는데 이 리뷰가 없어요 — "
                        "이미 답글이 달렸거나 기간 필터에서 빠졌을 수 있어요.")
            raise ReplyPostError(
                f"대상 배민 리뷰 카드를 찾지 못했습니다. {hint} "
                f"[찾는 번호 {want} / 훑어본 카드 {n}개 / "
                f"화면에서 본 번호 {', '.join(seen[:6]) or '없음'}"
                f"{' …' if len(seen) > 6 else ''}]")

        # 찾은 카드를 화면 안으로 끌어온다 — 가상 목록에서는 화면 밖 카드가
        # DOM 에서 지워져, 아래에서 텍스트를 읽거나 버튼을 누를 때 사라진
        # 요소를 붙잡고 있게 된다(2026-08-27 nth(12) 타임아웃).
        try:
            card.scroll_into_view_if_needed(timeout=5000)
            human_pause(0.3, 0.6)
        except Exception:  # noqa: BLE001 — 못 끌어와도 아래에서 다시 확인한다
            logger.debug("카드 스크롤 실패(무시)")

        # 안전: 엉뚱한 리뷰에 답글이 달리지 않게 본문을 대조한다.
        #
        # ⚠️ 공백을 무시하고 비교해야 한다. 화면 카드 텍스트에는 줄바꿈이
        #    들어가 있는데, DB 에 저장된 본문은 수집 단계에서 공백이 하나로
        #    눌려 있다. 글자 그대로 비교하면 같은 리뷰인데도 어긋난다
        #    (사장님 제보 2026-08-27: 리뷰 2358 등록이 이 검사에서 막혔다).
        content = _squash(self.review.get("content"))
        card_txt = _squash(card.inner_text())
        if content and content[:15] not in card_txt:
            raise ReplyPostError("대상 리뷰 본문이 일치하지 않습니다 — 게시 중단.")

        # 작성기 열기 — 미답변이면 '사장님 댓글 등록하기', 이미 답글이 있고
        # 수정 모드면 답글박스(형제)의 '수정' 버튼.
        open_btn = card.get_by_text(BAEMIN_REPLY_BTN_RE)
        editing = False
        if open_btn.count() == 0:
            # 등록 버튼이 없다 = 이미 답글이 달려 있다. 시간차로 앱/다른 경로에서
            # 먼저 등록된 경우인데, 직원이 등록을 누른 내용이 최종본이므로
            # '수정'으로 전환해 그 내용으로 맞춘다(누락 방지). 덮어쓴 사실은
            # replaced_existing 으로 상위에 알려 보고에 남긴다.
            box = card.locator(
                'xpath=following::*[contains(@class,"ReviewCommentBox-module__")][1]')
            if box.count() == 0:
                raise ReplyPostError(
                    "'사장님 댓글 등록하기' 버튼도, 수정할 답글박스도 "
                    "찾지 못했습니다.")
            if not self.allow_edit:
                self.replaced_existing = True
                logger.info("배민 리뷰 #%s 에 이미 답글이 있어 '수정'으로 "
                            "전환합니다", self.review.get("review_no"))
            edit_btn = box.first.get_by_text("수정", exact=True)
            if edit_btn.count() == 0:
                # 2026-08-27 현재 배민 답글박스에는 '삭제'만 있고 '수정'이
                # 없다. 남의 답글을 말없이 지우는 건 위험하므로 자동으로
                # 처리하지 않고, 사람이 판단하도록 알린다.
                raise ReplyPostError(
                    "이 리뷰엔 이미 답글이 달려 있는데 배민 화면에 '수정' "
                    "버튼이 없어요. 배민 사장님앱에서 기존 답글을 지운 뒤 "
                    "다시 등록해 주세요.")
            _baemin_click(edit_btn.first, "'수정' 버튼")
            editing = True
            card = box.first    # 이후 textarea·버튼은 답글박스 안에서 찾는다
        else:
            # 텍스트가 아니라 **버튼 역할**로 먼저 잡는다 — 텍스트로 잡으면
            # 안쪽 span 이 걸려 클릭이 먹지 않는다(2026-08-16 실측).
            btn = card.get_by_role("button", name=BAEMIN_REPLY_BTN_RE)
            _baemin_click(btn.first if btn.count() else open_btn.first,
                          f"'{BAEMIN_REPLY_BTN_TEXT}' 버튼")
        human_pause(0.8, 1.5)

        # 입력칸은 카드 밖에 열릴 수 있고 textarea 가 아닐 수도 있다.
        editor, kind = _baemin_find_editor(page, card)
        if editor is None:
            raise ReplyPostError(
                "답글 입력칸이 나타나지 않았습니다 — 작성기가 열리지 않았거나 "
                f"배민 화면 구조가 바뀌었을 수 있어요. [화면 상태: "
                f"{_baemin_editor_report(page)}]")
        _baemin_fill_editor(editor, kind, reply)
        human_pause(0.5, 1.0)

        # 제출 버튼: 신규='등록', 수정='저장' (실게시로 확인된 문구, 2026-07-24)
        # 작성기가 카드 밖에 열릴 수 있으므로 범위 → 화면 전체 순으로 찾는다.
        submit_name = "저장" if editing else "등록"
        submit = card.get_by_role("button", name=submit_name, exact=True)
        if submit.count() == 0:
            submit = page.get_by_role("button", name=submit_name, exact=True)
        if submit.count() == 0:
            raise ReplyPostError(
                f"'{submit_name}' 버튼을 찾지 못했습니다(텍스트 확인 필요).")
        _baemin_click(submit.first, f"'{submit_name}' 버튼")
        human_pause(1.8, 2.8)

        logger.info("배민 답글 게시 완료 (리뷰 #%s)",
                    self.review.get("review_no"))
        return {"platform": "baemin",
                "review_no": self.review.get("review_no"),
                "replaced": self.replaced_existing}

    def _find_baemin_card(self, page):
        """대상 배민 리뷰 카드(ReviewContent) Locator 를 반환한다(없으면 None).

        매칭 우선순위:
          1) 리뷰번호 텍스트
          2) 작성자 + 본문
          3) 작성자 + '사장님 댓글 등록하기'(미답변) — **유일할 때만**.
             내용 없는(별점/사진만) 리뷰는 본문 매칭이 불가능해 1차 일괄
             등록에서 10건이 전부 실패했다(2026-08-12). 같은 작성자의
             미답변 카드가 2개 이상이면 오게시 위험이라 포기한다.

        ⚠️ **리뷰번호를 아는데 못 찾았으면 2·3번으로 내려가지 않는다.**
           단골은 같은 닉네임으로 리뷰를 여러 번 남기므로, 아직 화면에
           안 뜬 리뷰를 찾는 중에 '같은 사람의 다른 리뷰'를 잡아 **엉뚱한
           리뷰에 답글이 달릴 뻔했다**(2026-08-16 실측: 찾는 번호
           …0826 인데 …1084 카드가 잡힘). 못 찾으면 None 을 돌려 위에서
           목록을 더 펼치게 한다.
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
        author_only_hits = []
        for i in range(cards.count()):
            c = cards.nth(i)
            # ⚠️ 가상 목록이라 훑는 도중에 카드가 화면 밖으로 밀려 DOM 에서
            #    사라진다. 그러면 inner_text() 가 15초를 기다리다 통째로
            #    실패한다(사장님 제보 2026-08-27: nth(12) 타임아웃).
            #    사라진 카드는 조용히 건너뛴다 — 다음 라운드에 다시 만난다.
            try:
                txt = c.inner_text(timeout=2000)
            except Exception:  # noqa: BLE001
                continue
            # 공백을 모두 지우고 번호만 본다 — '리뷰번호'와 숫자가 다른
            # 요소로 쪼개져 줄바꿈·이중공백·비단절공백이 끼면 'f"리뷰번호 {rid}"'
            # 형태의 매칭이 조용히 빗나간다(리뷰번호는 16자리라 단독으로도
            # 충분히 유일하다).
            if rid and str(rid) in re.sub(r"\s+", "", txt):
                return c
            if rid:
                continue        # 번호를 아는 건 번호로만 — 오게시 방지
            if author and content and author in txt and content in txt:
                return c
            if (author and not content and author in txt
                    and any(t in txt for t in BAEMIN_REPLY_BTN_TEXTS)):
                author_only_hits.append(c)
        if len(author_only_hits) == 1:
            return author_only_hits[0]
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
