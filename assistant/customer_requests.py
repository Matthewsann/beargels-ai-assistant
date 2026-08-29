"""리뷰 속 '고객 요청사항' 골라내기 — AI 없이 규칙으로만 (2026-08-28).

이 모듈은 리뷰 답글 페이지군의 세 목표(CLAUDE.md 참고) 중 세 번째
"고객 요청·반응을 놓치지 않고 전 직원에게 전파" 를 담당한다.

왜 만들었나:
    "스테이플러로 포장해주시는 것보단 스티커가 조아요, 열 때 넘 위험하더라구요"
    같은 말이 별점 5점 칭찬 리뷰 속에 섞여 들어온다. 답글은 잘 나가지만
    **정작 매장에서 바꿔야 할 이야기**는 아무도 모르고 지나간다
    (사장님 요청 2026-08-28). 그래서 따로 모아 단톡방에 옮기기 쉽게 만든다.

왜 AI 를 안 쓰나:
    사장님 확정(2026-08-28) — 키워드만. 대신 **정밀도**로 승부한다.
    요청 신호만 보면 절반이 오탐이었다(실측 1,663건 중 43건이 걸리는데
    그중 절반):
        · "테디베어 치즈케이크가 넘 귀여워서 먹기 아쉬웠지만"  ← '아쉬'
        · "이 맛있는 베이글 널리널리 알려지면 좋겠어요"        ← '좋겠'
        · "요청사항 들어주셔서 감사합니다"                     ← '요청'
    그래서 **요청 신호 + 매장이 실제로 바꿀 수 있는 대상**이 같은 문장에
    있을 때만 잡는다. '딸래미로 받아주세요' 같은 농담이 걸러지는 것도
    같은 이유다(바꿀 대상이 없다).

쓰는 쪽:
    from assistant.customer_requests import find_requests, format_for_kakao
    items = find_requests(reviews)          # 리뷰 dict 목록 → 요청 목록
    text  = format_for_kakao(items)         # 단톡방에 그대로 붙일 글
"""

from __future__ import annotations

import re

# 매장이 **실제로 바꿀 수 있는** 대상들. 요청처럼 들려도 이 중 하나를
# 가리키지 않으면 잡지 않는다(농담·덕담 거르기).
# 순서가 곧 분류 우선순위다 — 앞엣것이 먼저 걸린다.
TOPICS: tuple[tuple[str, str, str], ...] = (
    ("포장", "📦", r"포장|스티커|스테이플러|호치키스|스템플|봉투|용기|뚜껑|"
                   r"비닐|박스|담아|밀봉|새(?:서|어서)|샜"),
    ("가격", "💰", r"최소\s*주문|최소금액|주문\s*금액|가격|비싸|저렴|할인|배달비|"
                   r"쿠폰|금액"),
    ("양·구성", "🥯", r"양이|양을|양은|사이즈|크기|개수|한\s*개|더\s*넣|더\s*주|"
                     r"적게|많이\s*주|세트|구성|메뉴에|메뉴를|추가해|만들어\s*주"),
    ("맛·품질", "🔥", r"탄\s*자국|탔|타서|눅눅|딱딱|질겨|퍽퍽|덜\s*익|설익|비려|"
                     r"짜(?:요|고|서)|싱거|굽기|구워|바삭|촉촉|신선|상했|"
                     r"소스|크림|재료|속이|빵이"),
    ("온도", "🌡️", r"식어|차갑|미지근|따뜻|뜨겁|데워"),
    ("시간·영업", "🕘", r"영업\s*시간|오픈|마감|일찍|늦게|시간을|준비\s*시간|"
                       r"조리\s*시간"),
    ("배달·누락", "🛵", r"누락|빠졌|안\s*왔|안\s*들어|빼먹|잘못\s*왔|다른\s*게|"
                       r"배달\s*기사|늦게\s*왔|오래\s*걸"),
    ("서비스·응대", "🙇", r"응대|불친절|친절|직원|사장님이|말투|전화"),
)

# 요청·불편 신호. '~해주세요' 류(부탁)와 '~가 낫다' 류(선호)와
# '위험/불편' 류(문제 제기)를 모두 본다.
_ASK = re.compile(
    r"해\s*주세요|해주시면|해\s*주시|해주셨으면|해\s*주실|해\s*주시길|"
    r"주세요|주시면|주실\s*수|바랍니다|부탁|건의|요청드|"
    r"면\s*좋겠|면\s*좋을|으면\s*합니다|했으면|하면\s*안|하면\s*될까|"
    r"가능할까|안\s*될까|안\s*댈까|않을까|어떨까|추천드|바꿔|바뀌었으면|"
    r"낫(?:다|아요|을|겠)|나을|더\s*좋|"
    r"위험|불편|아쉬|아깝|힘들었|곤란|주의"
)

# 'A 보다 B 가 좋다' 꼴의 **선호**. '보다'만 보면 설명문까지 걸린다
# ("빵 보다는 초코 무스로 되어있는데" — 요청이 아니다). 그래서 비교 표현과
# 좋다/낫다가 **함께** 있을 때만 요청으로 본다.
# ⚠️ '조아요' 같은 표기도 잡는다 — 손님 글은 맞춤법대로 오지 않는다
#    (실제 사례: "스테이플러보단 스티커가 조아요").
# ⚠️ '무엇보다·누구보다·그보다'는 비교가 아니라 강조다("무엇보다 아침 일찍
#    배달되는 게 넘 좋았어요" — 칭찬이다). 앞글자를 확인해 걸러낸다.
_PREFER = (re.compile(r"(?<!무엇)(?<!누구)(?<!그)보단|"
                      r"(?<!무엇)(?<!누구)(?<!그)보다는|"
                      r"(?<!무엇)(?<!누구)(?<!그)보다\s|대신|차라리"),
           re.compile(r"좋|조아|낫|나을|편(?:해|하)"))

# 칭찬으로만 쓰이는 '~가 좋아요'는 요청 신호에서 뺐다 — "야채 신선하고
# 조합이 좋아요" 같은 문장이 통째로 걸려 들어왔다(2026-08-28 실측).

# 이 말이 같은 문장에 있으면 요청이 아니라 **칭찬·감사**다.
# ("요청사항 들어주셔서 감사합니다" 같은 문장을 통째로 거른다)
_THANKS = re.compile(r"감사|고맙|덕분|잘\s*먹었|맛있게\s*먹|최고|짱|훌륭|"
                     r"들어주셔|챙겨주셔|신경\s*써\s*주")

# 문장 나누기 — 마침표가 거의 없는 리뷰가 많아 줄바꿈·이모지도 경계로 본다.
_SPLIT = re.compile(r"[.!?~\n]+|(?<=[다요])\s{2,}")

_PLAT = {"baemin": "배민", "coupang": "쿠팡"}

# 리뷰는 마침표 없이 한 덩어리로 오는 일이 흔하다. 그러면 문장 하나가
# 리뷰 전체가 되어 단톡방 글이 통째로 길어진다. 접속어를 잘라 **요청이
# 시작되는 지점**부터 보여준다("…딸래미로 받아주세요♡ 그리고 확실히
# 스테이플러로…" → "확실히 스테이플러로…" 부터).
_CLAUSE = re.compile(r"\s(?:그리고|그리구|글구|그런데|근데|다만|대신|"
                     r"한가지|한 가지|그래도|하지만|단)\s")
QUOTE_MAX = 90


def _trim_quote(sentence: str, topic_pat: str) -> str:
    """요청 대목만 남긴다 — 앞의 칭찬·인사는 덜어낸다."""
    m = re.search(topic_pat, sentence)
    if not m:
        return sentence[:QUOTE_MAX]
    # 요청 대상 앞의 마지막 접속어 뒤부터 시작한다
    start = 0
    for c in _CLAUSE.finditer(sentence):
        if c.end() <= m.start():
            start = c.end()
        else:
            break
    out = sentence[start:].strip()
    if len(out) > QUOTE_MAX:
        out = out[:QUOTE_MAX].rstrip() + "…"
    return out


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SPLIT.split(text or "") if s and s.strip()]


def _topic_of(sentence: str) -> tuple[str, str, str] | None:
    """(분류, 아이콘, 그 분류를 맞춘 정규식) — 인용을 다듬을 때 다시 쓴다."""
    for name, icon, pat in TOPICS:
        if re.search(pat, sentence):
            return name, icon, pat
    return None


def request_in(text: str) -> tuple[str, str, str] | None:
    """리뷰 본문에서 요청 문장을 찾는다 → (분류, 아이콘, 그 문장). 없으면 None.

    문장 단위로 본다. 리뷰 하나에 칭찬과 요청이 섞여 있는 게 보통이라,
    글 전체를 뭉쳐서 보면 '감사'가 섞여 요청이 묻힌다.
    """
    for s in _sentences(text):
        asked = bool(_ASK.search(s)) or all(p.search(s) for p in _PREFER)
        if not asked:
            continue
        if _THANKS.search(s):
            continue                    # 이미 만족한 이야기다
        hit = _topic_of(s)
        if hit:
            name, icon, pat = hit
            return name, icon, _trim_quote(s, pat)
    return None


def find_requests(reviews, limit=20) -> list[dict]:
    """리뷰 목록에서 요청사항만 골라 최신순으로 돌려준다.

    Args:
        reviews: DB 에서 읽은 리뷰 dict 목록(content·author·platform·
                 written_date·rating 를 본다).
    """
    out = []
    for r in reviews or []:
        found = request_in(r.get("content") or "")
        if not found:
            continue
        topic, icon, quote = found
        out.append({
            "id": r.get("id"),
            "topic": topic,
            "icon": icon,
            "quote": " ".join(quote.split()),
            "author": r.get("author") or "손님",
            "rating": r.get("rating"),
            "platform": _PLAT.get(r.get("platform"), r.get("platform") or ""),
            "date": (r.get("written_date") or "")[:10],
            "content": " ".join((r.get("content") or "").split()),
        })
    out.sort(key=lambda x: x["date"], reverse=True)
    return out[:limit]


def format_for_kakao(items, today=None) -> str:
    """단톡방에 그대로 붙여 넣을 글. 링크·군더더기 없이 읽히게만."""
    if not items:
        return "📌 새로 들어온 고객 요청사항이 없어요."
    head = f"📌 고객 요청사항 {len(items)}건"
    if today:
        head += f" ({today})"
    lines = [head, ""]
    for i, it in enumerate(items, 1):
        lines.append(f"{i}. [{it['topic']}] {it['quote']}")
        star = f" ★{it['rating']}" if it.get("rating") else ""
        who = f"   - {it['platform']} {it['author']}님{star}"
        if it.get("date"):
            who += f" · {it['date'][5:].replace('-', '/')}"
        lines.append(who)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
