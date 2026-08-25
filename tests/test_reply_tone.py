"""답글 말투 회귀 테스트 — AI/공지 말투 금지(사장님 피드백 2026-07-24).

'~바라요', '되셨으면 좋겠어요', '정성껏 준비하겠습니다' 등은 사람이 안 쓰는
말투라 금지. 템플릿 폴백이 이 표현을 절대 만들지 않는지 검증한다.
"""

import pytest

from assistant.beargels import _template_reply, _THANKS_VARIANTS, _truncate_at_sentence

# 모든 답글 공통 금지(AI 말투·방문 표현).
BANNED = [
    "바라요", "바랍니다", "되셨으면", "되었길", "즐거운 한 끼",
    "정성껏 준비하겠습니다", "정성을 다하겠습니다", "큰 힘이 됩니다",
    "보답하겠습니다", "보답할게요",
    # 방문 표현 — 배달이라 '주문 주세요'로(방문 아님)
    "들러주세요", "놀러오세요", "오시면", "와주셔", "또 오세요",
]
# 격식체 종결 — 일반 답글은 해요체 통일이라 금지. 단 '불만 답글'은 정중한
# 격식체+다짐형('점검하겠습니다')이 규칙(사장님 확정 2026-07-26)이라 예외.
FORMAL_ENDINGS = ["입니다", "습니다", "드립니다"]

REVIEWS = [
    {"platform": "coupang", "review_no": "1", "author": "김손님", "rating": 5,
     "content": "", "order_count": 1},
    {"platform": "coupang", "review_no": "2", "author": "이단골", "rating": 5,
     "content": "늘 맛있어요", "order_count": 5},
    {"platform": "baemin", "review_no": "3", "author": "박고객", "rating": 2,
     "content": "배달이 늦고 식었어요", "order_count": 1},
    {"platform": "baemin", "review_no": "4", "author": "최손님", "rating": 5,
     "content": "글루텐프리 있나요?", "order_count": 1},
]


@pytest.mark.parametrize("review", REVIEWS)
def test_template_reply_has_no_ai_phrases(review):
    from assistant.beargels import classify_review
    typ = classify_review(review)
    reply = _template_reply(typ, review, review["author"],
                            review["order_count"], review["rating"], 500)
    for bad in BANNED:
        assert bad not in reply, f"금지 표현 '{bad}' 포함: {reply}"
    # 격식체는 불만(complaint) 답글에서만 허용.
    if typ != "complaint":
        for bad in FORMAL_ENDINGS:
            assert bad not in reply, f"일반 답글에 격식체 '{bad}': {reply}"


def test_complaint_reply_is_formal_pledge():
    # 불만 답글: 정중한 격식체 + 다짐형('~하겠습니다'), 캐주얼(ㅎㅎ·이모지) 금지.
    from assistant.beargels import classify_review
    review = {"platform": "baemin", "review_no": "9", "author": "박고객",
              "rating": 2, "content": "배달이 늦고 식었어요", "order_count": 1}
    reply = _template_reply(classify_review(review), review, "박고객", 1, 2, 500)
    assert "사과드립니다" in reply
    assert "하겠습니다" in reply           # 다짐형
    assert "ㅎㅎ" not in reply and "🐻" not in reply
    assert "환불" not in reply and "고객센터" not in reply  # 보상 안내 금지


def test_thanks_variants_clean():
    joined = " ".join(_THANKS_VARIANTS)
    for bad in BANNED + FORMAL_ENDINGS:
        assert bad not in joined, f"_THANKS_VARIANTS 에 금지 표현 '{bad}'"


def test_persona_forbids_unkeepable_promises():
    # 사장님 피드백: 지킬 수 없는 약속(공짜 증정 등) 금지 규칙이 페르소나에 있어야.
    from assistant.beargels import REPLY_PERSONA
    assert "지킬 수 없는 약속" in REPLY_PERSONA


def test_persona_has_delivery_and_nofabrication_rules():
    from assistant.beargels import REPLY_PERSONA
    assert "주문 주세요" in REPLY_PERSONA           # 배달 표현
    assert "지어내지 않는다" in REPLY_PERSONA         # 없는 사실 금지


# 실사고 재현(2026-08, 리뷰1198): 쿠팡 300자 한도를 넘는 모델 응답을
# text[:max_len]로 그냥 잘라 "...훨씬 더 만족스러운" 처럼 문장 중간에서
# 끊긴 채 실제로 게시됐다. 문장부호 뒤에서 자르도록 고쳤다.
def test_truncate_at_sentence_cuts_on_boundary_not_mid_word():
    text = ("다음번에 다시 주문을 주신다면 그때는 훨씬 더 만족스러운 경험을 "
            "드릴 수 있도록 노력하겠습니다. 감사합니다.")
    cut = _truncate_at_sentence(text, 58)
    assert cut == text[:len(cut)]          # 접두사 그대로(내용 변형 없음)
    assert cut.endswith((".", "!", "?", "~"))  # 문장부호에서 끝남
    assert not cut.endswith("만족스러운")       # 예전 버그: 여기서 끊겼었다


def test_truncate_at_sentence_returns_unchanged_when_within_limit():
    text = "짧은 답글입니다."
    assert _truncate_at_sentence(text, 300) == text


def test_truncate_at_sentence_falls_back_when_no_boundary_found():
    text = "가" * 500  # 문장부호가 전혀 없는 극단적인 경우
    cut = _truncate_at_sentence(text, 300)
    assert len(cut) == 300


def test_generate_review_reply_never_exceeds_platform_limit(monkeypatch):
    # 모델이 한도를 넘겨 응답해도(300자 한도에 320자짜리 응답), 최종 초안은
    # 한도 안에서 문장 끝까지만 담아야 한다.
    import assistant.beargels as beargels

    overlong = ("맛있게 드셨다니 정말 기쁘네요. " * 20) + "감사합니다."
    assert len(overlong) > 300
    monkeypatch.setattr(beargels, "_ask_claude", lambda *a, **k: overlong)

    review = {"platform": "coupang", "review_no": "10", "author": "김손님",
              "rating": 5, "content": "너무 맛있어요", "order_count": 1}
    draft = beargels.generate_review_reply(review)
    assert len(draft) <= 300
    assert draft.endswith((".", "!", "?", "~"))


# --- 금지 말투는 AI 가 없어도 반드시 사라진다 (사장님 지적 2026-08-16) -------

def test_banned_words_removed_without_ai(monkeypatch):
    """검문이 AI 재작성에만 기대면, AI 가 죽었을 때 '바라요'가 그대로 나간다.
    실제로 그렇게 새어 실고객 답글까지 갔다 — 표 치환으로 항상 없앤다."""
    import assistant.beargels as bg

    def dead(*a, **k):                       # AI 완전 불가 상황
        raise bg.LLMUnavailable("no key")

    monkeypatch.setattr(bg, "_ask_claude", dead)
    text = ("고객님, 맛있게 드셨길 바라요. 다음에도 큰 힘이 됩니다. "
            "가까이 오시면 들러주세요.")
    out = bg._strip_banned(text, 500)
    for bad in ("바라요", "큰 힘이 됩니다", "오시면", "들러주세요"):
        assert bad not in out, f"'{bad}' 가 남았다: {out}"


def test_local_fix_keeps_meaning():
    """치환 결과가 문장으로 읽혀야 한다(토막나면 안 됨)."""
    import assistant.beargels as bg
    out = bg._fix_banned_locally("맛있게 드셨길 바랍니다. 또 오세요.")
    assert "드셨길요" in out and "또 주문 주세요" in out
    assert "맛있게 맛있게" not in out   # 이중 치환 방지


def test_ai_rewrite_rejected_if_still_banned(monkeypatch):
    """AI 가 고쳐 준 결과에도 금지어가 남으면 그 결과를 버린다."""
    import assistant.beargels as bg
    monkeypatch.setattr(bg, "_ask_claude", lambda *a, **k: "여전히 바라요")
    out = bg._strip_banned("맛있게 드셨길 바라요", 500)
    assert "바라요" not in out


# --- 예시 기반 생성: 무료 모델로도 사장님 문체가 나오게 (2026-08-18) --------

def test_examples_block_is_built_from_owner_replies():
    """유형이 맞는 예시가 프롬프트에 실려야 한다(없으면 빈 문자열)."""
    import assistant.beargels as bg
    bg._EXAMPLES_CACHE = {"rating_only": [
        {"rating": 5, "content": "", "menus": ["베이글"], "order_count": 3,
         "reply": "김손님님, 세 번째 주문 감사해요! 맛있게 드셨길요."}]}
    r = {"platform": "coupang", "review_no": "1", "author": "김손님",
         "rating": 5, "content": "", "menus": ["베이글"]}
    block = bg._examples_block(r, "rating_only")
    assert "사장님이 실제로 쓴 답글" in block
    assert "세 번째 주문 감사해요" in block
    assert "베끼지 말고" in block          # 복붙 방지 지시가 있어야 한다
    bg._EXAMPLES_CACHE = None


def test_examples_prefer_same_menu_and_order_bucket():
    """같은 메뉴·같은 단골 구간 예시가 먼저 뽑혀야 한다."""
    import assistant.beargels as bg
    bg._EXAMPLES_CACHE = {"rating_only": [
        {"rating": 5, "content": "", "menus": ["딴거"], "order_count": 1,
         "reply": "무관한 예시"},
        {"rating": 5, "content": "", "menus": ["베이글"], "order_count": 30,
         "reply": "딱 맞는 예시"},
    ]}
    r = {"platform": "coupang", "review_no": "2", "author": "손님",
         "rating": 5, "content": "", "menus": ["베이글"],
         "raw": '{"orderCount": 25}'}
    picked = bg.pick_examples(r, "rating_only", k=1)
    assert picked and picked[0]["reply"] == "딱 맞는 예시"
    bg._EXAMPLES_CACHE = None


def test_no_examples_is_not_fatal():
    import assistant.beargels as bg
    bg._EXAMPLES_CACHE = {}
    assert bg._examples_block({"menus": []}, "rating_only") == ""
    bg._EXAMPLES_CACHE = None


# --- 사실 카드(프롬프트에 들어가는 '사실')가 오염되지 않았는지 ------------
# 틀린 사실 하나가 그대로 손님에게 나간다. 2026-08-23 점검에서 관리용 태그가
# 붙은 메뉴명·사라진 제조 사실·주문 불가한 반제품이 발견됐다.

def test_fact_card_keeps_manufacturing_truth():
    import pathlib
    card = (pathlib.Path(__file__).resolve().parent.parent
            / "reference" / "reply_context.md").read_text(encoding="utf-8")
    assert "본사 새벽 냉동" in card      # 베이글은 매장에서 굽지 않는다
    assert "그릴 토스팅" in card


def test_fact_card_menu_names_are_clean():
    import pathlib
    import re
    card = (pathlib.Path(__file__).resolve().parent.parent
            / "reference" / "reply_context.md").read_text(encoding="utf-8")
    section = card.split("## 판매 메뉴")
    assert len(section) > 1, "판매 메뉴 절이 있어야 한다"
    names = [l[2:].strip() for l in section[1].split("##")[0].split("\n")
             if l.startswith("- ")]
    assert names, "메뉴가 비면 AI 가 메뉴명을 지어낸다"
    for n in names:
        assert not re.search(r"\[[^\]]{1,12}\]", n), f"관리용 태그가 남음: {n}"
        assert "대박맛집" not in n, f"배민 키워드칩이 붙음: {n}"
        assert "반제품" not in n, f"손님이 주문할 수 없는 항목: {n}"


# --- 분량 기준이 실제 사장님 답글에서 나왔는지 (2026-08-24) -----------------
# 지침에 "짧고 산뜻하게 감사만"이라고 적혀 있었는데, 직원 최종본은 그 유형이
# 오히려 가장 길었다(별점만 리뷰 249자·5문장). 지침과 실제가 반대라
# AI 초안을 매번 다시 쓰게 만들었다 — 수정률이 안 떨어지던 큰 원인.

def test_length_targets_exist_for_every_kind():
    from assistant.beargels import TARGET_BY_KIND, SENTENCES_BY_KIND
    for kind in ("rating_only", "praise_detail", "neutral", "complaint"):
        assert kind in TARGET_BY_KIND, f"{kind} 목표 길이 없음"
        assert kind in SENTENCES_BY_KIND, f"{kind} 문장 수 없음"
        lo, hi = SENTENCES_BY_KIND[kind]
        assert 2 <= lo < hi <= 8


def test_rating_only_guide_is_not_curt():
    """별점만 리뷰에 '한 줄 감사'로 끝내라고 시키면 안 된다(실측과 반대)."""
    from assistant.beargels import _TYPE_GUIDE
    g = _TYPE_GUIDE["rating_only"]
    assert "짧고 산뜻" not in g
    assert "사진" in g            # 없는 사진을 언급하지 말라는 경고는 유지


def test_regular_customer_count_is_not_fed_to_model():
    """5회 이상 단골은 숫자를 세지 않는다 — 직원 답글의 3.7%만 숫자를 쓴다."""
    import inspect
    from assistant import beargels
    src = inspect.getsource(beargels.generate_review_reply)
    assert "숫자는 세지 말 것" in src


# --- 메뉴를 지어내지 못하게: 주문한 메뉴의 실제 사실을 프롬프트에 넣는다 -----
# 이름만 주면 모델이 상상해서 쓴다 — '런던식 터키쉬 샌드위치'(베이글)를 두고
# "치아바타에 베이글의 식감까지"라고 쓴 초안이 실제로 나왔다(2026-08-24).

def test_menu_facts_come_from_master(monkeypatch):
    from assistant import beargels as B
    monkeypatch.setattr(B, "_MENU_FACTS_CACHE", {
        B._menu_key("런던식 터키쉬 샌드위치"):
            ("런던식 터키쉬 샌드위치", "담백한 터키햄과 두 가지 치즈"),
    })
    out = B.menu_facts_for(["[SET] 런던식 터키쉬 샌드위치"])
    assert "담백한 터키햄" in out          # 태그가 붙어 있어도 찾아낸다
    assert B.menu_facts_for(["없는메뉴xyz"]) == ""   # 모르면 지어내지 않는다


def test_banned_endings_are_replaced():
    """'바라며' 같은 어미 변형이 목록에 없어 그대로 나갔다(실측)."""
    from assistant.beargels import _fix_banned_locally, _REPLY_BANNED
    out = _fix_banned_locally("든든한 한 끼가 되셨기를 바라며, 또 뵐게요")
    assert "바라며" not in out
    for w in ("바라며", "바라겠", "기원합니다"):
        assert w in _REPLY_BANNED


def test_menu_tag_and_trailing_letter_are_stripped():
    from assistant.beargels import _clean_menu
    assert _clean_menu("[B]둘을 위한 커플 세트R") == "둘을 위한 커플 세트"
    assert _clean_menu("[SET] 베이글 샌드위치 + 음료 세트") == "베이글 샌드위치 + 음료 세트"


# --- 배민 리뷰 본문 파싱 (2026-08-24 사장님 발견) ------------------------
# '(최근 6개월 누적 주문)' 이라는 잎 하나가 정확히 일치해야만 본문을 잡던
# 구조라, 배민 리뷰 115건의 본문이 통째로 유실됐다. AI 는 그 리뷰들을
# "글 없이 별점만 남기셨네요"로 답했다.

def test_baemin_body_is_extracted():
    from crawler.baemin import BaeminCrawler as B
    card = ("알뜰배달 한입에와앙 2026년 8월 22일 리뷰번호 2026082202038366 "
            "6회 주문 고객 (최근 6개월 누적 주문) 맛있더요 포장이 좋아요 "
            "주문메뉴 런던식 모짜렐라 베이글 배달리뷰 좋아요 사장님 댓글 등록하기")
    assert B._extract_body(card) == "맛있더요 포장이 좋아요"


def test_rating_only_card_has_no_body():
    from crawler.baemin import BaeminCrawler as B
    card = ("알뜰배달 오이 2026년 8월 22일 리뷰번호 2026082200000001 "
            "1회 주문 고객 (최근 6개월 누적 주문) 주문메뉴 플레인 베이글 "
            "배달리뷰 좋아요 사장님 댓글 등록하기")
    assert B._extract_body(card) is None


def test_owner_reply_is_not_mistaken_for_body():
    """카드에 딸려 온 사장님 답글을 리뷰 본문으로 저장하면 안 된다."""
    from crawler.baemin import BaeminCrawler as B
    card = ("알뜰배달 제리 2026년 3월 3일 리뷰번호 2026030300000001 "
            "2회 주문 고객 (최근 6개월 누적 주문) 배달이 늦었어요 "
            "주문메뉴 플레인 베이글 배달리뷰 좋아요 "
            "사장님 2026년 3월 3일 안녕하세요, 제리 고객님. 죄송합니다.")
    assert B._extract_body(card) == "배달이 늦었어요"


# --- 등록한 답글을 AI로 다시 쓸 때 상태가 유지되는지 (2026-08-24) ----------
# 예전엔 재생성이 무조건 reply_status 를 'drafted' 로 되돌려, '등록한 답글'
# 화면에서 AI 재생성을 누르면 그 답글이 목록에서 사라졌다.

def test_regen_keeps_posted_status(monkeypatch):
    from database import supabase_client as db
    seen = {}
    monkeypatch.setattr(db, "_update_review", lambda rid, patch: seen.update(patch))
    db.save_ai_draft(1, "새 답글", kind="praise_detail", keep_status=True)
    assert "reply_status" not in seen          # 등록 상태를 건드리지 않는다
    assert seen["reply_draft"] == "새 답글"

    seen.clear()
    db.save_ai_draft(1, "새 초안", kind="praise_detail")
    assert seen["reply_status"] == "drafted"   # 평소(대기 중 초안)는 그대로
