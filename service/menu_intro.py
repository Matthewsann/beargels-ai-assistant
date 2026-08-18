"""메뉴 이름 → 플랫폼 소개글(한/영) 초안.

새 메뉴를 만들 때마다 소개글을 손으로 쓰게 하면 결국 비게 되고, 그러면
채널마다 문구가 또 갈린다. 이름과 분류만으로 **쓸 만한 초안**을 만들어 두고
사장님이 다듬게 한다.

왜 AI 를 안 쓰나 — 이 서비스 앱에는 일부러 AI 키를 두지 않는다(service/app.py
머리말). 키를 넣으면 클라우드에 올라가고, 메뉴 소개는 문장 틀이 뚜렷해서
규칙만으로도 초안 품질이 충분하다. 실제로 기존 175개 소개글의 문형을 그대로
옮겼다.

브랜드 규칙(knowledge/톤앤보이스.md · 브랜드.md):
 · 과장 금지 — '역대급·인생맛집·미쳤다' 류는 사전에 아예 없다
 · **'갓 구운·수제 베이글·직접 반죽' 금지** — 베이글은 본사 냉동 납품이고
   매장은 그릴 토스팅만 한다. '주문 즉시 토스팅'만 사실이라 쓴다.
 · 수제청·크림치즈는 실제로 만드니 '수제'를 쓸 수 있다.
"""
from __future__ import annotations

import re

# 맛·재료 키워드 → (한글 수식구, 영문 표현). 이름에서 먼저 걸리는 것을 쓴다.
FLAVOR = [
    ("두바이", "피스타치오 필링을 넣은", "Dubai-style pistachio"),
    ("피스타치오", "고소한 피스타치오를 더한", "pistachio"),
    ("흑임자", "고소한 흑임자를 더한", "black sesame"),
    ("인절미", "인절미의 고소함을 담은", "injeolmi"),
    ("말차", "제주 말차의 깊은 풍미를 담은", "Jeju matcha"),
    ("그린티", "녹차의 깊은 풍미를 담은", "green tea"),
    ("초코", "진한 초코의 달콤함을 담은", "rich chocolate"),
    ("초콜릿", "진한 초콜릿의 풍미를 담은", "rich chocolate"),
    ("쇼콜라", "진한 초콜릿의 풍미를 담은", "chocolate"),
    ("딸기", "딸기의 상큼함을 담은", "strawberry"),
    ("블루베리", "블루베리의 상큼함을 담은", "blueberry"),
    ("크랜베리", "건크랜베리를 넣어 상큼한", "cranberry"),
    ("애플망고", "애플망고의 진한 달콤함을 담은", "apple mango"),
    ("망고", "망고의 달콤함을 담은", "mango"),
    ("자몽", "자몽의 상큼함이 살아 있는", "grapefruit"),
    ("레몬", "레몬의 상큼함을 더한", "lemon"),
    ("유자", "유자의 상큼한 향을 담은", "yuzu"),
    ("청포도", "청포도의 달콤함을 담은", "green grape"),
    ("키위", "키위의 상큼함을 담은", "kiwi"),
    ("복숭아", "복숭아의 달콤한 향을 담은", "peach"),
    ("수박", "수박의 시원한 단맛을 담은", "watermelon"),
    ("바나나", "바나나의 부드러운 단맛을 담은", "banana"),
    ("우베", "우베의 은은한 단맛을 담은", "ube"),
    ("바질", "바질의 향긋함이 살아 있는", "basil"),
    ("어니언", "구운 양파의 깊은 풍미를 담은", "roasted onion"),
    ("양파", "구운 양파의 깊은 풍미를 담은", "roasted onion"),
    ("대파", "구운 대파의 풍미를 담은", "roasted scallion"),
    ("베이컨", "베이컨의 짭조름한 풍미를 더한", "bacon"),
    ("햄", "햄을 넉넉히 넣은", "ham"),
    ("잠봉", "잠봉햄과 고메버터를 넣은", "jambon and butter"),
    ("터키", "훈제 터키햄을 넣은", "smoked turkey"),
    ("닭가슴살", "닭가슴살을 넉넉히 넣은", "chicken breast"),
    ("풀드포크", "바베큐 풀드포크를 듬뿍 넣은", "barbecue pulled pork"),
    ("연어", "연어를 올린", "salmon"),
    ("크래미", "크래미와 채소를 담은", "crab stick"),
    ("에그마요", "에그마요를 넉넉히 넣은", "egg mayo"),
    ("계란", "계란을 넣은", "egg"),
    ("치즈", "치즈의 고소함이 살아 있는", "cheese"),
    ("모짜렐라", "모짜렐라가 듬뿍 들어간", "mozzarella"),
    ("체다", "체다 치즈를 넣은", "cheddar"),
    ("크림치즈", "부드러운 크림치즈를 넣은", "cream cheese"),
    ("카프레제", "토마토와 채소를 산뜻하게 담은", "tomato and greens"),
    ("통밀", "통밀의 고소함이 살아 있는", "whole wheat"),
    ("플레인", "기본에 충실한", "plain"),
    ("소금", "달지 않은 소금 크림을 올린", "lightly salted cream"),
    ("연유", "연유의 달콤함을 더한", "condensed milk"),
    ("바닐라", "부드러운 바닐라 향을 더한", "vanilla"),
    ("헤이즐넛", "헤이즐넛의 고소한 향을 더한", "hazelnut"),
    ("카라멜", "카라멜의 달콤함을 더한", "caramel"),
    ("밤", "밤의 고소한 풍미를 담은", "chestnut"),
    ("고구마", "고구마의 은은한 단맛을 담은", "sweet potato"),
    ("감자", "포슬포슬한 감자 식감을 살린", "potato"),
    ("팥", "달콤한 팥을 넣은", "red bean"),
    ("호두", "고소한 호두를 넣은", "walnut"),
    ("아몬드", "고소한 아몬드를 넣은", "almond"),
    ("캐슈넛", "고소한 캐슈넛을 넣은", "cashew"),
    ("피넛", "고소한 땅콩을 더한", "peanut"),
    ("허니콘", "꿀과 콘의 달콤함을 담은", "honey corn"),
    ("꿀", "꿀의 달콤함을 더한", "honey"),
    ("홀스레디시", "알싸한 홀스레디시 풍미를 담은", "horseradish"),
    ("민트", "상쾌한 민트를 더한", "mint"),
    ("얼그레이", "얼그레이의 향긋함을 담은", "Earl Grey"),
    ("밀크티", "진한 홍차와 우유를 담은", "black tea and milk"),
    ("홍차", "진한 홍차를 담은", "black tea"),
    ("요거트", "부드러운 요거트를 담은", "yogurt"),
    ("디카페인", "카페인 부담 없이 즐기는", "decaf"),
    ("콜드브루", "차갑게 내린 콜드브루를 담은", "cold brew"),
    ("프로틴", "프로틴을 더한", "added protein"),
    ("단백질", "단백질을 더한", "added protein"),
]

# 품목(문장 끝에 오는 말) → (한글, 영문). 긴 것부터 찾는다.
ITEM = [
    ("치아바타 샌드위치", "치아바타 샌드위치", "ciabatta sandwich"),
    ("베이글 샌드위치", "베이글 샌드위치", "bagel sandwich"),
    ("샌드위치", "샌드위치", "sandwich"),
    ("탄단지 샐러드", "샐러드", "balanced salad"),
    ("샐러드", "샐러드", "salad"),
    ("크림치즈", "크림치즈", "cream cheese"),
    ("치아바타", "치아바타", "ciabatta"),
    ("베이글", "베이글", "bagel"),
    ("아메리카노", "아메리카노", "americano"),
    ("콜드브루", "콜드브루", "cold brew"),
    ("라떼", "라떼", "latte"),
    ("스무디", "스무디", "smoothie"),
    ("에이드", "에이드", "ade"),
    ("하이볼", "논알콜 하이볼", "alcohol-free highball"),
    ("아이스티", "아이스티", "iced tea"),
    ("밀크티", "밀크티", "milk tea"),
    ("티", "티", "tea"),
    ("주스", "주스", "juice"),
    ("케이크", "케이크", "cake"),
    ("쿠키", "쿠키", "cookie"),
    ("스콘", "스콘", "scone"),
    ("도넛", "도넛", "donut"),
    ("붕어", "붕어빵", "fish-shaped pastry"),
    ("버터바", "버터바", "butter bar"),
    ("컵빙", "컵빙수", "cup bingsu"),
    ("러스크", "러스크", "rusk"),
    ("빵", "빵", "bread"),
    ("세트", "세트", "set"),
]

# 분류만 알 때 쓰는 기본 품목
CATEGORY_ITEM = {
    "베이커리": ("베이커리 메뉴", "bakery item"),
    "크림치즈": ("크림치즈", "cream cheese"),
    "샌드위치": ("샌드위치", "sandwich"),
    "샐러드": ("샐러드", "salad"),
    "디저트": ("디저트", "dessert"),
    "커피": ("커피", "coffee"),
    "논커피": ("음료", "drink"),
    "시그니처&스페셜": ("시그니처 음료", "signature drink"),
    "에이드&스무디": ("음료", "drink"),
    "티": ("티", "tea"),
    "보틀": ("1L 보틀 음료", "1L bottled drink"),
    "세트": ("세트", "set"),
}

# 그릴 토스팅은 사실이라 쓸 수 있다. 빵류에만 붙인다.
TOAST_KO = " 주문 즉시 바삭하게 토스팅해 드립니다."
TOAST_EN = " Toasted crisp to order."
TOASTABLE = ("베이글", "치아바타", "샌드위치")


def _clean(name: str) -> str:
    """[SET]·(200g) 같은 꼬리표를 떼고 알맹이만 남긴다."""
    s = re.sub(r"\[[^\]]*\]", " ", name or "")
    s = re.sub(r"\([^)]*\)", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def draft(name: str, category: str = "") -> tuple[str, str]:
    """(한글, 영문) 초안. 못 만들면 빈 문자열."""
    base = _clean(name)
    if not base:
        return "", ""

    item_ko = item_en = ""
    for key, ko, en in ITEM:
        if key in base:
            item_ko, item_en = ko, en
            break
    if not item_ko:
        item_ko, item_en = CATEGORY_ITEM.get((category or "").strip(), ("메뉴", "item"))

    # 품목으로 이미 쓴 말은 맛에서 뺀다 — '크림치즈'가 품목인데 맛에도 잡히면
    # "크림치즈 with cream cheese" 같은 문장이 된다.
    used = item_ko + item_en
    flavors = []
    for key, ko_p, en_p in FLAVOR:
        if key not in base or key in used or en_p in used:
            continue
        if any(en_p == f[2] for f in flavors):
            continue
        flavors.append((key, ko_p, en_p))
        if len(flavors) == 2:
            break

    if flavors:
        ko = f"{flavors[0][1]} {item_ko}입니다."
        en = f"{item_en.capitalize()} with {' and '.join(f[2] for f in flavors)}."
    else:
        # 맛을 못 읽었을 때. 한글은 이름을 그대로 쓰되, 영문에 한글을 섞지 않는다
        # (플랫폼에 그대로 붙으면 그게 더 나쁘다). 분류 기준 한 줄로 둔다.
        ko = f"베어글스의 {item_ko}입니다."
        en = f"Our {item_en}."

    if any(t in base for t in TOASTABLE) and (category or "") in ("베이커리", "샌드위치"):
        ko += TOAST_KO
        en += TOAST_EN
    return ko, en
