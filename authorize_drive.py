"""구글 드라이브 최초 1회 로그인 스크립트.

실행하면 브라우저가 열립니다. 구글 계정으로 로그인하고 '허용'을 누르면
token.json 파일이 생성되고, 이후 봇은 이 파일로 자동 로그인합니다.

    python authorize_drive.py

토큰이 만료돼 봇이 "다시 인증하세요"라고 하면 이 스크립트를 다시 실행하세요.
"""

import os

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/drive"]


def main() -> None:
    client_file = os.getenv("GOOGLE_OAUTH_CLIENT_FILE", "client_secret.json")
    token_file = os.getenv("GOOGLE_OAUTH_TOKEN_FILE", "token.json")

    if not os.path.exists(client_file):
        raise SystemExit(
            f"OAuth 클라이언트 파일이 없습니다: {client_file}\n"
            "구글 클라우드에서 'OAuth 클라이언트 ID(데스크톱 앱)'를 만들고\n"
            f"다운로드한 JSON 파일을 '{client_file}' 이름으로 이 폴더에 저장하세요."
        )

    flow = InstalledAppFlow.from_client_secrets_file(client_file, SCOPES)
    # 브라우저가 자동으로 열립니다. 열리지 않으면 터미널에 뜬 URL을 직접 여세요.
    creds = flow.run_local_server(port=0)

    with open(token_file, "w") as f:
        f.write(creds.to_json())

    print(f"\n✅ 인증 완료! '{token_file}' 파일이 만들어졌습니다.")
    print("이제 `python main.py`로 봇을 실행할 수 있습니다.")


if __name__ == "__main__":
    main()
