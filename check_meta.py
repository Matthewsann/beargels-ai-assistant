"""메타 그래프 API 연결 점검.

    python check_meta.py

토큰이 제대로 됐는지, 인스타 계정을 찾는지, 해시태그 검색이 되는지 순서대로 본다.
문제가 있으면 뭘 고쳐야 하는지 알려준다.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

load_dotenv()

from sns_automation.meta_graph import MetaGraphError, from_env  # noqa: E402

OK, NO = "[O]", "[X]"


def main() -> int:
    print("\n메타 그래프 API 점검\n" + "=" * 42)

    # 1) 토큰
    if not os.getenv("META_ACCESS_TOKEN", "").strip():
        print(f"{NO} META_ACCESS_TOKEN 없음")
        print("    .env 에 추가하세요. 발급 방법은 docs/meta_graph_setup.md")
        return 1
    print(f"{OK} META_ACCESS_TOKEN 있음")
    api = from_env()

    # 2) 인스타 계정
    try:
        uid = api.resolve_ig_user_id()
        print(f"{OK} 인스타 비즈니스 계정 찾음 (IG_USER_ID={uid})")
        if not os.getenv("IG_USER_ID", "").strip():
            print(f"    .env 에 IG_USER_ID={uid} 를 넣어두면 다음부터 빨라집니다")
    except MetaGraphError as e:
        print(f"{NO} 인스타 계정 확인 실패\n    {e}")
        return 1

    # 3) 프로필
    try:
        me = api.me()
        print(f"{OK} @{me.get('username')} · 팔로워 {me.get('followers_count')}명 "
              f"· 게시물 {me.get('media_count')}개")
    except MetaGraphError as e:
        print(f"{NO} 프로필 조회 실패\n    {e}")

    # 4) 해시태그 검색 (7일 30개 한도라 1개만 테스트)
    try:
        posts = api.hashtag_media("송도카페", top=True, limit=3)
        print(f"{OK} 해시태그 검색 작동 — #송도카페 인기글 {len(posts)}개")
        for p in posts[:3]:
            cap = (p.get("caption") or "").splitlines()
            head = cap[0][:40] if cap else "(캡션 없음)"
            print(f"    ♥{p.get('like_count') or 0:>5}  {head}")
    except MetaGraphError as e:
        print(f"{NO} 해시태그 검색 실패\n    {e}")
        return 1

    # 5) 내 게시물 인사이트
    try:
        mine = api.my_media(limit=1)
        if mine:
            ins = api.media_insights(mine[0]["id"])
            got = {k: v for k, v in ins.items() if v is not None}
            print(f"{OK} 내 게시물 인사이트 조회됨 → {got or '(지표 없음)'}")
        else:
            print("    (게시물이 없어 인사이트는 건너뜀)")
    except MetaGraphError as e:
        print(f"    인사이트 조회 실패(핵심 기능엔 무관): {e}")

    print("\n전부 통과. 이제 아래로 시장 스캔을 돌릴 수 있습니다:")
    print("    python -m sns_automation.market_scan\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
