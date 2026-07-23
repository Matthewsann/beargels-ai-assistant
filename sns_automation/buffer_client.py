"""Buffer API 연동 (인스타그램 예약 발행)."""

import logging

import httpx

logger = logging.getLogger(__name__)

BUFFER_API_BASE = "https://api.bufferapp.com/1"


class BufferError(Exception):
    pass


class BufferClient:
    def __init__(self, access_token: str, instagram_profile_id: str):
        self.access_token = access_token
        self.profile_id = instagram_profile_id

    async def create_update(
        self,
        text: str,
        photo_url: str | None = None,
        video_url: str | None = None,
    ) -> dict:
        """Buffer 큐에 게시물을 추가한다 (프로필의 발행 스케줄에 따라 예약 발행).

        photo_url/video_url은 Buffer 서버가 접근 가능한 공개 URL이어야 한다.
        """
        data: dict = {
            "access_token": self.access_token,
            "profile_ids[]": self.profile_id,
            "text": text,
            "shorten": "false",
        }
        if photo_url:
            data["media[photo]"] = photo_url
            data["media[thumbnail]"] = photo_url
        elif video_url:
            data["media[link]"] = video_url

        async with httpx.AsyncClient(timeout=60) as client:
            res = await client.post(f"{BUFFER_API_BASE}/updates/create.json", data=data)

        body = res.json()
        if res.status_code != 200 or not body.get("success", False):
            message = body.get("message", res.text)
            logger.error("Buffer 발행 실패: %s", message)
            raise BufferError(f"Buffer API 오류: {message}")

        update = body["updates"][0]
        logger.info("Buffer 큐 등록 완료: update_id=%s", update.get("id"))
        return update
