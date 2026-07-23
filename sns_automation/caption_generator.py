"""Claude API 이미지 분석 + 캡션/해시태그 생성."""

import base64
import io
import logging
from dataclasses import dataclass

from anthropic import AsyncAnthropic
from PIL import Image

logger = logging.getLogger(__name__)

MODEL = "claude-opus-4-8"

# Claude API 이미지 제한에 맞춰 리사이즈
MAX_DIMENSION = 2000
JPEG_QUALITY = 85

SYSTEM_PROMPT = """\
너는 베이글 가게 '베어글스'의 인스타그램 담당자다.
매장에서 촬영한 사진/영상을 보고 인스타그램 게시물의 캡션과 해시태그를 작성한다.

브랜드 보이스:
- 꾸밈없이 맛있는 것만
- 친근하고 자연스러운 한국어
- 과장 없이 솔직하게 (예: "인생 최고의", "미쳤다" 같은 과장 표현 금지)
- 이모지는 최소한으로 (0~2개)

캡션 작성 규칙:
- 사진 속 메뉴가 무엇인지 파악하고, 그 메뉴를 중심으로 짧고 담백하게 쓴다
- 2~4문장, 말하듯 자연스럽게
- 해시태그는 캡션 본문에 넣지 않는다 (별도 필드로 분리)

해시태그 규칙:
- 8~12개
- #베어글스 #베이글 은 항상 포함
- 메뉴명, 동네/상권, 베이커리/카페 관련 태그를 섞는다
- 한국어 태그 위주, 영어 태그는 2개 이하
"""

OUTPUT_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "menu": {
                "type": "string",
                "description": "사진에서 파악한 메뉴 이름 (파악 불가 시 '미상')",
            },
            "caption": {"type": "string", "description": "인스타그램 캡션 본문"},
            "hashtags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "해시태그 목록, 각 항목은 #으로 시작",
            },
        },
        "required": ["menu", "caption", "hashtags"],
        "additionalProperties": False,
    },
}


@dataclass
class CaptionResult:
    menu: str
    caption: str
    hashtags: list[str]

    @property
    def full_text(self) -> str:
        return f"{self.caption}\n\n{' '.join(self.hashtags)}"


def _prepare_image(image_bytes: bytes) -> tuple[str, str]:
    """리사이즈 + JPEG 변환 후 (base64, media_type) 반환."""
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.thumbnail((MAX_DIMENSION, MAX_DIMENSION))
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    data = base64.standard_b64encode(buffer.getvalue()).decode("utf-8")
    return data, "image/jpeg"


class CaptionGenerator:
    def __init__(self, api_key: str):
        self.client = AsyncAnthropic(api_key=api_key)

    async def generate(
        self,
        image_bytes: bytes | None,
        file_name: str,
        is_video: bool = False,
        previous_caption: str | None = None,
        feedback: str | None = None,
    ) -> CaptionResult:
        """캡션 생성. feedback이 있으면 이전 캡션을 기반으로 재생성."""
        content: list[dict] = []

        if image_bytes:
            data, media_type = _prepare_image(image_bytes)
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": data},
                }
            )

        media_kind = "영상 (아래 이미지는 영상 썸네일)" if is_video else "사진"
        instruction = f"파일명: {file_name}\n미디어 종류: {media_kind}\n"
        if not image_bytes:
            instruction += "이미지를 분석할 수 없으니 파일명을 참고해 캡션을 작성해줘.\n"
        instruction += "이 게시물의 캡션과 해시태그를 작성해줘."

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
            system=SYSTEM_PROMPT,
            output_config={"format": OUTPUT_SCHEMA},
            messages=[{"role": "user", "content": content}],
        )

        import json

        text = next(b.text for b in response.content if b.type == "text")
        data = json.loads(text)
        hashtags = [t if t.startswith("#") else f"#{t}" for t in data["hashtags"]]
        return CaptionResult(menu=data["menu"], caption=data["caption"], hashtags=hashtags)
