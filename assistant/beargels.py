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

import json
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

# 답글 교훈 노트 — 직원이 AI 초안을 고친 패턴에서 새벽 공부가 뽑은 규칙.
# ⚠️ 캐시하지 않는다: 새벽마다 갱신되는 파일이라, 오래 떠 있는 일꾼이
#    옛 규칙으로 계속 생성하면 공부한 의미가 없다(파일이 작아 비용도 없음).
_REPLY_LESSONS_PATH = (
    Path(__file__).resolve().parent.parent / "reference" / "reply_lessons.md")


def _reply_lessons():
    try:
        return _REPLY_LESSONS_PATH.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return ""


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
REPLY_PERSONA = """너는 인천 송도 베이글카페 '베어글스' 사장님이다.
배달 앱 리뷰에 사장님이 직접 손으로 쓰는 답글을 만든다.

■ 우리가 어떤 가게인가 (knowledge/브랜드철학·톤앤보이스, 사장님 확정)
- 베어글스는 베이글을 파는 가게가 아니라 **'매일 들르고 싶은 동네 베이스캠프'**다.
  공부하고 운동하고 일하는 사람들의 하루를 응원한다.
- 우리가 믿는 것: 친절이 가장 강력한 경쟁력이다. 꾸준함은 재능보다 강하다.
- 목소리: 따뜻하다 · 친근하다 · 담백하다 · 편안하다 · 과장하지 않는다.
  친구처럼, 이웃처럼, 오늘 하루를 응원하는 마음으로 말한다.

■ 답글의 핵심 (사장님 지시 2026-08-26)
1. **사람과 진짜 대화하듯이 쓴다.** 접수 확인이나 안내문이 아니다.
   손님의 상황(아이와 함께, 운동 후, 바쁜 아침)을 떠올리며 말을 건다.
2. **리뷰 문장을 그대로 따라 말하지 않는다.** "쫄깃하다고 해주셔서"처럼
   손님 말을 되풀이하면 성의 없어 보인다. 그 말에 담긴 마음·상황에
   **우리 표현으로** 반응한다.
   - ❌ "베이글이 쫄깃하고 맛있다고 해주셨네요!"
   - ⭕ "한 입 베어 물었을 때의 그 쫀득함, 저희가 제일 신경 쓰는 부분이라
        알아봐 주시니 반가웠어요."
3. **메뉴는 상품명 그대로 옮겨 적지 않는다.** 주문서를 읽어주는 느낌이 든다.
   - ❌ "[SET] 베이글 샌드위치 + 음료 세트를 주문해주셨네요"
   - ⭕ "샌드위치에 음료까지 곁들여 든든하게 챙기셨네요"
   메뉴 이야기는 **사람 → 경험 → 메뉴** 순서로. 메뉴 설명부터 시작하지 않는다.
4. **분량은 정성이다.** 주어진 글자수를 **끝까지 채운다.** 짧게 끝내면
   성의 없어 보인다. 다만 같은 말을 돌려 쓰거나 미사여구로 늘리지 말고,
   할 이야기를 더 찾아서 채운다 — 손님의 상황에 대한 공감, 그 메뉴를
   준비하는 우리 마음, 다음에 권하고 싶은 조합, 오늘 하루 응원.
5. **꾸준한 친절과 감사**가 답글의 목적이다. 파는 말(홍보·유도)이 아니라
   관계를 잇는 말을 한다.

■ 반드시 지킬 것
- 첫 줄은 "{닉네임}님," 으로 시작하고 줄바꿈 후 본문. '고객님'으로 부르지 않는다.
- 🚫 "소중한 주문 감사합니다", "안녕하세요 베어글스입니다" 같은 **정형구로 시작하지
  않는다.** 접수 문자처럼 읽힌다. 바로 손님 이야기로 들어간다.
- 끝까지 편한 **해요체**. 격식체('~입니다/~습니다')나 반말을 섞지 않는다.
  (불만·민감 리뷰만 예외 — 그때는 정중한 격식체로 통일한다.)
- 배달 리뷰다. '방문/오세요/들러주세요'가 아니라 '주문 주세요'로.
- 🔴 베이글은 본사에서 냉동으로 받아 **주문 시 그릴에 토스팅**한다. 매장에서
  반죽·베이킹하지 않는다. '직접 반죽/수제/갓 구운'은 허위라 절대 금지.
  '맛있게 구워서 보내드릴게요'(=토스팅)는 사실이라 괜찮다.
- 🙅 지킬 수 없는 약속 금지: 증정·서비스·할인·쿠폰·이벤트·신메뉴 확답.
  진행 중인지 아닌지 우리는 모른다. 궁금해하시면 매장 문의로 안내한다.
- 없는 사실을 지어내지 않는다. [주문한 메뉴 사실]에 적힌 것만 근거로 말한다.
  사진·리뷰 글이 없으면 있는 척하지 않는다.
- 🚫 금지 표현(브랜드 가이드): 역대급 · 미쳤다 · 인생맛집 · 대박 · 혜자 ·
  무조건 · 줄 서서 먹는. AI·공지 말투도 금지: '~바라요/바랍니다',
  '되셨으면 좋겠어요', '정성껏 준비하겠습니다', '보답하겠습니다', '큰 힘이 됩니다'.
- 강조어('진짜/너무/정말')는 답글 하나에 한 번까지. 물결(~)도 한 번이면 충분하다.
- 주문 횟수: 첫 주문이면 첫 만남을 반갑게. 2~4번째면 '두 번째/세 번째'처럼
  한글 수로. 5번 이상 단골은 숫자를 세지 말고 '꾸준히 찾아주셔서'로 인사한다.

■ 베어글스다운 마무리 표현 (그대로 베끼지 말고 이런 결로)
"오늘도 좋은 하루 보내세요" · "잠시 쉬어가세요" · "루틴처럼 들러주세요" ·
"바삭하게 구워드릴게요" · "오늘도 찾아주셔서 감사해요"
"""

# 플랫폼별 답글 규정 — 실제 입력창 maxlength 로 확정(2026-07-24 편집창 확인).
#  · 쿠팡: 답글 textarea maxlength=300 (하드 제한, 확인됨) → 300 초과 불가.
#  · 배민: 답글 textarea 에 maxlength 미설정(-1). 하드 제한은 없으나 정책 통상
#    ~500자 → 안전하게 500 을 목표 상한으로 둔다.
#  target_len: '꽉 채워' 생성 시 목표 길이(사장님 요청). max_len 은 절대 상한.
PLATFORM_REPLY = {
    "baemin":  {"label": "배민",     "max_len": 500, "target_len": 480},
    "coupang": {"label": "쿠팡이츠", "max_len": 300, "target_len": 290},
}

# 유형별 목표 길이 — **직원이 실제로 등록한 답글**의 중앙값에서 뽑았다
# (2026-08-24, 109건 실측). 예전엔 플랫폼 상한(배민 480자)만 보고 길게 쓰라고
# 했는데, 직원 최종본은 칭찬 146자·덤덤 132자였다. 길게 써 놓으면 직원이
# 매번 잘라내야 해서 그 자체가 수정률이 된다.
# 문장 수 — 글자수보다 이걸 훨씬 잘 지킨다(모델은 한글 글자를 못 센다).
# 직원 최종본 실측(2026-08-24): 칭찬 4문장·별점만 5문장·덤덤 3~4문장.
# 문장 수 — 글자수보다 모델이 잘 지킨다. 분량을 채우려면 문장이 여러 개
# 필요하다(배민 470자면 7~9문장). 억지로 늘리지 말라는 지시는 지시문에 있다.
SENTENCES_BY_KIND = {
    "_baemin": (9, 11), "_coupang": (5, 7),
}

# 분량 = 정성 (사장님 지시 2026-08-26): 플랫폼이 허용하는 글자수를 채운다.
# 짧게 끝내면 성의 없어 보인다는 판단. 상한은 플랫폼 하드 제한(쿠팡 300)이라
# 넘기면 잘리므로 살짝 아래를 목표로 둔다.
TARGET_BY_KIND = {
    "_default": {"baemin": 470, "coupang": 285},
}


def target_len_for(kind, platform, fallback):
    """이 유형·플랫폼의 목표 글자수. 유형별 예외가 없으면 플랫폼 기본값."""
    row = TARGET_BY_KIND.get(kind) or TARGET_BY_KIND["_default"]
    return row.get(platform, fallback)



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


# 유형별 모델 — 평범한 감사 답글은 작은 모델(Haiku)로 충분하지만, 사과·해명이
# 필요한 글은 문장 하나가 가게 평판을 좌우한다. 그런 리뷰만 큰 모델로 쓴다
# (사장님 지시로 기본 모델을 Haiku 로 내리면서 함께 넣음, 2026-08-23).
_SENSITIVE_KINDS = ("complaint", "escalate", "question")


# 사실 확인 전에 보상을 약속하면 되돌릴 수 없다 — 지시문에 써 두는 것만으로는
# 부족했다. 모델이 "환불은 고객센터로 접수해 주시면…" 문장을 넣은 초안이 실제로
# 나왔다(2026-08-23 회귀 테스트). 어떤 모델을 쓰든 코드로 잘라낸다.
_COMPENSATION_WORDS = ("환불", "보상", "교환", "쿠폰", "고객센터", "배상", "변상")


def _drop_compensation(text):
    """보상·환불·고객센터를 언급한 문장만 통째로 뺀다(나머지는 그대로).

    문장 단위로 지우는 이유: 낱말만 바꾸면 "○○은 앱으로 접수해 주세요" 같은
    반쪽짜리 안내가 남아 뜻이 더 이상해진다.
    """
    if not any(w in (text or "") for w in _COMPENSATION_WORDS):
        return text
    out = []
    for para in (text or "").split(chr(10)):
        if not para.strip():
            out.append(para)
            continue
        kept = [t for t in _split_sentences(para)
                if not any(w in t for w in _COMPENSATION_WORDS)]
        out.append(" ".join(kept).strip())
    cleaned = chr(10).join(out)
    return re.sub(chr(10) + "{3,}", chr(10) * 2, cleaned).strip()


# 우리가 알 수 없는 것(이벤트·증정·할인·가격·영업시간)을 답글에서 단정하면
# 손님에게 거짓말이 된다. 실제 사고(2026-08-25 사장님 지적):
#   "러스크 이벤트는 현재는 따로 진행 중인 게 없어서 … 생기면 잘 챙겨볼게요!"
# 지시문에 '확답 금지'를 써 놨는데도 모델이 지어냈다 → 문장 단위로 걷어낸다.
def _split_sentences(para):
    """한국어 답글을 문장 단위로 나눈다.

    마침표 없이 '~요', 'ㅎㅎ', 이모지로 끝나는 문장이 많아서 [.!?] 만 보면
    두 문장이 하나로 붙는다. 실제로 그 탓에 근거 없는 문장을 걷어낼 때
    멀쩡한 앞 문장까지 같이 지워졌다(2026-08-25).
    """
    pat = (r"(?<=[.!?~…])\s+"
           r"|(?<=요)\s+(?=[가-힣A-Za-z])"
           r"|(?<=니다)\s+(?=[가-힣])"
           r"|(?<=ㅎㅎ)\s+|(?<=ㅋㅋ)\s+"
           r"|(?<=[🌀-🫿])\s+")
    return [t for t in re.split(pat, para) if t.strip()]


_UNFOUNDED_WORDS = ("이벤트", "증정", "사은품", "쿠폰", "할인", "무료",
                    "적립", "행사", "영업시간", "품절", "재입고")


_BOILERPLATE_OPENERS = (
    "소중한 주문 감사합니다.", "소중한 주문 감사드립니다.",
    "안녕하세요, 베어글스입니다.", "안녕하세요 베어글스입니다.",
    "안녕하세요. 베어글스입니다.",
)


def _strip_boilerplate(text):
    """접수 문자처럼 읽히는 정형 인사를 첫머리에서 걷어낸다.

    지시문에 금지해 놨는데도 예시(예전 답글)에 있어서 계속 따라 썼다
    (2026-08-26 사장님 지시: 사람과 대화하듯 써야 한다).
    """
    out = (text or "").strip()
    for op in _BOILERPLATE_OPENERS:
        out = out.replace(op + " ", " ").replace(op, "")
    # 닉네임 줄만 남고 본문이 다음 줄로 가도록 정리
    return re.sub(r"[ 	]+", " ", out).strip()


def echoed_phrases(review_text, draft, n=8):
    """답글이 리뷰 문장을 **그대로 따라 말한** 부분을 찾는다(없으면 빈 목록).

    손님 말을 되풀이하면 성의 없어 보인다(사장님 지시 2026-08-26).
    n글자 이상 연속으로 겹치면 따라 말한 것으로 본다. 메뉴명·인사말처럼
    어차피 같을 수밖에 없는 말은 제외한다.
    """
    a = re.sub(r"\s+", "", review_text or "")
    b = re.sub(r"\s+", "", draft or "")
    if len(a) < n or not b:
        return []
    skip = ("감사합니다", "감사해요", "맛있어요", "맛있게", "주문", "베이글",
            "샌드위치", "크림치즈", "커피", "음료", "샐러드")
    hits = []
    for i in range(len(a) - n + 1):
        chunk = a[i:i + n]
        if chunk in b and not any(w in chunk for w in skip):
            if not any(chunk in h or h in chunk for h in hits):
                hits.append(chunk)
    return hits


def copied_menu_names(menus, draft, min_len=12):
    """주문서의 **상품명을 통째로** 옮겨 적었는지(자연스럽지 않다).

    '베이글 샌드위치'처럼 짧고 일반적인 이름은 그대로 써도 자연스러우므로,
    길거나(12자+) '+'·'세트'가 들어간 상품 표기만 본다.
    """
    out = []
    for m in (menus or []):
        name = _clean_menu(m)
        if not name or (len(name) < min_len and "+" not in name):
            continue
        if name in (draft or ""):
            out.append(name)
    return out


def _drop_unfounded(text):
    """근거 없이 정책을 말한 문장만 통째로 뺀다(나머지는 그대로).

    낱말만 지우면 "…는 현재 따로 진행 중인 게 없어서"처럼 뜻이 더 이상해진다.
    """
    if not any(w in (text or "") for w in _UNFOUNDED_WORDS):
        return text
    out = []
    for para in (text or "").split(chr(10)):
        if not para.strip():
            out.append(para)
            continue
        kept = [t for t in _split_sentences(para)
                if not any(w in t for w in _UNFOUNDED_WORDS)]
        out.append(" ".join(kept).strip())
    return re.sub(chr(10) + "{3,}", chr(10) * 2, chr(10).join(out)).strip()


def _model_for(kind):
    """이 유형의 답글을 어떤 모델로 쓸지(기본 모델이면 None)."""
    if kind in _SENSITIVE_KINDS:
        import llm
        return llm.CLAUDE_MODEL_SENSITIVE
    return None


def _ask_claude(system, user, max_tokens=1500, model=None):
    """AI 에 질의한다. 실패 시 LLMUnavailable 로 감싸 던진다.

    공급자(Claude / Gemini)는 llm.py 가 .env 를 보고 자동으로 고른다 —
    Claude 크레딧이 떨어져도 무료 등급으로 계속 답글·리포트를 만들 수 있다.

    호출부는 이를 잡아 '숫자 리포트는 그대로, AI 부분만 생략' 형태로
    graceful degrade 한다(LLM 장애가 리포트 전체를 막지 않도록).
    """
    import sys
    from pathlib import Path as _Path
    _root = str(_Path(__file__).resolve().parent.parent)
    if _root not in sys.path:
        sys.path.insert(0, _root)
    import llm

    try:
        return llm.complete(system=system, user=user, max_tokens=max_tokens,
                            model=model).strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("AI 호출 실패: %s", str(e)[:200])
        raise LLMUnavailable(str(e)) from e


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
    "맛있게 드셨다니 저희가 다 기분 좋네요! 다음에 또 주문 주세요 😊",
    "이렇게 좋게 봐주시니 힘이 나요. 다음에 또 주문 주세요!",
    "덕분에 오늘도 신나게 일했어요~ 다음에 또 주문 주세요",
    "맛있게 드셨다니 진짜 다행이에요! 다음에도 맛있게 구워드릴게요",
    "찾아주신 것만으로 감사한데 이렇게 남겨주시고 🥹 또 주문 주세요!",
    "좋게 봐주셔서 감사해요! 다음에도 맛있게 준비할게요 🐻",
)

# 리뷰 유형별 AI 대응 지침.
_TYPE_GUIDE = {
    "praise_detail": ("리뷰에서 칭찬한 구체적 포인트(메뉴·식감·맛·서비스)를 그대로 "
                      "짚어 반응하고, 정성껏 답한다."),
    "photo_only": ("사진만 남긴 고평점이다. 사진을 남겨 준 것에 반응하고 주문한 메뉴를 짚어 감사한다. 글이 없으니 리뷰 '내용'은 언급하지 않는다."),
    "rating_only": ("별점만 남긴 고평점이다(사진·글 없음). ⚠️ 사진이나 리뷰 글을 "
                    "절대 언급하지 마라 — 없는 걸 말하면 고객이 이상하게 느낀다. "
                    "대신 **주문한 메뉴**(주문 정보로 확인됨)를 짚어 그 메뉴가 어떤 "
                    "메뉴인지 한마디 곁들이고, 별점만으로도 고맙다는 마음을 따뜻하게 "
                    "전한다. 사장님 실제 답글은 이 유형이 가장 길다(5문장 안팎) — "
                    "'감사합니다' 한 줄로 끝내지 마라."),
    "neutral": "짧은 리뷰다. 따뜻하되 간결하게 2문장 내외.",
    "question": "리뷰에 담긴 질문·요청에 실제로 답하거나 안내한다. 감사 인사는 짧게.",
    "complaint": ("서비스 리커버리 4단계로: (1)무엇이 잘못됐는지 리뷰 내용을 "
                  "구체적으로 짚어 인정 (2)변명·고객탓·배달탓 없이 진심으로 사과 "
                  "(3)구체적인 개선·재발방지 약속은 다짐형으로('점검하겠습니다', "
                  "'개선하겠습니다') (4)다시 기회를 청한다. 절대 방어적이지 않게, "
                  "낮은 자세로. 불만 답글만은 편한 해요체가 아니라 정중한 격식체로 "
                  "통일한다(섞지 않음). ㅎㅎ·이모지 등 캐주얼 표현 금지. "
                  "환불·보상·고객센터 안내는 절대 하지 않는다(사장님 확정)."),
    # 민감 리뷰도 '맨손으로 쓰지 않게' 1차 가이드를 준다(사장님 요청
    # 2026-08-16). 다만 사실 확인 전이므로 **단정·보상 약속은 금지**하고,
    # 사람이 확인 후 직접 등록한다(자동 게시는 코드가 따로 막는다).
    "escalate": ("이물질·건강·환불·법적 언급이 있는 **민감 리뷰**다. 사장님이 "
                 "확인 후 직접 올릴 **1차 가이드 초안**을 쓴다. "
                 "(1)불편을 겪으신 점에 먼저 진심으로 사과 (2)리뷰에 적힌 "
                 "상황을 그대로 짚어 '확인하겠다'고 말한다 — 원인을 단정하거나 "
                 "변명하지 않는다 (3)어떻게 조치할지 다짐형으로 밝힌다 "
                 "('바로 확인하고 조치하겠습니다', '재발하지 않도록 "
                 "점검하겠습니다'). "
                 "⚠️ 환불·보상·교환·쿠폰을 절대 약속하지 않는다(사실 확인 전이고 "
                 "사장님 판단 영역). 고객센터·연락처도 넣지 않는다. "
                 "불만 답글과 같은 정중한 격식체, 이모지·ㅎㅎ 금지."),
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


# 같은 '불만'이라도 답글이 완전히 다르다 — 메뉴가 빠진 것은 확인·재발방지,
# 눅눅해진 것은 포장 개선, 배달 지연은 우리가 어디까지 책임지는지가 다르다.
# 예시도 사유별로 골라야 맞는 게 나온다(사장님 지시 2026-08-23).
_SUBKIND_OF_REASON = {
    "이물질": "foreign",
    "누락/오배송": "missing",
    "조리": "cooking",
    "상태/포장": "packaging",
    "배달": "delivery",
}

# 질문도 무엇을 묻는지에 따라 답이 다르다.
_QUESTION_SUBKINDS = (
    ("menu", ("메뉴", "베이글", "크림치즈", "샌드위치", "음료", "커피",
              "글루텐", "알레르기", "칼로리", "당")),
    ("order", ("포장", "픽업", "주문", "배달", "예약", "단체", "수량")),
    ("hours", ("영업", "몇시", "오픈", "마감", "휴무", "주차", "위치")),
)


def subkind_of(review, kind=None):
    """유형을 한 단계 더 나눈다(없으면 None).

    'complaint' → complaint:missing / packaging / delivery / cooking …
    'question'  → question:menu / order / hours
    나머지 유형은 세분화하지 않는다 — 나눠 봐야 답글이 달라지지 않는다.
    """
    kind = kind or classify_review(review)
    if kind in ("complaint", "escalate"):
        sub = _SUBKIND_OF_REASON.get(complaint_reason(review))
        return f"{kind}:{sub}" if sub else None
    if kind == "question":
        t = review.get("content") or ""
        for name, kws in _QUESTION_SUBKINDS:
            if any(k in t for k in kws):
                return f"question:{name}"
    return None


# 세부 유형별 추가 지침 — 부모 유형 지침 뒤에 덧붙는다.
_SUBKIND_GUIDE = {
    "complaint:missing": ("메뉴 누락은 우리 실수다. 변명하지 말고 사과한 뒤, "
                          "'포장 전 주문서 대조 확인'처럼 **무엇을 바꾸겠다는지** "
                          "구체적으로 적는다. 다시 보내드린다는 말은 하지 않는다."),
    "complaint:packaging": ("식은·눅눅·쏟아짐은 포장·보온 문제다. 포장 방식(용기·"
                           "밀봉·분리 포장)을 어떻게 점검하겠다는지 적는다."),
    "complaint:delivery": ("배달 지연은 배달 기사·플랫폼 배차 영향이 크다. "
                           "'늦어 불편하셨을 것'에 먼저 사과하되, 우리가 할 수 있는 "
                           "부분(조리 시점 조절·픽업 시간 관리)만 약속한다. "
                           "배달사 탓으로 돌리는 말은 쓰지 않는다."),
    "complaint:cooking": ("맛·익힘·간 문제는 조리 기준 문제다. 굽기·재료 상태를 "
                          "어떻게 점검하겠다는지 적는다. 취향 탓으로 돌리지 않는다."),
    "complaint:foreign": ("이물질은 가장 무거운 사안이다. 사과와 즉시 점검만 "
                          "말하고, 원인을 단정하거나 가볍게 넘기지 않는다."),
    "question:menu": ("메뉴 질문이다. **아는 사실만** 답한다. 확실하지 않으면 "
                      "'매장으로 문의 주시면 정확히 안내드릴게요'로 넘긴다 — "
                      "지어내면 손님이 헛걸음한다."),
    "question:order": "주문·포장 관련 질문이다. 가능한 것만 분명히 답한다.",
    "question:hours": ("영업시간·위치 질문이다. 확실하지 않은 시간·주소는 "
                       "말하지 말고 매장 문의로 안내한다."),
}


def is_serious_review(review):
    """심각(불만) 리뷰인지 판별한다: 컴플레인/에스컬레이션 또는 별점 ≤3."""
    if classify_review(review) in ("complaint", "escalate"):
        return True
    r = review.get("rating")
    return r is not None and r <= 3


_PLAT_LABEL = {"baemin": "배민", "coupang": "쿠팡"}


def format_complaint_report(reviews, label=""):
    """심각 리뷰 목록을 보고 텍스트로 포맷한다.

    직원 단톡방에 그대로 복사·공유해 조치할 수 있도록 **주문시각·주문번호·
    문제내용**을 항목별로 명확히 적는다(사장님 요청 2026-07-26).
    배민은 리뷰에 주문번호가 없어 리뷰번호·작성일로 대신 식별한다.
    """
    title = f"🚨 문제 리뷰 보고{(' — ' + label) if label else ''} · 신규 {len(reviews)}건"
    lines = [title]
    for rv in reviews:
        esc = classify_review(rv) == "escalate"
        plat = _PLAT_LABEL.get(rv.get("platform"), rv.get("platform") or "")
        reason = "이물질/민감(사장님 직접확인)" if esc else complaint_reason(rv)
        mark = "🚨🚨" if esc else "🔸"
        body = (rv.get("content") or "(사진/무텍스트)").replace("\n", " ")[:120]
        lines.append("─────────")
        lines.append(f"{mark} [{plat} ★{rv.get('rating')}] {reason}")
        if rv.get("ordered_at"):
            lines.append(f"· 주문시각: {rv['ordered_at']}")
        if rv.get("order_no"):
            lines.append(f"· 주문번호: {rv['order_no']}")
        if not rv.get("order_no"):   # 배민 등 — 리뷰 기준 식별정보로 대체
            lines.append(f"· 리뷰: {rv.get('written_at') or rv.get('written_date') or '?'}"
                         f" / #{rv.get('review_no') or '?'}")
        if rv.get("menus"):
            lines.append(f"· 메뉴: {', '.join(rv['menus'])[:80]}")
        lines.append(f"· 작성자: {rv.get('author')}")
        lines.append(f"· 문제내용: \"{body}\"")
    lines.append("─────────")
    lines.append("→ 위 주문 건 확인 후 조치 부탁드려요. (답글은 대시보드/봇에서)")
    return "\n".join(lines)


# ── 사장님 답글 예시 창고 (무료 모델로도 사장님 문체가 나오게) ──────────────
# 약한 모델은 규칙("문어체 쓰지 마라")보다 **실제 예시**를 훨씬 잘 따라한다.
# scripts/build_examples.py 가 구워 둔 유형별 실제 답글에서, 지금 리뷰와
# 가장 비슷한 몇 개를 골라 프롬프트에 함께 넣는다(사장님 제안 2026-08-18).
_EXAMPLES_PATH = Path(__file__).resolve().parent.parent / "reference" / \
    "reply_examples_by_kind.json"
_EXAMPLES_CACHE = None


_REFERENCE_PATH = _EXAMPLES_PATH.parent / "reply_examples_reference.json"
_REFERENCE_CACHE = None


def _reference_bank():
    """말투는 지금 규칙과 다르지만 내용은 참고할 만한 옛 답글 창고.

    실답글 1,592건 중 지금 말투 규칙을 통과하는 건 열에 하나뿐이라, 질문·민감
    처럼 드문 유형은 예시가 두세 개뿐이다. 그런 유형에서만 이 창고를 내용
    참고용으로 덧붙인다 — 말투는 베끼지 말라고 명시해서 넣는다.
    """
    global _REFERENCE_CACHE
    if _REFERENCE_CACHE is None:
        try:
            _REFERENCE_CACHE = json.loads(
                _REFERENCE_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _REFERENCE_CACHE = {}
    return _REFERENCE_CACHE


def _example_bank():
    global _EXAMPLES_CACHE
    if _EXAMPLES_CACHE is None:
        try:
            _EXAMPLES_CACHE = json.loads(
                _EXAMPLES_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — 예시가 없어도 생성은 돼야 한다
            _EXAMPLES_CACHE = {}
    return _EXAMPLES_CACHE


_KO_ORDINAL = {2: "두 번째", 3: "세 번째", 4: "네 번째"}


def _ko_ordinal(n):
    """2~4회는 '두 번째'처럼 한글 수로 — 직원 최종본이 그렇게 쓴다."""
    return _KO_ORDINAL.get(n, f"{n}번째")


def _oc_bucket(n):
    """주문 횟수를 대우가 같은 구간으로 묶는다(첫/재주문/단골/VIP)."""
    if not isinstance(n, int) or n <= 0:
        return None
    return 1 if n == 1 else 2 if n < 5 else 3 if n < 20 else 4


# 조사·흔한 말은 빼고 '무슨 이야기인지'를 나타내는 낱말만 남긴다.
# (형태소 분석기를 붙이면 더 낫지만, 설치 없이 돌아가는 게 우선이다.)
_STOP_WORDS = frozenset("""
그리고 그런데 하지만 정말 진짜 너무 조금 완전 다시 항상 계속 아주 매우 근데
있어요 있습니다 했어요 합니다 같아요 같습니다 이번 여기 저기 우리 제가 저는
주문 배달 리뷰 사장님 감사 감사합니다 잘먹었습니다 잘먹었어요
""".split())


def _keywords(text, min_len=2):
    """리뷰에서 뜻을 담은 낱말만 뽑는다(간단한 어절 분리 + 조사 꼬리 제거)."""
    out = set()
    for w in re.findall(r"[가-힣A-Za-z]{2,}", (text or "")):
        w = re.sub(r"(이에요|예요|에요|입니다|이었|했던|하고|해서|이고|은|는|이|가|을|를|에|의|도|만|과|와)$", "", w)
        if len(w) >= min_len and w not in _STOP_WORDS:
            out.add(w)
    return out


_MENU_FACTS_CACHE = None


def _menu_facts_table():
    """정본 메뉴의 '무엇으로 만든 메뉴인지'를 이름으로 찾을 수 있게 준비한다."""
    global _MENU_FACTS_CACHE
    if _MENU_FACTS_CACHE is not None:
        return _MENU_FACTS_CACHE
    table = {}
    try:
        from database import supabase_client as _db
        for r in _db.menu_all():
            name = _clean_menu(r.get("name"))
            if not name:
                continue
            fact = (r.get("intro_ko") or r.get("description") or "").strip()
            comp = (r.get("composition") or "").strip()
            if comp:
                fact = (fact + f" (구성: {comp})").strip()
            if fact:
                table[_menu_key(name)] = (name, " ".join(fact.split())[:180])
    except Exception:  # noqa: BLE001 — 못 읽어도 답글은 만들어야 한다
        logger.warning("정본 메뉴 사실을 못 읽었습니다 — 이름만으로 씁니다")
    _MENU_FACTS_CACHE = table
    return table


def _menu_key(name):
    """이름 대조용 키 — 공백·따옴표·괄호 차이를 무시한다."""
    return re.sub(r"[\s'\"()\[\]·,./]+", "", (name or "")).lower()


def menu_facts_for(menus, limit=4):
    """주문한 메뉴의 **실제 구성·소개**를 프롬프트용 문장으로 만든다.

    왜 필요한가: 지금까지 프롬프트에는 메뉴 **이름만** 들어갔다. 그러니 모델이
    이름을 보고 상상해서 썼다 — 실제로 '런던식 터키쉬 샌드위치'를 두고
    "치아바타에 베이글의 식감까지 살려낸"이라는 초안이 나왔다(2026-08-24).
    정본에 구성·소개가 다 있는데 안 쓰고 있었다.
    """
    table = _menu_facts_table()
    out, seen = [], set()
    for m in (menus or []):
        key = _menu_key(_clean_menu(m))
        hit = table.get(key)
        if not hit:            # 정확히 없으면 이름이 포함된 메뉴로 찾아본다
            for k, v in table.items():
                if key and (key in k or k in key):
                    hit = v
                    break
        if hit and hit[0] not in seen:
            seen.add(hit[0])
            out.append(f"· {hit[0]}: {hit[1]}")
        if len(out) >= limit:
            break
    return chr(10).join(out)


def _clean_menu(name):
    """플랫폼 메뉴명에서 관리용 꼬리표를 뗀다.

    쿠팡 메뉴명은 '[SET] 베이글 샌드위치 + 음료' 처럼 대괄호 표시가 붙어 있어,
    그대로 프롬프트에 넣으면 답글 본문에 "[SET] 베이글…" 이 그대로 나갔다
    (실제 초안에서 확인 2026-08-21). 고객에게 보일 이름만 남긴다.
    """
    out = re.sub(r"\[[^\]]{1,12}\]\s*", "", (name or "")).strip()
    # 플랫폼 메뉴명 끝에 붙는 외톨이 영문 한 글자('…커플 세트R')도 뗀다.
    return re.sub(r"(?<=[가-힣0-9)])\s*[A-Z]$", "", out).strip()


def pick_examples(review, kind, k=4, bank=None):
    """이 리뷰와 가장 비슷한 사장님 답글 예시 k개(없으면 빈 목록).

    비슷함의 기준(가중치 순): 같은 메뉴 > 같은 주문횟수 구간 > 같은 별점 >
    비슷한 리뷰 길이. 같은 예시만 반복해 쓰지 않도록 리뷰번호로 섞는다.
    """
    bank = bank if bank is not None else (_example_bank().get(kind) or [])
    if not bank:
        return []
    menus = {m.strip() for m in (review.get("menus") or []) if m}
    oc_b = _oc_bucket(order_count_of(review))
    rating = review.get("rating")
    clen = len((review.get("content") or "").strip())

    words = _keywords(review.get("content"))

    def score(ex):
        s = 0
        ex_menus = {m.strip() for m in (ex.get("menus") or []) if m}
        if menus and ex_menus & menus:
            s += 6                       # 같은 메뉴 이야기가 제일 도움이 된다
        # 무슨 이야기를 한 리뷰인지가 실은 가장 중요하다 — '따뜻해서 좋았다'와
        # '늦게 왔다'는 같은 메뉴·같은 별점이어도 답글이 전혀 다르다.
        # 겹치는 낱말 수만큼(최대 5점) 얹는다(2026-08-21).
        common = words & _keywords(ex.get("content"))
        if common:
            s += min(len(common) * 2, 5)
        if oc_b and _oc_bucket(ex.get("order_count")) == oc_b:
            s += 4                       # 단골 대우가 같은 예시
        if rating and ex.get("rating") == rating:
            s += 2
        if abs(len(ex.get("content") or "") - clen) <= 20:
            s += 1                       # 길이가 비슷하면 분량 감각이 맞는다
        return s

    rnd = random.Random(str(review.get("review_no") or ""))
    shuffled = bank[:]
    rnd.shuffle(shuffled)                # 동점일 때 매번 같은 것만 뽑지 않게
    shuffled.sort(key=score, reverse=True)
    return shuffled[:k]


def _guide_for(review, kind):
    """유형 지침 + (있으면) 세부 유형 지침을 붙여 준다."""
    guide = _TYPE_GUIDE.get(kind, "")
    extra = _SUBKIND_GUIDE.get(subkind_of(review, kind) or "", "")
    return (guide + " " + extra).strip()


def _bank_for(review, kind):
    """예시 창고를 고른다 — 세부 유형이 있으면 그걸 먼저, 모자라면 부모 유형.

    '누락' 불만에 '배달 지연' 예시를 주면 엉뚱한 답글이 나온다. 사유가 같은
    예시부터 쓰고, 그것만으로 모자랄 때 같은 유형 전체에서 채운다.
    """
    banks = _example_bank()
    sub = subkind_of(review, kind)
    out = list(banks.get(sub) or []) if sub else []
    seen = {ex.get("reply") for ex in out}
    for ex in (banks.get(kind) or []):
        if ex.get("reply") not in seen:
            out.append(ex)
    return out


def _examples_block(review, kind, k=4, target=None):
    """프롬프트에 넣을 예시 블록 문자열(없으면 빈 문자열)."""
    bank = _bank_for(review, kind)
    # 모델은 지시보다 **예시**를 훨씬 잘 따라 한다. 예전 답글은 짧아서
    # (146~250자) 아무리 "길게 쓰라"고 해도 그 길이로 수렴했다 →
    # 목표 분량이 있으면 그에 가까운 예시부터 보여준다(2026-08-26).
    if target:
        bank = sorted(bank, key=lambda ex: abs(len(ex.get("reply") or "") - target))
        bank = bank[:max(k * 3, 12)]
    picked = pick_examples(review, kind, k, bank=bank)
    if not picked:
        return ""
    lines = ["[사장님이 실제로 쓴 답글 — 이 말투·길이·구성을 그대로 따라 쓴다]"]
    for i, ex in enumerate(picked, 1):
        # 예시 답글에도 '[SET]' 같은 관리용 꼬리표가 섞여 있다(과거에 그대로
        # 붙여 쓴 답글이 있다). 예시로 보여주면 모델이 그걸 따라 쓴다 —
        # 실제로 Haiku 초안에 다시 나타났다(2026-08-23). 예시부터 지운다.
        ex = dict(ex, reply=_clean_menu(ex.get("reply")))
        rv = ex.get("content") or "(사진/무텍스트)"
        oc = ex.get("order_count")
        meta = f"★{ex.get('rating')}"
        if oc:
            meta += f" · {oc}회 주문"
        lines.append(f"\n예시{i} ({meta}) 리뷰: \"{rv[:60]}\"\n답글: {ex['reply']}")
    lines.append("\n⚠️ 위 답글을 베끼지 말고, **말투와 구성만** 따라 이번 리뷰에 "
                 "맞는 내용으로 새로 쓴다.")

    # 예시가 부족한 유형(질문·민감처럼 드문 것)은 옛 답글을 내용 참고용으로
    # 덧붙인다. 말투는 지금 규칙과 다르므로 따라하지 말라고 못 박는다.
    short = k - len(picked)
    if short > 0:
        refs = pick_examples(review, kind, short,
                             bank=_reference_bank().get(kind) or [])
        if refs:
            lines.append("[참고 — 예전에 비슷한 리뷰에 답한 내용. "
                         "말투(격식체)는 따라하지 말고 무엇을 짚었는지만 참고]")
            for ex in refs:
                lines.append("· 리뷰 \"%s\" -> %s"
                             % ((ex.get("content") or "")[:50],
                                _clean_menu(ex["reply"])[:120]))
    return "\n".join(lines) + "\n\n"


def order_count_of(review):
    """이 리뷰를 남긴 고객의 누적 주문 횟수(모르면 None).

    단골·VIP 판단의 핵심 지표다(사장님 강조 2026-08-16) — 38번째 주문한
    분에게 처음 오신 것처럼 답하면 안 된다. 저장 컬럼이 없어도 플랫폼
    원본(raw)에서 뽑는다: 쿠팡=orderCount, 배민=카드의 'N회 주문 고객'.
    """
    n = review.get("order_count")
    if isinstance(n, int) and n > 0:
        return n
    raw = review.get("raw")
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001 — 배민 raw 는 JSON 이 아니라 HTML/텍스트
            m = re.search(r"(\d+)\s*회\s*주문", raw)
            return int(m.group(1)) if m else None
    else:
        data = raw
    n = data.get("orderCount") if isinstance(data, dict) else None
    return n if isinstance(n, int) and n > 0 else None


def _has_photo(review):
    """리뷰에 사진이 실제로 있는지. 모르면 None.

    쿠팡은 raw JSON 의 images 로 확실히 알 수 있다. 배민 등 판별 불가면
    None — 이때는 사진을 '언급하지 않는' 쪽이 안전하다(없는 사진을 언급한
    답글이 실고객에 나간 사고, 2026-08-12).
    """
    if review.get("platform") == "coupang" and review.get("raw"):
        try:
            data = review["raw"]
            if isinstance(data, str):
                data = json.loads(data)
            return bool(data.get("images"))
        except Exception:  # noqa: BLE001
            return None
    return None


def classify_review(review):
    """리뷰를 대응 유형으로 분류한다.

    반환: escalate | complaint | question | praise_detail | photo_only |
          rating_only | neutral
    """
    if any(k in (review.get("content") or "") for k in ESCALATION_KEYWORDS):
        return "escalate"
    rating = review.get("rating")
    content = (review.get("content") or "").strip()
    if rating is not None and rating <= 3:
        return "complaint"
    if not content:
        # 사진이 '확인된' 경우에만 사진 리뷰로. 불확실하면 별점만 남긴
        # 리뷰로 보고 사진 언급을 피한다.
        return "photo_only" if _has_photo(review) else "rating_only"
    if any(q in content for q in ("?", "되나요", "있나요", "가능", "문의", "언제")):
        return "question"
    return "praise_detail" if len(content) >= 15 else "neutral"


_SENTENCE_ENDS = (".", "!", "?", "~", "😊", "🥯", "🐻", "✨", "☕", "🍪", "🥗", "💗", ")")


def _truncate_at_sentence(text, max_len):
    """모델 응답이 max_len을 넘으면 문장 끝에서 자른다.

    단순히 text[:max_len]로 자르면 "...훨씬 더 만족스러운" 처럼 문장 중간에서
    끊긴 채 실고객에게 나갈 뻔한 사고가 있었다(2026-08, 리뷰1198 쿠팡 300자
    한도). 잘라낼 자리를 max_len 이전 구간에서 마지막 문장부호/이모지 뒤로
    찾는다 — 못 찾으면 어쩔 수 없이 그 자리에서 자른다.
    """
    if len(text) <= max_len:
        return text
    window = text[:max_len]
    cut = max(window.rfind(c) for c in _SENTENCE_ENDS)
    if cut >= max_len // 2:  # 너무 앞쪽이면(잘라낼 게 거의 없으면) 포기
        return window[:cut + 1]
    return window


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
    # 쿠팡은 'KIM****************' 처럼 별을 길게 붙인다. 그대로 부르면 답글이
    # 이상해지므로 별은 최대 3개로 줄인다(마스킹은 유지).
    name = re.sub(r"\*{4,}", "***", name)
    return name


def generate_review_reply(review):
    """리뷰 하나에 대한 답글 초안을 생성한다(고객에게 게시할 후보).

    - 리뷰를 유형 분류 후 유형별 지침으로 답한다.
    - 🚨 민감 리뷰(이물질·건강·환불·법적 등)도 **1차 가이드 초안**은 만든다
      (사장님 요청 2026-08-16). 예전엔 '직접 대응 필요' 한 줄만 줘서 사장님이
      맨손으로 써야 했다. 다만 **자동 게시는 여전히 금지** — 사실 확인이
      필요한 건이라 사람이 확인하고 직접 등록한다(게시 차단은
      review_reply._apply 의 2중 차단이 그대로 담당).
    - 플랫폼별 글자수 한도·주문 횟수(단골)를 반영한다.

    ⚠️ '초안'만 만든다. 실제 게시는 반드시 사장님 승인을 거친다.
    """
    typ = classify_review(review)

    rating = review.get("rating")
    content = (review.get("content") or "").strip()
    author = _clean_author(review.get("author"))
    menus = ", ".join(_clean_menu(m) for m in (review.get("menus") or []))         or "주문 메뉴"
    # 주문 횟수는 단골·VIP 판단의 핵심 지표 — 넘겨받지 못했으면 원본에서 캔다.
    oc = order_count_of(review)
    cfg = PLATFORM_REPLY.get(review.get("platform"),
                             {"label": "", "max_len": 300, "target_len": 290})
    max_len = cfg["max_len"]
    target = target_len_for(typ, review.get("platform"), cfg.get("target_len", max_len))
    # 모델은 "N자 내외"보다 **범위**를 훨씬 잘 지킨다(실측: 내외로 주면 최대
    # 43% 짧게 나왔다). 실제 최종본 길이를 중심으로 위아래를 명시한다.
    smin, smax = SENTENCES_BY_KIND.get(typ, (3, 5))
    # 단계별로 대우를 달리한다(사장님 강조 2026-08-16): 처음 → 단골 → VIP.
    if oc == 1:
        visit = "첫 주문 고객 — 첫 방문을 반갑게 맞이하고 다음을 청한다"
    elif isinstance(oc, int) and oc >= 20:
        # ⚠️ 숫자를 프롬프트에 넣으면 모델이 반드시 그 숫자를 쓴다. 사장님
        #    실제 답글은 단골에게 횟수를 세지 않는다(숫자 언급 3.7%뿐,
        #    2026-08-24 실측) → 5회 이상은 숫자를 아예 주지 않는다.
        visit = ("아주 오래된 **VIP 단골** — 꾸준히 함께해 주신 것에 감사한다. "
                 "몇 번째인지 숫자는 세지 말 것")
    elif isinstance(oc, int) and oc >= 5:
        visit = ("**단골** — '꾸준히 찾아주셔서'·'여러 번 함께해주셔서'처럼 "
                 "알아봐 준다. 몇 번째인지 숫자는 세지 말 것")
    elif isinstance(oc, int) and oc > 1:
        visit = f"{_ko_ordinal(oc)} 주문 — 다시 찾아주신 것을 반긴다(한글 수로 표기)"
    else:
        visit = "고객(주문 횟수 모름 — 첫 주문인지 단골인지 단정하지 말 것)"

    try:
        ctx = _reply_context()
        ctx_block = f"[참고 사실(백데이터)]\n{ctx}\n\n" if ctx else ""
        lessons = _reply_lessons()
        if lessons:
            ctx_block += f"[답글 교훈 노트 — 반드시 지킬 것]\n{lessons}\n\n"
        # 사장님이 실제로 쓴 비슷한 답글을 함께 보여준다 — 무료·저가 모델은
        # 규칙보다 예시를 훨씬 잘 따라한다(사장님 제안 2026-08-18).
        ctx_block += _examples_block(review, typ, target=target)

        # 주문한 메뉴가 **무엇으로 만든 메뉴인지** 알려준다. 이름만 주면
        # 모델이 상상해서 쓴다(치아바타를 두고 "베이글의 식감"이라고 쓴 초안이
        # 실제로 나왔다, 2026-08-24).
        facts = menu_facts_for(review.get("menus"))
        if facts:
            ctx_block += ("[주문한 메뉴 사실 — 이 내용만 근거로 메뉴를 "
                          "이야기한다. 여기 없는 재료·식감을 지어내지 마라]" + chr(10)
                          + facts + chr(10) + chr(10))
        # 별점이 만점이 아니면 자축하지 말고 개선 한마디를 넣게 한다.
        if isinstance(rating, int) and rating <= 4:
            ctx_block += (f"[⚠️ 별점 {rating}점 — 만점이 아니다] 무엇이 아쉬웠는지 "
                          "글에 없더라도 마냥 자축하지 말고, '더 잘 챙기겠다'는 "
                          "한마디를 자연스럽게 넣는다. '별점 감사합니다'로 넘기지 "
                          "마라." + chr(10) + chr(10))
        user = (
            f"{ctx_block}"
            f"[{cfg['label']}] {visit} '{author}'가 {menus} 주문 후 "
            f"별점 {rating}점으로 남긴 리뷰:\n"
            f"\"{content or ('(사진만, 텍스트 없음)' if typ == 'photo_only' else '(내용 없이 별점만 남김)')}\"\n\n"
            f"[이 리뷰 유형 대응 지침] {_guide_for(review, typ)}\n"
            "위 지침대로 답글을 써줘." + chr(10)
            + "[구성] 아래 흐름으로 채운다(항목 제목은 쓰지 말고 자연스럽게 이어서):" + chr(10)
            + " ① 이름을 부르고 반가움·감사 — 정형구 말고 이 손님에게 하는 말로" + chr(10)
            + " ② 손님의 상황·마음에 공감 (아이와 함께, 운동 후, 바쁜 아침 등)" + chr(10)
            + " ③ 시키신 것에 대한 우리 이야기 — 어떻게 준비하는지, 왜 그 조합이 좋은지" + chr(10)
            + " ④ 다음에 권하고 싶은 것 한 가지 (강요 아닌 제안)" + chr(10)
            + " ⑤ 오늘 하루 응원하며 마무리" + chr(10) + chr(10)
            + f"**{smin}~{smax}문장**으로 쓴다(문장을 짧게 툭툭 끊지 말고 "
            + "한 문장 안에서 이유·마음까지 충분히 풀어 쓴다). "
            f"(닉네임 줄 제외, {max_len}자 절대 초과 금지). 사장님 실제 답글이 "
            "그 분량이다. 문장 수를 채우려고 같은 말을 반복하지 말고, 주문 "
            "메뉴·경험을 구체적으로 짚어 진짜 내용으로 채운다(사진은 실제로 "
            "있는 리뷰에서만 언급). 다른 답글과 겹치지 않게 자연스럽게 쓴다."
        )
        def _tidy(text):
            """정형구·상품명 태그·근거 없는 정책 문장을 걷어낸다.

            생성 직후에 돌려야 한다 — 맨 마지막에 돌리면 문장이 빠져 짧아진
            답글을 다시 채울 기회가 없다(2026-08-26).
            """
            return _drop_unfounded(_clean_menu(_strip_boilerplate(text)))

        def _write(extra=""):
            return _tidy(_truncate_at_sentence(
                _ask_claude(REPLY_PERSONA, user + extra, max_tokens=900,
                            model=_model_for(typ)), max_len))

        draft = _write()
        # 손님 말을 그대로 되풀이하거나 주문서 상품명을 옮겨 적으면 성의
        # 없어 보인다(사장님 지시 2026-08-26). 걸리면 무엇이 문제인지
        # 짚어서 한 번만 다시 쓰게 한다 — 지시문에만 적어 두면 계속 샌다.
        bad = []
        if echoed_phrases(content, draft):
            bad.append("손님이 쓴 문장을 그대로 따라 썼다")
        if copied_menu_names(review.get("menus"), draft):
            bad.append("주문서의 상품명을 그대로 옮겨 적었다")
        if len(draft) < target * 0.7:
            bad.append(f"너무 짧다({len(draft)}자) — {target}자에 가깝게 채워라")
        # 분량이 모자라면 '다시 쓰기'보다 **넓히기**가 잘 듣는다. 처음부터
        # 길게 쓰라고 하면 모델이 미사여구로 늘리는데, 쓴 답글을 두고
        # "무엇을 더 얘기할 수 있나"를 물으면 진짜 내용이 붙는다(2026-08-26).
        for _grow in range(2):
            if len(draft) >= target * 0.85:
                break
            grown = _truncate_at_sentence(_ask_claude(
                REPLY_PERSONA,
                "아래는 우리가 손님에게 보낼 답글 초안이다. 지금 "
                f"{len(draft)}자인데 **{target}자에 가깝게** 넓혀라." + chr(10)
                + "- 이미 쓴 문장을 바꾸지 말고, 자연스럽게 이어서 내용을 더한다."
                + chr(10)
                + "- 더할 거리: 손님 상황에 대한 공감 한 마디, 그 메뉴를 준비할 때"
                " 우리가 신경 쓰는 점, 다음에 곁들이면 좋을 조합 제안,"
                " 오늘 하루를 응원하는 인사." + chr(10)
                + "- 같은 말을 다시 쓰거나 '정말/너무' 같은 강조어로 늘리지 마라."
                + chr(10)
                + "- 없는 사실(이벤트·증정·할인·가격)은 절대 만들지 마라." + chr(10)
                + chr(10) + "[초안]" + chr(10) + draft,
                max_tokens=900, model=_model_for(typ)), max_len)
            grown = _tidy(grown)
            if len(grown) > len(draft):
                draft = grown
            else:
                break

        for _try in range(2):
            if not bad:
                break
            logger.info("답글 다시 쓰기(%d): %s", _try + 1, ", ".join(bad))
            draft = _write(
                chr(10) + chr(10) + "[다시 쓰기] 방금 쓴 답글에 이런 문제가 "
                "있었다: " + " / ".join(bad) + ". 같은 내용을 되풀이하지 말고, "
                "손님의 상황에 우리 표현으로 반응하며 처음부터 다시 써라. "
                f"길이는 {target}자에 가깝게(최소 {int(target * 0.85)}자) 채운다.")
            bad = []
            if echoed_phrases(content, draft):
                bad.append("손님이 쓴 문장을 그대로 따라 썼다")
            if copied_menu_names(review.get("menus"), draft):
                bad.append("주문서의 상품명을 그대로 옮겨 적었다")
            if len(draft) < target * 0.75:
                bad.append(f"너무 짧다({len(draft)}자) — {target}자에 가깝게 채워라")
        # 마지막 방어선 — 어떤 모델을 쓰든 '[SET]' 같은 꼬리표가 손님에게
        # 나가지 않게 본문에서도 한 번 더 지운다.
        if typ in _SENSITIVE_KINDS:
            draft = _drop_compensation(draft)
            if len(draft) < 40:          # 너무 많이 잘렸으면 안전한 템플릿으로
                return _strip_banned(
                    _template_reply(typ, review, author, oc, rating, max_len),
                    max_len)
        return _strip_banned(draft, max_len)
    except LLMUnavailable:
        # 템플릿도 검문을 태운다 — 사람이 쓴 문구라도 규칙이 바뀌면 어긋날 수 있다.
        return _strip_banned(
            _template_reply(typ, review, author, oc, rating, max_len), max_len)


# 생성 후 최종 검문 — 모델이 프롬프트의 금지 규칙을 흘리는 일이 실제로 있다
# (Gemini가 '바라요'를 씀, 2026-08-06). AI 말투·방문 표현·브랜드 금지어.
_REPLY_BANNED = (
    "바라며", "바라겠", "기원합니다",
    "바라요", "바랍니다", "되셨으면", "되었길", "즐거운 한 끼",
    "정성껏 준비하겠습니다", "정성을 다하겠습니다", "큰 힘이 됩니다",
    "보답하겠습니다", "보답할게요",
    "들러주세요", "놀러오세요", "오시면", "와주셔", "또 오세요",
    "역대급", "미쳤다", "인생맛집", "혜자", "대박",
)


# 금지 표현 → 사장님 말투로 바꾸는 **확정 치환표**.
# ⚠️ 왜 표가 필요한가: 예전엔 검문이 'AI 에게 다시 고쳐달라'고만 했는데,
#    AI 가 불안정하면(키 만료·한도 초과) 조용히 원문을 그대로 내보냈다.
#    그래서 '바라요' 같은 말투가 실제 답글까지 나갔다(사장님 지적 2026-08-16).
#    이제 표로 먼저 고치고, AI 는 있으면 다듬는 용도로만 쓴다.
# ⚠️ 긴 표현을 먼저 둔다 — 짧은 것이 먼저 걸리면 이중 치환돼 문장이 깨진다
#    ('드셨길 바랍니다' → '맛있게 맛있게 드셨길요' 사고, 2026-08-16).
_BANNED_FIX = (
    # '~바라며/바라겠' 는 목록에 없어서 초안에 그대로 나갔다(2026-08-24 실측:
    # "든든한 한 끼가 되셨기를 바라며"). 어미 변형까지 막는다.
    ("되셨기를 바라며", "되셨으면 좋겠어요"),
    ("되시길 바라며", "되셨으면 좋겠어요"),
    ("되었기를 바라며", "되었으면 좋겠어요"),
    ("바라며", "좋겠고"),
    ("바라겠", "좋겠"),
    ("기원합니다", "좋겠어요"),
    ("되셨으면 좋겠습니다", "되셨길요"),
    ("드셨길 바랍니다", "드셨길요"),
    ("드셨길 바라요", "드셨길요"),
    ("바라겠습니다", "좋겠어요"),
    ("바랍니다", "좋겠어요"),
    ("바라요", "좋겠어요"),
    ("되셨으면", "되었으면"),
    ("되었길 바랍니다", "되었길요"),
    ("되었길", "되었길요"),
    ("즐거운 한 끼", "맛있는 한 끼"),
    ("정성껏 준비하겠습니다", "정성껏 준비할게요"),
    ("정성을 다하겠습니다", "정성껏 만들게요"),
    ("큰 힘이 됩니다", "정말 힘이 나요"),
    ("큰 힘이 되었습니다", "정말 힘이 났어요"),
    ("보답하겠습니다", "더 맛있게 만들어 드릴게요"),
    ("보답할게요", "더 맛있게 만들어 드릴게요"),
    ("들러주세요", "주문 주세요"),
    ("놀러오세요", "주문 주세요"),
    ("또 오세요", "또 주문 주세요"),
    ("오시면", "주문 주시면"),
    ("와주셔서", "찾아주셔서"),
    ("와주셔", "찾아주셔"),
    ("역대급", "정말"),
    ("인생맛집", "맛있는 곳"),
    ("미쳤다", "정말 좋았다"),
    ("혜자", "알찬"),
    ("대박", "정말"),
)


def _fix_banned_locally(text):
    """금지 표현을 표대로 바꾼다(AI 없이도 항상 동작). 바뀐 텍스트 반환."""
    for bad, good in _BANNED_FIX:
        if bad in text:
            text = text.replace(bad, good)
    return text


def _strip_banned(text, max_len):
    """금지 표현을 없앤다 — **AI 가 없어도 반드시 없어진다.**

    1) 확정 치환표로 먼저 고친다(실패할 수 없는 경로).
    2) AI 가 살아 있으면 문장을 한 번 다듬는다(선택). 다듬은 결과에 금지어가
       남아 있으면 버리고 1)의 결과를 쓴다.
    """
    if not any(b in text for b in _REPLY_BANNED):
        return text

    safe = _fix_banned_locally(text)
    still = [b for b in _REPLY_BANNED if b in safe]
    if still:                       # 표에 없는 금지어가 남은 경우만 AI 에 맡긴다
        try:
            fixed = _ask_claude(
                REPLY_PERSONA,
                "다음 답글에서 금지 표현(" + ", ".join(still) + ")이 들어간 부분만 "
                "자연스러운 다른 말로 바꿔줘. 나머지 내용·말투·길이는 그대로 두고, "
                "답글 본문만 출력해:\n\n" + safe,
                max_tokens=600)
            fixed = _truncate_at_sentence(fixed, max_len).strip()
            if fixed and not any(b in fixed for b in _REPLY_BANNED):
                return fixed
        except LLMUnavailable:
            pass
    return _truncate_at_sentence(safe, max_len).strip() or safe[:max_len]


def _template_reply(typ, review, author, oc, rating, max_len):
    """크레딧 없을 때 템플릿 폴백 — 유형별 + 리뷰별 변형(복붙 방지)."""
    seed = str(review.get("review_no") or author)

    # 민감 리뷰(이물질·건강·환불·법적): 사실 확인 전이라 **단정·보상 약속
    # 없이** 사과 + 확인 + 조치만. 사장님이 확인 후 직접 등록한다.
    if typ == "escalate":
        return (f"{author}님,\n불편을 드린 점 진심으로 사과드립니다. "
                "말씀해 주신 상황은 바로 확인하겠습니다. 확인되는 대로 "
                "같은 일이 반복되지 않도록 조치하고 점검하겠습니다. "
                "불편을 감수하고 알려주셔서 감사합니다.")[:max_len]

    # 컴플레인: 감사보다 '사과'가 먼저. 정중한 격식체 + 다짐형(사장님 확정
    # 2026-07-26). 환불·보상 안내 금지, 캐주얼(ㅎㅎ·이모지) 금지.
    if typ == "complaint":
        loyal = ("늘 주문해 주시는데 이런 일이 생겨 더 죄송합니다. "
                 if isinstance(oc, int) and oc > 1 else "")
        return (f"{author}님,\n불편을 드린 점 진심으로 사과드립니다. {loyal}"
                "말씀해 주신 내용은 무겁게 받아들이고, 같은 일이 반복되지 않도록 "
                "바로 점검하고 개선하겠습니다. 알려주셔서 감사합니다. 다음에 주문 "
                "주시면 그때는 제대로 챙겨서 보내드리겠습니다.")[:max_len]

    # ⚠️ 주문 횟수를 모를 때(oc=None) '처음 주문해주셨는데' 라고 단정하면 안 된다.
    #    단골이 그 답글을 받으면 기분이 상한다 — 중립 인사를 쓴다.
    if isinstance(oc, int) and oc > 1:
        opener = f"벌써 {oc}번째네요, 기억해주고 또 주문해주셔서 진짜 감사해요."
    elif oc == 1:
        opener = "처음 주문해주셨는데 입맛에 맞으셨다니 너무 좋네요!"
    else:
        opener = "주문해주시고 이렇게 후기까지 남겨주셔서 감사해요!"

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
