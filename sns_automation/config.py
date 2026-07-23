"""환경변수 로드 및 설정."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    supabase_url: str
    supabase_key: str
    google_oauth_client_file: str
    google_oauth_token_file: str
    google_drive_folder_id: str
    buffer_access_token: str
    instagram_profile_id: str
    poll_interval_minutes: int


REQUIRED_VARS = [
    "ANTHROPIC_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "SUPABASE_URL",
    "SUPABASE_KEY",
    "GOOGLE_DRIVE_FOLDER_ID",
    "BUFFER_ACCESS_TOKEN",
    "INSTAGRAM_PROFILE_ID",
]


def load_settings() -> Settings:
    missing = [v for v in REQUIRED_VARS if not os.getenv(v)]
    if missing:
        raise RuntimeError(f"누락된 환경변수: {', '.join(missing)} (.env 파일을 확인하세요)")

    return Settings(
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        telegram_chat_id=os.environ["TELEGRAM_CHAT_ID"],
        supabase_url=os.environ["SUPABASE_URL"],
        supabase_key=os.environ["SUPABASE_KEY"],
        google_oauth_client_file=os.getenv("GOOGLE_OAUTH_CLIENT_FILE", "client_secret.json"),
        google_oauth_token_file=os.getenv("GOOGLE_OAUTH_TOKEN_FILE", "token.json"),
        google_drive_folder_id=os.environ["GOOGLE_DRIVE_FOLDER_ID"],
        buffer_access_token=os.environ["BUFFER_ACCESS_TOKEN"],
        instagram_profile_id=os.environ["INSTAGRAM_PROFILE_ID"],
        poll_interval_minutes=int(os.getenv("POLL_INTERVAL_MINUTES", "5")),
    )
