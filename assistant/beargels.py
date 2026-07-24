"""
베어글스 AI 비서

수집된 주문/리뷰 데이터를 분석해 Claude(Anthropic API)로 사장님용 리포트를
생성한다.

설계 원칙:
  - 숫자(주문 수·매출·평점 분포)는 파이썬에서 결정론적으로 계산한다.
    LLM 에게 산수를 시키지 않는다(환각/오차 방지).
  - Claude 는 그 숫자와 리뷰 원문을 받아 "인사이트·리뷰 요약·주의 리뷰"
    같은 자연어 판단만 담당한다.
  - 결과는 텔레그램으로 그대로 보낼 수 있는 한국어 텍스트다.

모델: claude-opus-4-8 (adaptive thinking). ANTHROPIC_API_KEY 는 .env 에서 로드.
"""

import logging
import os
import random
import re
from collections import Counter
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

# 답글 생성 백데이터(사실/맥락) — reference/reply_context.md (crawler.reply_history
# 가 생성). 실제 메뉴명·서비스·배달 맥락을 참고해 없는 사실을 지어내지 않게 한다.
_REPLY_CONTEXT_PATH = (
    Path(__file__).resolve().parent.parent / "reference" / "reply_context.md")
_REPLY_CONTEXT_CACHE = None


def _reply_context():
    """reply_context.md(사실 백데이터)를 읽어 캐시한다. 없으면 빈 문자열."""
    global _REPLY_CONTEXT_CACHE
    if _REPLY_CONTEXT_CACHE is None:
        try:
            _REPLY_CONTEXT_CACHE = _REPLY_CONTEXT_PATH.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            _REPLY_CONTEXT_CACHE = ""
    return _REPLY_CONTEXT_CACHE

logger = logging.getLogger(__name__)

# 분석용 모델 — .env BEARGELS_MODEL 로 교체 가능(비용/품질 조절).
#   claude-haiku-4-5(~$2/월) < claude-sonnet-5(~$4) < claude-opus-4-8(~$11)
MODEL = os.getenv("BEARGELS_MODEL", "claude-opus-4-8")
_client = None

# 비서 페르소나 — 모든 Claude 호출의 기본 system 프롬프트.
PERSONA = (
    "너는 인천 송도 베이글카페 '베어글스'(@beargels_songdo) 사장님 Matthew의 "
    "운영 비서다. 메뉴는 베이글·샌드위치·음료, 채널은 배민·쿠팡이츠·매장.\n"
    "말투 규칙: 친근하지만 직설적이고 간결하게. 불필요한 칭찬·인사치레 금지. "
    "데이터에 근거해서만 말하고 솔직하게 피드백한다. 핵심만. 한국어."
)

# 고객 답글 페르소나 — 베어글스의 '실제' 답글 스타일을 학습해 반영(2026-07).
REPLY_PERSONA = (
    "너는 인천 송도 베이글카페 '베어글스' 사장님이다. 고객 리뷰에 직접 답글을 단다.\n"
    "진짜 사장님이 손으로 쓴 것처럼 자연스럽고 편한 '해요체'로 쓴다. 답글이 "
    "AI/자동응답/공지처럼 보이면 실패다.\n"
    "규칙:\n"
    "- **반드시 고객의 실제 닉네임으로 부른다**: \"{닉네임}님,\" 으로 시작. "
    "'고객님'/'고객'으로 부르지 않는다(닉네임이 있으면 무조건 그걸 쓴다).\n"
    "- 닉네임 뒤 줄바꿈 후 본문. 길이는 넉넉하게(주어진 글자수를 잘 채우도록) "
    "쓰되, 억지로 늘리거나 같은 말을 반복하지 않는다. 모든 문장이 진짜 내용"
    "(구체적 메뉴 반응·감사·재방문·안부)이어야 한다.\n"
    "- 편한 구어체. 가끔 ㅎㅎ, ~, ! 나 😊🥹🐻 같은 이모지 하나 정도는 자연스럽게.\n"
    "- 주문 횟수 개인화: 첫 주문이면 '처음 오셨는데 입맛에 맞으셨다니 좋네요' 류, "
    "재주문이면 '벌써 N번째네요, 늘 감사해요' 처럼 단골을 알아봐 준다.\n"
    "- 리뷰에 언급된 메뉴/사진/경험에 구체적으로 반응한다(뭉뚱그리지 말 것).\n"
    "- 배민·쿠팡이츠는 '배달'이다. '방문/오세요/들러주세요' 대신 '주문 주세요/"
    "시켜주세요'로 쓴다.\n"
    "- 없는 사실을 지어내지 않는다(예: 확인 안 된 조리 과정·수제 여부). 주어진 "
    "[참고 사실]과 리뷰에 있는 내용으로만 반응한다.\n"
    "- 🙅 지킬 수 없는 약속을 하지 않는다: 공짜 증정·서비스·할인·신메뉴 챙겨주기·"
    "특별대우 등 확답 금지(예: '새로 나온 거 챙겨드릴게요', '서비스 드릴게요'). "
    "감사·재주문 인사로 자연스럽게 마무리하되 실현 못 할 약속은 넣지 않는다.\n"
    "🚫 절대 쓰지 마라(AI·공지 말투): '~바라요/바랍니다', '되셨으면 좋겠어요', "
    "'되었길 바랍니다', '즐거운 한 끼', '정성껏 준비하겠습니다', '정성을 다하겠습니다', "
    "'큰 힘이 됩니다', '보답하겠습니다', '항상 최선을'. 딱딱한 격식체 금지.\n"
    "✅ 이런 느낌으로: '맛있게 드셨다니 다행이에요!', '저희가 다 기분 좋네요 ㅎㅎ', "
    "'또 들러주세요~', '덕분에 힘나요', '담에 또 뵈어요!'\n"
    "저평점이면 톤은 유지하되 변명 없이 진심으로 사과하고, '바로 확인하고 고칠게요', "
    "'다시 오시면 제대로 보여드릴게요' 처럼 구체적으로 말한다.\n"
    "[좋은 예시]\n"
    "★5 첫주문 → \"KIM님,\\n처음 오셨는데 입맛에 맞으셨다니 너무 좋네요! 담에 또 "
    "들러주세요 😊\"\n"
    "★5 3번째 → \"이우님,\\n벌써 세 번째네요, 알아봐주고 또 와주셔서 진짜 감사해요 "
    "ㅎㅎ 담에도 맛있게 구워둘게요!\""
)

# 플랫폼별 답글 규정 — 실제 입력창 maxlength 로 확정(2026-07-24 편집창 확인).
#  · 쿠팡: 답글 textarea maxlength=300 (하드 제한, 확인됨) → 300 초과 불가.
#  · 배민: 답글 textarea 에 maxlength 미설정(-1). 하드 제한은 없으나 정책 통상
#    ~500자 → 안전하게 500 을 목표 상한으로 둔다.
#  target_len: '꽉 채워' 생성 시 목표 길이(사장님 요청). max_len 은 절대 상한.
PLATFORM_REPLY = {
    "baemin":  {"label": "배민",     "max_len": 500, "target_len": 480},
    "coupang": {"label": "쿠팡이츠", "max_len": 300, "target_len": 290},
}


def get_client():
    """Anthropic 클라이언트(싱글턴)."""
    global _client
    if _client is None:
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise RuntimeError(".env 에 ANTHROPIC_API_KEY 를 설정하세요.")
        _client = Anthropic()
    return _client


# ---------------------------------------------------------------------------
# 결정론적 통계 (LLM 에 넘기지 않는다)
# ---------------------------------------------------------------------------

def compute_order_stats(orders):
    """주문 리스트에서 매출 지표를 계산한다."""
    total = sum(o.get("price") or 0 for o in orders)
    n = len(orders)
    # 인기 메뉴 상위 (메뉴 문자열 기준 단순 집계)
    menu_counter = Counter(
        (o.get("menu") or "").strip() for o in orders if o.get("menu"))
    top_menus = menu_counter.most_common(5)
    return {
        "order_count": n,
        "revenue": total,
        "avg_order_value": round(total / n) if n else 0,
        "top_menus": top_menus,
    }


def compute_review_stats(reviews):
    """리뷰 리스트에서 평점 분포/부정 리뷰를 계산한다."""
    ratings = [r.get("rating") for r in reviews if r.get("rating") is not None]
    dist = Counter(ratings)
    avg = round(sum(ratings) / len(ratings), 2) if ratings else None
    # 별점 3 이하 또는 본문이 있는 저평점 = 주의 리뷰 후보
    negatives = [r for r in reviews
                 if (r.get("rating") is not None and r["rating"] <= 3)]
    return {
        "review_count": len(reviews),
        "avg_rating": avg,
        "distribution": {s: dist.get(s, 0) for s in range(5, 0, -1)},
        "negatives": negatives,
    }


# ---------------------------------------------------------------------------
# Claude 호출
# ---------------------------------------------------------------------------

class LLMUnavailable(RuntimeError):
    """Claude 호출 실패(크레딧 부족·네트워크 등)."""


def _ask_claude(system, user, max_tokens=1500):
    """Claude 에 질의한다. 실패 시 LLMUnavailable 로 감싸 던진다.

    호출부는 이를 잡아 '숫자 리포트는 그대로, AI 부분만 생략' 형태로
    graceful degrade 한다(LLM 장애가 리포트 전체를 막지 않도록).
    """
    try:
        resp = get_client().messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            thinking={"type": "adaptive"},
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Claude 호출 실패: %s", str(e)[:200])
        raise LLMUnavailable(str(e)) from e
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def summarize_reviews(reviews, max_reviews=40):
    """리뷰 목록을 Claude 로 요약한다. 부정 리뷰는 별도로 짚어준다."""
    if not reviews:
        return "리뷰가 없습니다."
    stats = compute_review_stats(reviews)

    lines = []
    for r in reviews[:max_reviews]:
        body = (r.get("content") or "(사진/무텍스트)").replace("\n", " ")
        lines.append(f"- ★{r.get('rating')} [{r.get('author')}] {body}")
    review_block = "\n".join(lines)

    system = (
        "너는 베어글스라는 베이글 카페의 리뷰 분석가다. 사장님이 빠르게 읽을 수 "
        "있게 한국어로 간결하게 답한다. 과장 없이 사실 기반으로, 실행 가능한 "
        "포인트 위주로 정리한다."
    )
    user = (
        f"평점 평균 {stats['avg_rating']}, 분포 {stats['distribution']}.\n"
        f"아래는 최근 리뷰다:\n{review_block}\n\n"
        "다음을 정리해줘:\n"
        "1) 칭찬 포인트 (반복 언급되는 강점)\n"
        "2) 개선 포인트 (불만·아쉬움)\n"
        "3) 즉시 대응이 필요한 리뷰 (있으면 작성자와 이유)"
    )
    try:
        return _ask_claude(system, user)
    except LLMUnavailable:
        neg = stats["negatives"]
        note = (f"⚠️ AI 요약 실패(크레딧/네트워크 확인). "
                f"평균 ★{stats['avg_rating']}, 저평점 리뷰 {len(neg)}건.")
        return note


def generate_daily_report(orders, reviews):
    """일일 매출/리뷰 리포트(텔레그램 전송용 한국어 텍스트)를 생성한다."""
    ostat = compute_order_stats(orders)
    rstat = compute_review_stats(reviews)

    # 숫자 요약(코드로 확정) — 헤더
    top_menu_txt = "\n".join(
        f"   {i}. {m} ({c}건)" for i, (m, c) in enumerate(ostat["top_menus"], 1)
    ) or "   (데이터 없음)"
    header = (
        "📊 베어글스 일일 리포트\n"
        f"─────────────\n"
        f"• 주문 {ostat['order_count']}건 / 매출 {ostat['revenue']:,}원 "
        f"(건단가 {ostat['avg_order_value']:,}원)\n"
        f"• 리뷰 {rstat['review_count']}건 / 평균 ★{rstat['avg_rating']}\n"
        f"  별점분포 {rstat['distribution']}\n"
        f"• 인기 메뉴\n{top_menu_txt}\n"
        f"─────────────"
    )

    # Claude 인사이트 — 숫자와 리뷰를 함께 주고 자연어 판단
    review_lines = "\n".join(
        f"- ★{r.get('rating')} [{r.get('author')}] "
        f"{(r.get('content') or '(사진)').replace(chr(10), ' ')}"
        for r in reviews[:30]
    ) or "(리뷰 없음)"
    system = (
        "너는 베어글스 베이글 카페 사장님의 AI 비서다. 매출·리뷰 데이터를 보고 "
        "오늘 사장님이 알아야 할 것을 3~5줄로 요약한다. 한국어, 간결, 실행 "
        "가능한 조언 위주. 숫자는 주어진 값을 그대로 쓰고 새로 계산하지 마라."
    )
    user = (
        f"[매출]\n주문 {ostat['order_count']}건, 매출 {ostat['revenue']:,}원, "
        f"건단가 {ostat['avg_order_value']:,}원\n인기메뉴 {ostat['top_menus']}\n\n"
        f"[리뷰] 평균 ★{rstat['avg_rating']}, 분포 {rstat['distribution']}\n"
        f"{review_lines}\n\n"
        "오늘의 핵심 인사이트와 주의할 리뷰를 요약해줘."
    )
    try:
        insight = _ask_claude(system, user, max_tokens=1200)
    except LLMUnavailable:
        # LLM 장애 시에도 숫자 리포트는 그대로 전달(핵심 지표 손실 방지)
        neg = rstat["negatives"]
        neg_txt = ", ".join(
            f"{r.get('author')}(★{r.get('rating')})" for r in neg[:5]
        ) or "없음"
        insight = ("⚠️ AI 인사이트를 생성하지 못했습니다 "
                   "(Anthropic 크레딧/네트워크 확인 필요).\n"
                   f"저평점(★≤3) 리뷰: {neg_txt}")

    return f"{header}\n\n🤖 오늘의 인사이트\n{insight}"


# ---------------------------------------------------------------------------
# 아침 브리핑 / 저녁 리뷰 (핵심 기능)
# ---------------------------------------------------------------------------

def morning_briefing(task_texts, yesterday_orders):
    """아침 브리핑: 어제 매출 요약 + 오늘 할 일 우선순위 정리.

    task_texts: 사장님이 보낸 오늘 할 일 문자열 리스트.
    yesterday_orders: 어제 주문 리스트(DB).
    """
    ystat = compute_order_stats(yesterday_orders)
    header = (
        "☀️ 좋은 아침이에요, Matthew.\n"
        f"어제 매출: {ystat['order_count']}건 / {ystat['revenue']:,}원"
    )
    if not task_texts:
        return header + "\n\n오늘 할 일을 보내주세요. 우선순위 정리해드릴게요."

    try:
        user = (
            f"어제 매출은 {ystat['revenue']:,}원({ystat['order_count']}건). "
            f"오늘 사장님이 보낸 할 일 목록: {task_texts}\n"
            "카페 운영 관점에서 우선순위를 정해 번호 매겨 정리하고, 각 항목에 "
            "왜 그 순서인지 아주 짧게(한 구절). 마지막에 오늘 꼭 챙길 것 1개만 "
            "콕 집어줘."
        )
        body = _ask_claude(PERSONA, user, max_tokens=800)
    except LLMUnavailable:
        body = "오늘 할 일:\n" + "\n".join(
            f"{i}. {t}" for i, t in enumerate(task_texts, 1))
    return f"{header}\n\n📋 오늘 할 일\n{body}"


def evening_review(done_tasks, undone_tasks, today_orders, reviews):
    """저녁 리뷰: 오늘 완료/미완료 + 매출 분석 + 내일 챙길 것."""
    ostat = compute_order_stats(today_orders)
    rstat = compute_review_stats(reviews)
    header = (
        "🌙 오늘 마감 정리\n"
        f"매출: {ostat['order_count']}건 / {ostat['revenue']:,}원 "
        f"(건단가 {ostat['avg_order_value']:,}원)\n"
        f"할 일: 완료 {len(done_tasks)} · 미완료 {len(undone_tasks)}\n"
        f"리뷰: {rstat['review_count']}건 / ★{rstat['avg_rating']}"
    )
    done = [t.get("description") for t in done_tasks]
    undone = [t.get("description") for t in undone_tasks]
    try:
        neg = [(r.get("author"), r.get("rating")) for r in rstat["negatives"]]
        user = (
            f"오늘 매출 {ostat['revenue']:,}원({ostat['order_count']}건), "
            f"인기메뉴 {ostat['top_menus']}.\n"
            f"완료한 일: {done}\n미완료: {undone}\n"
            f"저평점 리뷰: {neg}\n"
            "오늘 하루 짧게 총평하고, 미완료 중 내일 꼭 챙길 것과 저평점 리뷰 "
            "대응을 정리해줘. 잔소리 말고 실행할 것만."
        )
        body = _ask_claude(PERSONA, user, max_tokens=900)
    except LLMUnavailable:
        undone_txt = ", ".join(undone) or "없음"
        neg_txt = ", ".join(
            f"{r.get('author')}(★{r.get('rating')})"
            for r in rstat["negatives"][:5]) or "없음"
        body = (f"내일 챙길 것(미완료): {undone_txt}\n"
                f"주의 리뷰(★≤3): {neg_txt}\n"
                "⚠️ (AI 총평은 Anthropic 크레딧 충전 후 제공)")
    return f"{header}\n\n{body}"


# 🚨 자동 답글 금지 — 사장님이 직접 대응해야 하는 민감 키워드.
# ⚠️ 음식 리뷰 오탐 주의: 바로 "고소"(savory, 고소하고/고소한/고소해)는 매우
#    흔한 칭찬이라 넣지 않는다. 법적 '고소'는 의도가 분명한 활용형만 잡는다
#    (고소하겠/고소할/고소했/고발/형사).
ESCALATION_KEYWORDS = (
    "이물질", "머리카락", "머리카", "벌레", "곰팡이", "곰팡", "식중독", "배탈",
    "토했", "구토", "설사", "알레르기", "환불", "신고", "법적", "위생",
    "고소하겠", "고소할", "고소했", "고소 접수", "고발", "형사",
    "상했", "상한", "플라스틱", "비닐", "철사", "돌이", "역겨",
)

# 사진/무텍스트 고평점용 클로징 변형 풀(복붙 느낌 방지 — 리뷰별로 다르게).
# ⚠️ 사장님 피드백: '~바라요/되셨으면/정성껏 준비하겠습니다' 같은 AI·공지
#    말투 금지. 진짜 사람이 쓰는 편한 해요체로.
_THANKS_VARIANTS = (
    "맛있게 드셨다니 저희가 다 기분 좋네요! 또 들러주세요 😊",
    "이렇게 좋게 봐주시니 힘이 나요. 담에 또 뵈어요!",
    "덕분에 오늘도 신나게 구웠어요. 또 놀러오세요~",
    "맛있게 드셨다니 진짜 다행이에요! 담에도 맛있게 구워둘게요",
    "찾아주신 것만으로 감사한데 이렇게 남겨주시고 🥹 또 오세요!",
    "좋게 봐주셔서 감사해요! 담에도 맛있게 준비할게요 🐻",
)

# 리뷰 유형별 AI 대응 지침.
_TYPE_GUIDE = {
    "praise_detail": ("리뷰에서 칭찬한 구체적 포인트(메뉴·식감·맛·서비스)를 그대로 "
                      "짚어 반응하고, 정성껏 답한다."),
    "photo_only": "사진만 남긴 고평점이다. 짧고 산뜻하게 감사. 억지로 길게 쓰지 마라.",
    "neutral": "짧은 리뷰다. 따뜻하되 간결하게 2문장 내외.",
    "question": "리뷰에 담긴 질문·요청에 실제로 답하거나 안내한다. 감사 인사는 짧게.",
    "complaint": ("서비스 리커버리 4단계로: (1)무엇이 잘못됐는지 리뷰 내용을 "
                  "구체적으로 짚어 인정 (2)변명·고객탓·배달탓 없이 진심으로 사과 "
                  "(3)구체적인 개선·재발방지 약속 (4)다시 기회를 청하거나 필요시 "
                  "고객센터/DM 안내. 절대 방어적이지 않게, 낮은 자세로."),
}


# 불만 사유 분류 — 심각 리뷰 보고에서 사유를 함께 알려주기 위함.
_COMPLAINT_REASONS = (
    ("이물질", ("이물질", "머리카락", "머리카", "벌레", "곰팡이", "곰팡",
               "플라스틱", "비닐", "철사", "돌이", "역겨")),
    ("누락/오배송", ("누락", "안왔", "안 왔", "빠졌", "덜왔", "덜 왔",
                   "안나왔", "안 나왔", "안옴", "다른 게", "다른게", "잘못")),
    ("조리", ("덜익", "덜 익", "설익", "탔", "안익", "조리", "짜요", "싱거",
             "비려", "비린", "질겨")),
    ("상태/포장", ("식었", "차갑", "눅눅", "녹아", "녹음", "포장", "젖", "쏟",
                 "뭉개", "상했", "상한", "냄새", "터졌")),
    ("배달", ("배달", "늦게", "지연", "오래", "한참")),
)


def complaint_reason(review):
    """불만 리뷰의 사유 라벨을 반환한다(내용 기반, 없으면 '기타 불만')."""
    t = review.get("content") or ""
    for label, kws in _COMPLAINT_REASONS:
        if any(k in t for k in kws):
            return label
    return "기타 불만"


def is_serious_review(review):
    """심각(불만) 리뷰인지 판별한다: 컴플레인/에스컬레이션 또는 별점 ≤3."""
    if classify_review(review) in ("complaint", "escalate"):
        return True
    r = review.get("rating")
    return r is not None and r <= 3


_PLAT_LABEL = {"baemin": "배민", "coupang": "쿠팡"}


def format_complaint_report(reviews, label=""):
    """심각 리뷰 목록을 텔레그램 보고 텍스트로 포맷한다."""
    title = f"🚨 심각 리뷰 보고{(' — ' + label) if label else ''} · {len(reviews)}건"
    lines = [title, "─────────"]
    for rv in reviews:
        esc = classify_review(rv) == "escalate"
        plat = _PLAT_LABEL.get(rv.get("platform"), rv.get("platform") or "")
        reason = "이물질/민감(직접확인)" if esc else complaint_reason(rv)
        mark = "🚨🚨" if esc else "🔸"
        body = (rv.get("content") or "(사진/무텍스트)").replace("\n", " ")[:90]
        lines.append(
            f"{mark} [{plat} ★{rv.get('rating')}] {rv.get('author')} · {reason}\n"
            f"   \"{body}\"")
    lines.append("─────────")
    lines.append("답글은 봇에서 확인 후 대응하세요.")
    return "\n".join(lines)


def classify_review(review):
    """리뷰를 대응 유형으로 분류한다.

    반환: escalate | complaint | question | praise_detail | photo_only | neutral
    """
    if any(k in (review.get("content") or "") for k in ESCALATION_KEYWORDS):
        return "escalate"
    rating = review.get("rating")
    content = (review.get("content") or "").strip()
    if rating is not None and rating <= 3:
        return "complaint"
    if not content:
        return "photo_only"
    if any(q in content for q in ("?", "되나요", "있나요", "가능", "문의", "언제")):
        return "question"
    return "praise_detail" if len(content) >= 15 else "neutral"


def _clean_author(name):
    """작성자명을 답글 호칭용으로 안전화한다.

    배민 파서 변동 등으로 날짜/숫자가 작성자로 잘못 들어오면(예: '2026년 7월
    24일') 실고객 답글에 그대로 나가면 안 되므로 '고객'으로 대체한다. 마스킹된
    실명(예: '김**', 'KIM***')은 그대로 둔다.
    """
    name = (name or "").strip()
    if not name:
        return "고객"
    if re.search(r"\d{4}\s*년|\d{4}[-.]\d{1,2}|\d{1,2}\s*월\s*\d{1,2}\s*일", name):
        return "고객"          # 날짜가 이름으로 잘못 파싱됨
    if not re.search(r"[가-힣A-Za-z]", name):
        return "고객"          # 숫자/기호뿐이면 이름 아님
    return name


def generate_review_reply(review):
    """리뷰 하나에 대한 답글 초안을 생성한다(고객에게 게시할 후보).

    - 리뷰를 유형 분류 후 유형별 지침으로 답한다.
    - 🚨 민감 리뷰(이물질·건강·환불·법적 등)는 답글을 만들지 않고 '직접 대응
      필요'를 반환한다. 절대 자동 게시 대상이 아니다.
    - 플랫폼별 글자수 한도·주문 횟수(단골)를 반영한다.

    ⚠️ '초안'만 만든다. 실제 게시는 반드시 사장님 승인을 거친다.
    """
    typ = classify_review(review)
    if typ == "escalate":
        return ("⚠️ [직접 대응 필요] 이물질·건강·환불·법적 언급 등 민감 리뷰입니다. "
                "자동 답글 대신 사장님이 직접 확인해 주세요.")

    rating = review.get("rating")
    content = (review.get("content") or "").strip()
    author = _clean_author(review.get("author"))
    menus = ", ".join(review.get("menus") or []) or "주문 메뉴"
    oc = review.get("order_count")
    cfg = PLATFORM_REPLY.get(review.get("platform"),
                             {"label": "", "max_len": 300, "target_len": 290})
    max_len = cfg["max_len"]
    target = cfg.get("target_len", max_len)
    if oc == 1:
        visit = "첫 주문 고객"
    elif isinstance(oc, int) and oc > 1:
        visit = f"{oc}번째 재주문(단골) 고객"
    else:
        visit = "고객"

    try:
        ctx = _reply_context()
        ctx_block = f"[참고 사실(백데이터)]\n{ctx}\n\n" if ctx else ""
        user = (
            f"{ctx_block}"
            f"[{cfg['label']}] {visit} '{author}'가 {menus} 주문 후 "
            f"별점 {rating}점으로 남긴 리뷰:\n"
            f"\"{content or '(사진만, 텍스트 없음)'}\"\n\n"
            f"[이 리뷰 유형 대응 지침] {_TYPE_GUIDE.get(typ, '')}\n"
            f"위 지침대로 답글을 써줘. 글자수를 넉넉히 활용해 {target}자 내외로 "
            f"정성껏 길게(최대 {max_len}자 초과 금지). 단 억지로 늘리거나 같은 말을 "
            f"반복하지 말고, 주문 메뉴·사진·경험을 구체적으로 여러 개 짚어 "
            f"진짜 내용으로 채운다. 다른 답글과 겹치지 않게 자연스럽게."
        )
        return _ask_claude(REPLY_PERSONA, user, max_tokens=600)[:max_len]
    except LLMUnavailable:
        return _template_reply(typ, review, author, oc, rating, max_len)


def _template_reply(typ, review, author, oc, rating, max_len):
    """크레딧 없을 때 템플릿 폴백 — 유형별 + 리뷰별 변형(복붙 방지)."""
    seed = str(review.get("review_no") or author)

    # 컴플레인은 감사 인사보다 '사과'가 먼저 나가야 한다.
    if typ == "complaint":
        loyal = ("늘 와주시는데 이런 일이 생겨서 더 죄송해요. "
                 if isinstance(oc, int) and oc > 1 else "")
        return (f"{author}님,\n불편을 드려서 정말 죄송해요. {loyal}"
                "말씀해주신 부분 바로 확인하고 고칠게요. 다시 한 번 오시면 "
                "제대로 보여드릴게요.")[:max_len]

    if isinstance(oc, int) and oc > 1:
        opener = f"벌써 {oc}번째네요, 알아봐주고 또 와주셔서 진짜 감사해요."
    else:
        opener = "처음 오셨는데 입맛에 맞으셨다니 너무 좋네요!"

    if typ == "question":
        body = " 물어봐주신 건 확인해서 꼼꼼히 챙길게요. 편하게 여쭤봐주셔서 감사해요!"
    else:
        body = " " + random.Random(seed).choice(_THANKS_VARIANTS)
    return f"{author}님,\n{opener}{body}"[:max_len]


if __name__ == "__main__":
    # 단독 실행: attach 세션으로 실데이터를 긁어 리포트 생성까지 시연
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    from crawler.baemin import BaeminCrawler

    with BaeminCrawler() as c:
        orders = c.fetch_orders()
        reviews = c.fetch_reviews(max_scroll=1)
    print(generate_daily_report(orders, reviews))
