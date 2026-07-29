"""로컬 답글 엔진 — Claude API 없이도 '진짜 답글'을 조립한다.

크레딧 부족/네트워크 장애로 LLM을 못 쓸 때의 폴백을, 단순 감사 템플릿이
아니라 리뷰 내용에 실제로 반응하는 조립식 답글로 고도화한다(사장님 요청
2026-07-29). 원리:

  [호칭+오프너(주문 횟수별)] + [주문 메뉴 반응] + [리뷰 내용 키워드 반응]
  + [클로징] 을 리뷰별 시드 랜덤으로 조합 → 같은 문장 복붙이 안 생긴다.

지켜야 할 말투(테스트로 회귀 방지):
  - 일반 답글: 편한 해요체. '~바라요/되셨으면/정성껏 준비하겠습니다' 같은
    AI·공지 말투 금지. 배달 가게라 방문 표현('들러주세요') 금지.
  - 불만 답글은 beargels._template_reply 의 확정 형식(격식체+다짐형)을 쓰고
    이 모듈은 관여하지 않는다.
  - 없는 사실(원산지·조리법 약속 등)을 지어내지 않는다 — 문장은 전부
    '고객 경험에 대한 반응'과 '가게 자신의 이야기'로만 구성.
"""

import random

# ---------------------------------------------------------------------------
# 문장 풀 — 전부 해요체, 금지 표현 없음(tests/test_reply_tone.py 로 검증).
# ---------------------------------------------------------------------------

_OPENER_FIRST = (
    "처음 주문해주셨는데 입맛에 맞으셨다니 정말 기뻐요!",
    "첫 주문에 이렇게 좋은 리뷰까지 남겨주셔서 감사해요!",
    "처음 찾아주셨는데 맛있게 드셨다니 다행이에요!",
)
_OPENER_REPEAT = (
    "벌써 {oc}번째 주문이시네요, 늘 찾아주셔서 진짜 감사해요.",
    "{oc}번이나 주문해주신 단골님이라 리뷰가 더 반가워요.",
    "또 주문해주셨네요! 기억하고 있어요, 항상 감사해요.",
)
_OPENER_DEFAULT = (
    "주문해주시고 리뷰까지 남겨주셔서 감사해요!",
    "맛있게 드시고 리뷰까지, 오늘 제일 반가운 소식이에요.",
)

# 메뉴명 키워드 → 메뉴 언급 반응(사실 주장 없이 '반응'만).
# {menu}=짧게 정리한 메뉴명, {menu_j}=메뉴명+은/는(받침 자동).
# ⚠️ 순서 중요: 세트명('베이글 샌드위치')이 '베이글'로 오매칭되지 않게
#    구체적인 키워드(샌드위치·세트)를 앞에 둔다.
_MENU_REACTIONS = (
    ("세트", ("세트로 든든하게 챙겨 드셨다니 좋네요.",)),
    ("샌드위치", ("{menu} 든든하게 드셨다니 좋네요.",)),
    ("케이크", ("케이크는 포장할 때마다 제일 조심하는 메뉴라 "
               "예쁘게 잘 도착했다니 다행이에요.",)),
    ("잠봉", ("잠봉뵈르는 버터 풍미가 포인트인데 제대로 즐기셨네요.",)),
    ("치아바타", ("치아바타 겉은 바삭하게 속은 촉촉하게 잘 갔다니 안심이에요.",)),
    ("크림치즈", ("크림치즈 조합으로 드셨다니, 그 조합 진짜 좋죠 ㅎㅎ",)),
    ("바질", ("바질 향 좋아하시는 분들이 특히 자주 찾는 메뉴예요.",)),
    ("러스크", ("러스크까지 챙겨 드셨다니 감사해요, 바삭했길래 넣어봤어요 ㅎㅎ",)),
    ("베이글", ("{menu} 쫄깃하게 잘 즐기셨다니 마음이 놓여요.",
               "{menu_j} 식감이 생명인데 맛있게 드셨다니 뿌듯해요.")),
)
_MENU_GENERIC = (
    "주문해주신 {menu} 맛있게 드셨다니 마음이 놓여요.",
    "{menu} 고르신 안목, 최고예요 ㅎㅎ",
)


def _short_menu(name):
    """'[SET] 베이글 샌드위치 + 미니샐러드 + 음료 세트' → '베이글 샌드위치'.

    괄호 태그를 벗기고 '+' 앞부분만 남겨 답글에 자연스럽게 들어가게 한다.
    """
    import re
    name = re.sub(r"[\[(][^\])]*[\])]", " ", name or "")
    name = name.split("+")[0].strip()
    return name[:20].strip() or "주문하신 메뉴"


def _with_topic_josa(word):
    """받침 유무에 따라 은/는을 붙인다(예: 베이글→베이글은, 케이크→케이크는)."""
    ch = word[-1] if word else ""
    if "가" <= ch <= "힣" and (ord(ch) - 0xAC00) % 28 != 0:
        return word + "은"
    return word + "는"

# 리뷰 본문 키워드 → 반응 문장.
_CONTENT_REACTIONS = (
    (("맛있", "맛나", "존맛", "최고", "굿"),
     ("맛있었다는 말이 저희한텐 제일 힘나는 칭찬이에요.",
      "맛있게 드셨다니 오늘 하루 피로가 싹 풀리네요 ㅎㅎ")),
    (("포장", "배송", "배달"),
     ("포장 상태까지 챙겨봐주셔서 감사해요.",)),
    (("신선", "재료"),
     ("재료는 늘 신선하게 챙기려고 하는데 알아봐주셔서 기뻐요.",)),
    (("양이", "양도", "푸짐", "든든"),
     ("양도 만족스러우셨다니 다행이에요.",)),
    (("쫀득", "쫄깃", "꾸덕", "겉바", "바삭"),
     ("그 식감 알아봐주시다니, 제대로 즐기셨네요 ㅎㅎ",)),
    (("아이", "애기", "가족", "남편", "아내", "부모님"),
     ("가족분들이랑 같이 맛있게 드셨다니 더 기분 좋네요.",)),
    (("또 시", "재주문", "단골", "자주", "매번", "항상"),
     ("다음 주문도 실망시키지 않을게요!",)),
)

_PHOTO_LINES = (
    "올려주신 사진만 봐도 맛있게 드신 게 보여서 저희가 다 뿌듯해요 ㅎㅎ",
    "사진 예쁘게 찍어 올려주셔서 감사해요, 저장해두고 싶을 정도예요.",
)

_QUESTION_LINES = (
    "물어봐주신 부분은 확인해서 답글이나 다음 주문 때 꼭 챙길게요.",
    "여쭤봐주신 건 확인해볼게요, 편하게 물어봐주셔서 감사해요!",
)


def _closers():
    from assistant.beargels import _THANKS_VARIANTS
    return _THANKS_VARIANTS


def compose(review, typ, author, oc, rating, max_len):
    """리뷰 내용에 반응하는 답글을 API 없이 조립한다(불만 제외 전 유형).

    문장을 하나씩 붙이며 max_len 을 넘기 직전에 멈춰, 중간에 잘린 문장이
    없게 한다.
    """
    rng = random.Random(f"{review.get('platform')}:{review.get('review_no')}")
    content = (review.get("content") or "").strip()
    menus = [m for m in (review.get("menus") or []) if m]

    parts = [f"{author}님,"]

    # 1) 오프너 — 주문 횟수별.
    if oc == 1:
        parts.append(rng.choice(_OPENER_FIRST))
    elif isinstance(oc, int) and oc > 1:
        parts.append(rng.choice(_OPENER_REPEAT).format(oc=oc))
    else:
        parts.append(rng.choice(_OPENER_DEFAULT))

    # 2) 메뉴 반응 — 주문 메뉴명을 실제로 언급.
    #    (사진만 리뷰는 짧게가 원칙이라 메뉴 문장은 생략)
    if menus and typ != "photo_only":
        menu = _short_menu(menus[0])
        pool = None
        for key, lines in _MENU_REACTIONS:
            if any(key in _short_menu(m) for m in menus):
                menu = _short_menu(next(m for m in menus if key in _short_menu(m)))
                pool = lines
                break
        line = rng.choice(pool or _MENU_GENERIC)
        parts.append(line.format(menu=menu, menu_j=_with_topic_josa(menu)))

    # 3) 리뷰 본문 키워드 반응(최대 2개) / 사진만 리뷰 / 질문.
    if typ == "photo_only":
        parts.append(rng.choice(_PHOTO_LINES))
    elif typ == "question":
        parts.append(rng.choice(_QUESTION_LINES))
    elif content:
        hit = 0
        for keys, lines in _CONTENT_REACTIONS:
            if any(k in content for k in keys):
                parts.append(rng.choice(lines))
                hit += 1
                if hit >= 2:
                    break

    # 4) 클로징.
    parts.append(rng.choice(_closers()))

    # 조립 — 호칭 뒤 줄바꿈, 이후 문장은 공백 연결. 한도 넘기 전 문장에서 멈춤.
    out = parts[0]
    for i, p in enumerate(parts[1:]):
        cand = out + ("\n" if i == 0 else " ") + p
        if len(cand) > max_len:
            break
        out = cand
    return out
