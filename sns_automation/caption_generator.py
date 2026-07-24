"""Claude API 이미지 분석 + 릴스/알고리즘 최적화 캡션·해시태그 생성."""

import base64
import io
import json
import logging
import os
from dataclasses import dataclass

from anthropic import AsyncAnthropic
from PIL import Image

logger = logging.getLogger(__name__)

MODEL = "claude-opus-4-8"

# Claude API 이미지 제한에 맞춰 리사이즈
MAX_DIMENSION = 2000
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
    "type": "json_schema",
    "schema": {
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
    },
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


def _prepare_image(image_bytes: bytes) -> dict:
    """리사이즈 + JPEG 변환 후 Claude 이미지 블록으로 반환."""
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.thumbnail((MAX_DIMENSION, MAX_DIMENSION))
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    data = base64.standard_b64encode(buffer.getvalue()).decode("utf-8")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": data},
    }


class CaptionGenerator:
    def __init__(self, api_key: str):
        self.client = AsyncAnthropic(api_key=api_key)
        self._system = SYSTEM_PROMPT.format(knowledge=_load_knowledge() or "(지침 파일 없음)")

    async def generate(
        self,
        images: list[bytes],
        topic: str,
        is_reel: bool = False,
        media_count: int = 1,
        previous_caption: str | None = None,
        feedback: str | None = None,
    ) -> CaptionResult:
        """캡션 생성.

        images: 대표 프레임/사진 이미지 바이트 목록 (영상이면 썸네일).
        topic: 콘텐츠 주제(폴더명). feedback이 있으면 이전 캡션 기반 재생성.
        """
        content: list[dict] = [_prepare_image(b) for b in images if b]

        kind = "릴스(영상)" if is_reel else "사진 게시물"
        instruction = (
            f"콘텐츠 주제: {topic}\n"
            f"형식: {kind} (미디어 {media_count}개)\n"
        )
        if not content:
            instruction += "이미지를 분석할 수 없으니 주제명을 참고해 작성해줘.\n"
        instruction += "이 콘텐츠의 캡션과 해시태그를 작성해줘."

        if feedback and previous_caption:
            instruction += (
                f"\n\n이전에 작성한 캡션:\n{previous_caption}\n\n"
                f"사장님(Matthew)의 수정 요청:\n{feedback}\n\n"
                "수정 요청을 반영해서 다시 작성해줘."
            )

        content.append({"type": "text", "text": instruction})

        response = await self.client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=self._system,
            output_config={"format": OUTPUT_SCHEMA},
            messages=[{"role": "user", "content": content}],
        )

        text = next(b.text for b in response.content if b.type == "text")
        data = json.loads(text)
        hashtags = [t if t.startswith("#") else f"#{t}" for t in data["hashtags"]]
        return CaptionResult(
            menu=data["menu"],
            caption=data["caption"],
            hashtags=hashtags,
            overlay_text=data.get("overlay_text", ""),
        )
