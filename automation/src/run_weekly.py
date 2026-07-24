"""
매주 한 번 실행하는 오케스트레이터:  글 생성 → 네이버 임시저장까지 한 번에.

    python src/run_weekly.py

예약 실행(Windows 작업 스케줄러 / mac·리눅스 cron)에 이 명령을 걸어두면,
켜둔 PC 가 알아서 이번 주 초안들을 만들어 임시저장까지 해둡니다.
사장님은 네이버 '임시저장 목록'에서 사진만 넣고 발행하면 됩니다.
"""

from __future__ import annotations

import sys

import generate_post
import naver_autodraft


def main() -> None:
    print("=" * 50)
    print(" 베어글스 주간 블로그 자동화 시작")
    print("=" * 50)

    # 1) 글 생성
    print("\n[1/2] 글 초안 생성")
    try:
        generate_post.main()
    except SystemExit as e:
        print(e); sys.exit(1)

    # 2) 네이버 임시저장
    print("\n[2/2] 네이버 임시저장")
    cfg = naver_autodraft.load_config()
    headful = bool(cfg.get("naver", {}).get("headful", True))

    class _A:  # naver_autodraft.load_posts 가 기대하는 인자 형태
        post = None
        all = True

    posts = naver_autodraft.load_posts(_A())
    pw, ctx, page = naver_autodraft.launch(cfg, headful=headful)
    ok = 0
    try:
        for post in posts:
            if naver_autodraft.draft_one(page, cfg, post):
                ok += 1
    finally:
        ctx.close(); pw.stop()

    print(f"\n끝! {ok}/{len(posts)} 건 임시저장 완료.")
    print("네이버 블로그 > 글쓰기 > 임시저장 목록에서 사진 넣고 발행하세요 🐻")


if __name__ == "__main__":
    main()
