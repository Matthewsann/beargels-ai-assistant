"""Buffer에 연결된 인스타그램 프로필 ID 찾기.

.env의 BUFFER_ACCESS_TOKEN을 읽어 Buffer에 연결된 프로필 목록을 보여주고,
인스타그램 프로필의 ID를 알려준다. 그 값을 .env의 INSTAGRAM_PROFILE_ID에 넣으면 된다.

    python get_instagram_id.py
"""

import os

import httpx
from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    token = os.getenv("BUFFER_ACCESS_TOKEN", "").strip()
    if not token:
        raise SystemExit(".env의 BUFFER_ACCESS_TOKEN을 먼저 채운 뒤 다시 실행하세요.")

    try:
        res = httpx.get(
            "https://api.bufferapp.com/1/profiles.json",
            params={"access_token": token},
            timeout=30,
        )
    except Exception as e:
        raise SystemExit(f"Buffer 연결 실패: {e}")

    data = res.json()
    if res.status_code != 200 or not isinstance(data, list):
        raise SystemExit(f"Buffer 오류: {data}")

    print("\nBuffer에 연결된 프로필 목록:")
    instagram_id = None
    for p in data:
        service = p.get("service", "?")
        pid = p.get("id", "?")
        name = p.get("formatted_username") or p.get("service_username") or ""
        print(f"  - {service:12s} {name:20s}  id = {pid}")
        if service == "instagram" and instagram_id is None:
            instagram_id = pid

    if instagram_id:
        print(f"\n✅ 인스타그램 프로필 ID: {instagram_id}")
        print("   이 값을 .env 파일의 INSTAGRAM_PROFILE_ID= 뒤에 넣고 저장하세요.")
    else:
        print("\n⚠️ 인스타그램 프로필이 목록에 없습니다.")
        print("   https://publish.buffer.com 에서 인스타그램 계정을 먼저 연결하세요.")


if __name__ == "__main__":
    main()
