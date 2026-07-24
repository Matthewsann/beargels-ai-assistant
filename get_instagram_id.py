"""Buffer에 연결된 인스타그램 채널 ID 자동 찾기.

.env의 BUFFER_ACCESS_TOKEN을 읽어 Buffer에 연결된 채널 목록을 보여주고,
인스타그램 채널 ID를 .env의 BUFFER_CHANNEL_ID에 자동으로 채워준다.

    python get_instagram_id.py
"""

import io
import re
import sys

import httpx
from dotenv import dotenv_values

# 한글 윈도우(cp949) 콘솔에서 ✅/❌ 이모지가 깨지지 않도록 UTF-8로 출력
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

GRAPHQL_URL = "https://api.buffer.com/graphql"


def _graphql(token: str, query: str, variables: dict | None = None) -> dict:
    res = httpx.post(
        GRAPHQL_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={"query": query, "variables": variables or {}},
        timeout=30,
    )
    body = res.json()
    if res.status_code != 200 or body.get("errors"):
        errors = body.get("errors") or [{"message": res.text}]
        raise SystemExit(f"Buffer 오류: {errors[0].get('message')}")
    return body["data"]


def main() -> None:
    env = dotenv_values(".env")
    token = (env.get("BUFFER_ACCESS_TOKEN") or "").strip()
    if not token:
        raise SystemExit(".env의 BUFFER_ACCESS_TOKEN을 먼저 채운 뒤 다시 실행하세요.")

    account = _graphql(token, "{ account { email organizations { id } } }")["account"]
    print(f"\nBuffer 계정: {account['email']}")

    org_id = account["organizations"][0]["id"]
    channels = _graphql(
        token,
        "query C($o: OrganizationId!) { channels(input: { organizationId: $o })"
        " { id service name isDisconnected } }",
        {"o": org_id},
    )["channels"]

    print("연결된 채널 목록:")
    instagram_id = None
    for c in channels:
        state = " (연결 끊김!)" if c["isDisconnected"] else ""
        print(f"  - {c['service']:12s} {c['name']:20s} id = {c['id']}{state}")
        if c["service"] == "instagram" and not c["isDisconnected"] and instagram_id is None:
            instagram_id = c["id"]

    if not instagram_id:
        print("\n⚠️ 인스타그램 채널이 없습니다.")
        print("   buffer.com 에서 인스타그램 계정을 먼저 연결하세요 (Channels → Connect).")
        return

    print(f"\n✅ 인스타그램 채널 ID: {instagram_id}")

    # .env에 자동 반영
    text = io.open(".env", encoding="utf-8").read()
    new_text = re.sub(
        r"^BUFFER_CHANNEL_ID=.*$", f"BUFFER_CHANNEL_ID={instagram_id}", text, flags=re.M
    )
    if new_text != text:
        io.open(".env", "w", encoding="utf-8").write(new_text)
        print("   .env의 BUFFER_CHANNEL_ID에 자동으로 채워 넣었습니다.")
    else:
        print("   .env 값이 이미 최신입니다.")


if __name__ == "__main__":
    main()
