"""발행용 미디어 임시 호스팅 (Supabase Storage).

Buffer는 파일을 직접 업로드받지 않고 "공개 URL"만 받는다.
그래서 발행할 사진/편집된 릴스를 Supabase Storage 공개 버킷에 잠깐 올려
공개 URL을 만들어 전달하고, 발행이 끝나면 지운다.

사진은 인스타그램이 JPEG만 받으므로 (아이폰 HEIC, PNG 등은) JPEG로 변환한다.
릴스는 편집기(video_editor)가 이미 인스타 규격 mp4(1080×1920)로 만들어 주므로
변환 없이 그대로 올린다.
"""

import io
import logging

from PIL import Image, ImageOps
from supabase import Client

logger = logging.getLogger(__name__)

BUCKET = "sns-media"

# 인스타그램 권장: 최대 1440px, JPEG
_MAX_SIZE = 1440


def to_jpeg(image_bytes: bytes) -> bytes:
    """어떤 포맷이든 인스타그램용 JPEG로 변환 (회전 보정 + 리사이즈 포함)."""
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)  # 폰 사진 회전 정보 반영
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if max(img.size) > _MAX_SIZE:
        img.thumbnail((_MAX_SIZE, _MAX_SIZE), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()


class MediaHost:
    def __init__(self, client: Client):
        self.client = client

    def _upload(self, path: str, data: bytes, content_type: str) -> str:
        """공개 버킷에 올리고 공개 URL을 반환 (같은 이름이면 덮어쓰기)."""
        self.client.storage.from_(BUCKET).upload(
            path, data, {"content-type": content_type, "upsert": "true"}
        )
        url = self.client.storage.from_(BUCKET).get_public_url(path)
        return url.rstrip("?")  # get_public_url 이 붙이는 '?' 꼬리 제거

    def upload_image(self, post_id: str, image_bytes: bytes) -> str:
        """JPEG 변환 후 공개 버킷에 올리고 공개 URL을 반환."""
        url = self._upload(f"{post_id}.jpg", to_jpeg(image_bytes), "image/jpeg")
        logger.info("발행용 이미지 업로드: %s", url)
        return url

    def upload_video(self, post_id: str, video_bytes: bytes) -> str:
        """편집된 릴스 mp4를 공개 버킷에 올리고 공개 URL을 반환."""
        url = self._upload(f"{post_id}.mp4", video_bytes, "video/mp4")
        logger.info("발행용 릴스 업로드: %s (%.1fMB)", url, len(video_bytes) / 1e6)
        return url

    def delete(self, post_id: str) -> None:
        """발행 완료 후 임시 파일 정리 (사진·영상 모두, 실패해도 치명적이지 않음)."""
        try:
            self.client.storage.from_(BUCKET).remove([f"{post_id}.jpg", f"{post_id}.mp4"])
        except Exception:
            logger.warning("임시 파일 삭제 실패 (무시): %s", post_id)
