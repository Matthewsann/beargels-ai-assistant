"""메뉴 소개글(한/영)을 LLM 으로 쓴다 — 규칙 생성기보다 훨씬 낫다.

규칙(menu_intro.py)으로 찍으면 "~살아 있는 ~입니다"만 반복돼 카페 문구가
아니게 된다(사장님 지적 2026-08-17). 문장은 LLM 이 쓰고, 규칙 쪽은 LLM 을
못 쓸 때의 대비로만 남긴다.

문체는 **정보 중심으로 단정하게**(사장님 선택). 재료·특징을 분명히 적고,
감탄사나 과장 없이 한두 문장. 외국인·번역기가 봐도 오해가 없게 쓴다.
"""
from __future__ import annotations

import json
import os
import re
import time

# 무료 한도가 넉넉한 쪽. .env 에서 바꿀 수 있다.
MODEL = os.getenv("INTRO_GEMINI_MODEL", "gemini-flash-lite-latest")

SYSTEM = """너는 '베어글스'라는 동네 베이글 카페의 메뉴 소개글을 쓴다.
네이버 플레이스·배달앱·키오스크에 그대로 들어갈 짧은 글이다.

문체 — 정보 중심으로 단정하게:
- 한글 1~2문장. 재료와 특징을 분명히 적는다. 두 번째 문장은 먹는 법이나
  어울리는 상황을 담백하게 덧붙인다(없어도 된다).
- 영문 1~2문장. 한국어를 직역하지 말고 영어로 자연스럽게 쓴다.
- 감탄사·과장·이모지를 쓰지 않는다.

절대 쓰지 않는 말:
- 역대급, 미쳤다, 인생맛집, 가성비 끝판왕, 무조건, 반드시, 대박, 줄 서서 먹는
- **갓 구운, 수제 베이글, 매일 직접 반죽, 매장에서 구운** — 베이글은 본사에서
  냉동으로 납품받고 매장에서는 그릴에 토스팅만 한다. 제빵을 암시하면 허위다.

써도 되는 사실:
- 빵류는 "주문 즉시 토스팅"이 사실이다(꼭 넣을 필요는 없다).
- 수제청·크림치즈는 매장/본사에서 실제로 만들므로 '수제'를 쓸 수 있다.

출력은 JSON 만. 설명이나 코드블록 없이:
{"ko": "한글 소개", "en": "English intro"}"""


def _prompt(name, category, composition=None, description=None):
    lines = [f"메뉴명: {name}", f"분류: {category or '미정'}"]
    if composition:
        lines.append(f"구성: {composition}")
    if description:
        lines.append(f"기존 설명(참고만, 그대로 베끼지 말 것): {description[:300]}")
    return "\n".join(lines)


def _parse(text):
    """```json 펜스나 앞뒤 잡소리가 붙어 와도 JSON 만 건져낸다."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        raise ValueError(f"JSON 을 못 찾음: {t[:120]}")
    obj = json.loads(m.group(0))
    ko = (obj.get("ko") or "").strip()
    en = (obj.get("en") or "").strip()
    if not ko or not en:
        raise ValueError("ko/en 이 비어 있음")
    return ko, en


def draft(name, category="", composition=None, description=None):
    """(한글, 영문). 실패하면 예외를 올린다 — 호출부가 규칙 생성기로 넘어간다.

    사장님 선택에 따라 **무료 제미나이**를 쓴다. llm.complete 는 공급자를
    자동으로 고르므로, 여기서는 제미나이를 직접 부른다(클로드 크레딧을 메뉴
    소개에 쓸 이유가 없다). 제미나이 키가 없으면 자동 선택으로 넘어간다.
    """
    import llm

    user = _prompt(name, category, composition, description)
    have = llm.available_providers()
    last = None
    if "gemini" in have:
        # 모델마다 무료 한도가 다르다. 기본값(gemini-flash-latest → 3.7-flash)은
        # **하루 20건**뿐이라 메뉴 175개를 못 돌린다. flash-lite 는 한도가 훨씬
        # 넉넉해서 소개글처럼 짧은 글에는 이쪽이 맞다(2026-08-17 실측).
        # 무료 티어라 429(할당량)·503(과부하)이 흔하다. 잠깐 쉬고 두 번 더 —
        # 한 번 실패했다고 규칙 초안으로 떨어지면 품질이 확 나빠진다.
        for attempt in range(3):
            try:
                return _parse(llm._call_gemini(SYSTEM, user, 400, MODEL))
            except Exception as e:  # noqa: BLE001
                last = e
                msg = str(e)
                if "429" not in msg and "503" not in msg:
                    break              # 진짜 오류면 더 두드리지 않는다
                time.sleep(2 * (attempt + 1))
    # 제미나이가 안 되면 다른 공급자(클로드)라도 써 본다.
    try:
        return _parse(llm.complete(SYSTEM, user, max_tokens=400))
    except Exception as e:  # noqa: BLE001
        raise (last or e)
