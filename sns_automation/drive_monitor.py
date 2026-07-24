"""Google Drive 폴더 모니터링 (OAuth 로그인 + 폴링)."""

import io
import logging
import os

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]

IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
VIDEO_MIME_PREFIX = "video/"


def load_oauth_credentials(token_file: str) -> Credentials:
    """authorize_drive.py로 저장해둔 token.json을 읽고, 만료됐으면 자동 갱신한다."""
    if not os.path.exists(token_file):
        raise RuntimeError(
            f"인증 파일이 없습니다: {token_file}\n"
            "먼저 `python authorize_drive.py`를 실행해 구글 로그인을 완료하세요."
        )
    creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(token_file, "w") as f:
                f.write(creds.to_json())
            logger.info("구글 액세스 토큰을 갱신했습니다.")
        else:
            raise RuntimeError(
                "구글 인증이 만료됐습니다. `python authorize_drive.py`를 다시 실행하세요."
            )
    return creds


class DriveClient:
    def __init__(self, token_file: str, folder_id: str):
        credentials = load_oauth_credentials(token_file)
        self.service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        self.folder_id = folder_id

    def list_media_files(self, folder_id: str | None = None) -> list[dict]:
        """폴더 안의 이미지/영상 파일 목록 (오래된 것부터).

        folder_id 미지정 시 self.folder_id(콘텐츠 루트) 기준.
        """
        target = folder_id or self.folder_id
        query = (
            f"'{target}' in parents"
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

    def list_topic_folders(self) -> list[dict]:
        """콘텐츠 루트(self.folder_id) 바로 아래의 주제 폴더 목록 (이름순)."""
        query = (
            f"'{self.folder_id}' in parents"
            " and trashed = false"
            " and mimeType = 'application/vnd.google-apps.folder'"
        )
        folders: list[dict] = []
        page_token = None
        while True:
            res = (
                self.service.files()
                .list(
                    q=query,
                    fields="nextPageToken, files(id, name)",
                    orderBy="name",
                    pageSize=100,
                    pageToken=page_token,
                )
                .execute()
            )
            folders.extend(res.get("files", []))
            page_token = res.get("nextPageToken")
            if not page_token:
                break
        return folders

    def find_topic_folder(self, name: str) -> dict | None:
        """주제명으로 하위 폴더를 찾는다. 정확 일치 우선, 없으면 부분 일치."""
        safe = name.strip().replace("\\", "\\\\").replace("'", "\\'")
        base = (
            f"'{self.folder_id}' in parents and trashed = false"
            " and mimeType = 'application/vnd.google-apps.folder'"
        )
        for clause in (f"name = '{safe}'", f"name contains '{safe}'"):
            res = (
                self.service.files()
                .list(q=f"{base} and {clause}", fields="files(id, name)", pageSize=10)
                .execute()
            )
            files = res.get("files", [])
            if files:
                return files[0]
        return None

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
