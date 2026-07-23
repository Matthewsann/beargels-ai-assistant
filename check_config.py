"""설정 점검 스크립트.

.env 값과 각 서비스 연결을 하나씩 확인하고 ✅/❌로 알려준다.
봇을 실행하기 전에 무엇이 준비됐고 무엇이 빠졌는지 파악하는 용도.

    python check_config.py
"""

import os

from dotenv import load_dotenv

load_dotenv()

OK = "✅"
NO = "❌"
WARN = "⚠️"


def _print(mark: str, msg: str) -> None:
    print(f"{mark} {msg}")


def check_env() -> dict:
    """필수 환경변수 존재 여부."""
    print("\n[1/5] .env 값 확인")
    required = {
        "ANTHROPIC_API_KEY": "Claude API 키",
        "TELEGRAM_BOT_TOKEN": "텔레그램 봇 토큰",
        "TELEGRAM_CHAT_ID": "텔레그램 채팅 ID",
        "SUPABASE_URL": "Supabase 주소",
        "SUPABASE_KEY": "Supabase 키",
        "GOOGLE_DRIVE_FOLDER_ID": "구글 드라이브 폴더 ID",
        "BUFFER_ACCESS_TOKEN": "Buffer 토큰",
        "INSTAGRAM_PROFILE_ID": "인스타그램 프로필 ID",
    }
    values = {}
    for key, label in required.items():
        val = os.getenv(key, "").strip()
        values[key] = val
        if val:
            _print(OK, f"{label} ({key}) 입력됨")
        else:
            _print(NO, f"{label} ({key}) 비어 있음 — .env에 채워주세요")
    return values


def check_files() -> None:
    print("\n[2/5] 구글 인증 파일 확인")
    client_file = os.getenv("GOOGLE_OAUTH_CLIENT_FILE", "client_secret.json")
    token_file = os.getenv("GOOGLE_OAUTH_TOKEN_FILE", "token.json")
    if os.path.exists(client_file):
        _print(OK, f"OAuth 클라이언트 파일 있음 ({client_file})")
    else:
        _print(NO, f"OAuth 클라이언트 파일 없음 ({client_file}) — 구글 클라우드에서 받아 넣으세요")
    if os.path.exists(token_file):
        _print(OK, f"로그인 토큰 있음 ({token_file})")
    else:
        _print(WARN, f"로그인 토큰 없음 ({token_file}) — 3_google_login.bat 을 먼저 실행하세요")


def check_telegram(token: str, chat_id: str) -> None:
    print("\n[3/5] 텔레그램 연결 확인")
    if not token:
        _print(NO, "봇 토큰이 없어 건너뜀")
        return
    try:
        import httpx

        res = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15)
        data = res.json()
        if data.get("ok"):
            name = data["result"].get("username", "?")
            _print(OK, f"봇 연결 성공 (@{name})")
        else:
            _print(NO, f"봇 토큰이 잘못됐습니다: {data.get('description')}")
    except Exception as e:
        _print(NO, f"텔레그램 연결 실패: {e}")


def check_supabase(url: str, key: str) -> None:
    print("\n[4/5] Supabase 연결 확인")
    if not url or not key:
        _print(NO, "Supabase 주소/키가 없어 건너뜀")
        return
    try:
        from supabase import create_client

        client = create_client(url, key)
        client.table("sns_posts").select("id").limit(1).execute()
        _print(OK, "Supabase 연결 및 sns_posts 테이블 확인 성공")
    except Exception as e:
        msg = str(e)
        if "sns_posts" in msg or "relation" in msg.lower():
            _print(NO, "sns_posts 테이블이 없습니다 — SQL 마이그레이션을 실행했는지 확인하세요")
        else:
            _print(NO, f"Supabase 연결 실패: {e}")


def check_drive() -> None:
    print("\n[5/5] 구글 드라이브 연결 확인")
    token_file = os.getenv("GOOGLE_OAUTH_TOKEN_FILE", "token.json")
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()
    if not os.path.exists(token_file):
        _print(WARN, "로그인 토큰이 없어 건너뜀 (3_google_login.bat 먼저 실행)")
        return
    if not folder_id:
        _print(NO, "폴더 ID가 없어 건너뜀")
        return
    try:
        from sns_automation.drive_monitor import DriveClient

        drive = DriveClient(token_file, folder_id)
        files = drive.list_media_files()
        _print(OK, f"드라이브 폴더 접근 성공 — 현재 사진/영상 {len(files)}개")
    except Exception as e:
        _print(NO, f"드라이브 연결 실패: {e}")


def main() -> None:
    print("=" * 44)
    print("  베어글스 SNS 봇 - 설정 점검")
    print("=" * 44)
    values = check_env()
    check_files()
    check_telegram(values.get("TELEGRAM_BOT_TOKEN", ""), values.get("TELEGRAM_CHAT_ID", ""))
    check_supabase(values.get("SUPABASE_URL", ""), values.get("SUPABASE_KEY", ""))
    check_drive()
    print("\n" + "=" * 44)
    print("모두 ✅ 이면 4_run_bot.bat 으로 봇을 실행하세요.")
    print("❌ 가 있으면 해당 항목을 먼저 해결하세요.")
    print("=" * 44)


if __name__ == "__main__":
    main()
