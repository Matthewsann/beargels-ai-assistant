"""Google Drive 폴더 모니터링 (서비스 계정 + 폴링)."""

import io
import logging

import httpx
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]

IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
VIDEO_MIME_PREFIX = "video/"


class DriveClient:
    def __init__(self, service_account_file: str, folder_id: str):
        credentials = service_account.Credentials.from_service_account_file(
            service_account_file, scopes=SCOPES
        )
        self.service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        self.folder_id = folder_id

    def list_media_files(self) -> list[dict]:
        """폴더 안의 이미지/영상 파일 목록 (오래된 것부터)."""
        query = (
            f"'{self.folder_id}' in parents"
            " and trashed = false"
            " and (mimeType contains 'image/' or mimeType contains 'video/')"
        )
        files: list[dict] = []
        page_token = None
        while True:
            res = (
                self.service.files()
                .list(
                    q=query,
                    fields="nextPageToken, files(id, name, mimeType, thumbnailLink, createdTime)",
                    orderBy="createdTime",
                    pageSize=100,
                    pageToken=page_token,
                )
                .execute()
            )
            files.extend(res.get("files", []))
            page_token = res.get("nextPageToken")
            if not page_token:
                break
        return files

    def get_file(self, file_id: str) -> dict:
        return (
            self.service.files()
            .get(fileId=file_id, fields="id, name, mimeType, thumbnailLink")
            .execute()
        )

    def download_file(self, file_id: str) -> bytes:
        request = self.service.files().get_media(fileId=file_id)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buffer.getvalue()

    def download_thumbnail(self, file: dict) -> bytes | None:
        """영상 파일용: Drive가 생성한 썸네일 이미지를 받아온다."""
        link = file.get("thumbnailLink")
        if not link:
            return None
        # 썸네일 크기를 키운다 (기본 s220 → s1024)
        link = link.rsplit("=", 1)[0] + "=s1024"
        try:
            res = httpx.get(link, timeout=30)
            res.raise_for_status()
            return res.content
        except httpx.HTTPError as e:
            logger.warning("썸네일 다운로드 실패 (%s): %s", file.get("name"), e)
            return None

    def make_public(self, file_id: str) -> str:
        """파일을 링크 공개로 전환하고 Buffer가 가져갈 수 있는 직접 URL을 반환."""
        self.service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
        ).execute()
        return f"https://drive.google.com/uc?export=download&id={file_id}"
