"""이미지 분석 + 릴스/알고리즘 최적화 캡션·해시태그 생성.

**유료 Claude 전용**(사장님 확정 2026-08-30 저녁): 인스타 AI 는 유료만 쓰고
무료 Gemini 한도는 리뷰 답글 몫으로 남긴다. `llm.complete(only=("claude",),
paid=True)` 라 Gemini 는 두드리지 않고, Claude 가 실패하면 webapp 의
템플릿 폴백(_fallback_caption)으로 떨어진다.
"""

import asyncio
import io
import logging
import os
from dataclasses import dataclass

from PIL import Image

logger = logging.getLogger(__name__)

# 요청이 무거워지지 않게 리사이즈 (무료 한도도 아낀다)
MAX_DIMENSION = 1280
JPEG_QUALITY = 85

# 브랜드·전략·템플릿 지식 폴더 (안의 모든 .md를 프롬프트에 붙인다)
_KNOWLEDGE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "knowledge")


def _load_knowledge() -> str:
    """knowledge 폴더의 모든 .md 파일을 하나로 합친다 (파일명 순)."""
    try:
        names = sorted(f for f in os.listdir(_KNOWLEDGE_DIR) if f.endswith(".md"))
    except OSError:
        return ""
    parts = []
    for name in names:
        try:
            with open(os.path.join(_KNOWLEDGE_DIR, name), encoding="utf-8") as f:
                parts.append(f"### {name}\n{f.read()}")
        except OSError:
            continue
    return "\n\n".join(parts)


SYSTEM_PROMPT = """\
너는 베이글 카페 '베어글스 송도점'의 인스타그램 릴스/게시물 담당자다.
매장에서 촬영한 영상/사진을 보고, 인스타그램 알고리즘에 맞는 캡션과 해시태그를 쓴다.

아래 [브랜드·전략 지침]을 반드시 근거로 삼아라. 이게 최우선 기준이다.

[브랜드·전략 지침]
{knowledge}

핵심 원칙 (요약):
- 브랜드 보이스: 꾸밈없이 맛있는 것만 / 친근·솔직 / 과장 금지 / 이모지 0~2개.
- 캡션 첫 문장은 스크롤을 멈추게 하는 '훅'. 이어서 담백하게 2~4문장.
- 캡션에 검색 키워드를 자연스럽게 녹인다 (예: 송도 베이글, 송도 카페, 메뉴명).
- 해시태그는 3~5개. 브랜드+지역+메뉴를 섞고, 캡션이 아닌 별도 필드로 낸다.
- 영상(릴스)이면 무음 시청을 고려해, 필요하면 화면 자막 아이디어도 제안한다.
"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "menu": {
            "type": "string",
            "description": "파악한 메뉴/소재 이름 (파악 불가 시 '미상')",
        },
        "caption": {
            "type": "string",
            "description": "인스타 캡션 본문. 첫 문장은 훅. 해시태그는 넣지 말 것.",
        },
        "hashtags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "해시태그 3~5개, 각 항목은 #으로 시작",
        },
        "overlay_text": {
            "type": "string",
            "description": "릴스 화면에 넣으면 좋은 짧은 자막 문구 제안 (사진이면 빈 문자열)",
        },
    },
    "required": ["menu", "caption", "hashtags", "overlay_text"],
    "additionalProperties": False,
}


@dataclass
class CaptionResult:
    menu: str
    caption: str
    hashtags: list[str]
    overlay_text: str = ""

    @property
    def full_text(self) -> str:
        return f"{self.caption}\n\n{' '.join(self.hashtags)}"


def _prepare_image(image_bytes: bytes) -> tuple[str, bytes]:
    """리사이즈 + JPEG 변환. llm.complete 의 images 형식인 (mime, 바이트)로 반환."""
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.thumbnail((MAX_DIMENSION, MAX_DIMENSION))
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return ("image/jpeg", buffer.getvalue())


class CaptionGenerator:
    def __init__(self, api_key: str | None = None):
        # api_key 인자는 예전 호환용 — 더 이상 쓰지 않는다.
        # 지침은 금고 전체(26K자, 희석+비용)가 아니라 릴스 전용 발췌를 쓴다:
        # '굽지 않음' 사실·금지어 11개·실판매 메뉴 표기가 확실히 들어가게.
        from .planner import _brand_core
        core = _brand_core()
        self._system = SYSTEM_PROMPT.format(
            knowledge=core or _load_knowledge()[:3000] or "(지침 파일 없음)")

    async def generate(
        self,
        images: list[bytes],
        topic: str,
        is_reel: bool = False,
        media_count: int = 1,
        previous_caption: str | None = None,
        feedback: str | None = None,
        note: str = "",
    ) -> CaptionResult:
        """캡션 생성.

        images: 대표 프레임/사진 이미지 바이트 목록 (영상이면 썸네일).
        topic: 콘텐츠 주제(폴더명). feedback이 있으면 이전 캡션 기반 재생성.
        note: 사장님의 촬영 메모 — 메뉴명·한정 같은 **사실의 출처**.
              메모에 없는 사실은 캡션에 쓰지 않는다(지어내기 방지).
        """
        content: list[dict] = [_prepare_image(b) for b in images if b]

        kind = "릴스(영상)" if is_reel else "사진 게시물"
        instruction = (
            f"콘텐츠 주제: {topic}\n"
            f"형식: {kind} (미디어 {media_count}개)\n"
        )
        note = (note or "").strip()
        if note:
            instruction += (
                f"\n[사장님 메모 — 사실과 의도의 출처]\n{note[:1500]}\n"
                "메뉴 이름·가격·한정/신메뉴 같은 사실은 이 메모와 브랜드 지침에 "
                "있는 것만 쓴다. 사진은 묘사에만 쓴다.\n"
            )
        else:
            instruction += ("\n(사장님 메모 없음 — 사실 단정 없이 "
                            "화면 묘사·식감 중심으로 쓸 것)\n")
        if not content:
            instruction += "이미지를 분석할 수 없으니 주제명을 참고해 작성해줘.\n"
        instruction += "이 콘텐츠의 캡션과 해시태그를 작성해줘."

        if feedback and previous_caption:
            instruction += (
                f"\n\n이전에 작성한 캡션:\n{previous_caption}\n\n"
                f"사장님(Matthew)의 수정 요청:\n{feedback}\n\n"
                "수정 요청을 반영해서 다시 작성해줘."
            )

        from .planner import INSTA_CLAUDE_MODEL, _json_from
        import json as _json
        import llm

        sys_full = (
            f"{self._system}\n\n"
            "반드시 아래 JSON 스키마에 맞는 **JSON 하나만** 출력한다. "
            "설명·인사말·코드펜스 금지.\n"
            f"{_json.dumps(OUTPUT_SCHEMA, ensure_ascii=False)}"
        )
        text = await asyncio.to_thread(
            llm.complete, system=sys_full, user=instruction, max_tokens=1024,
            images=content or None, only=("claude",), paid=True,
            model=INSTA_CLAUDE_MODEL,
        )
        data = _json_from(text)
        hashtags = [t if t.startswith("#") else f"#{t}" for t in data["hashtags"]]
        return CaptionResult(
            menu=data["menu"],
            caption=data["caption"],
            hashtags=hashtags,
            overlay_text=data.get("overlay_text", ""),
        )
