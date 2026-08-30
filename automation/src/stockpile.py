"""
[1단계] 자동으로 계속 콘텐츠를 쌓는 러너 (본문 + 이미지/촬영목록).

예약 실행(작업 스케줄러/cron)에 걸어두면, 켜둔 PC가 알아서 창고를 채웁니다.
발행은 하지 않습니다 — 오직 생성/적재만. 발행·예약은 사람이 네이버에서 직접.

    python src/stockpile.py            # config 의 stockpile.per_run 개수만큼 쌓기
    python src/stockpile.py 5          # 5개 쌓기
    python src/stockpile.py --plan     # content-plan.yaml 의 지정 글(신메뉴/이벤트)도 함께
"""

from __future__ import annotations

import sys

from anthropic import Anthropic

import generate_post
import generate_image
import library


def main() -> None:
    raise SystemExit(
        "퇴역한 스크립트입니다 — 글 생성은 웹 기획실(무료 Gemini)을 쓰세요. "
        "(유료 Claude 직접 호출 차단, 2026-08-30 비용 감사)")
    cfg = generate_post.load_config()
    per_run = int(cfg.get("stockpile", {}).get("per_run", 3))
    do_plan = "--plan" in sys.argv
    nums = [a for a in sys.argv[1:] if a.isdigit()]
    count = int(nums[0]) if nums else per_run

    print("=" * 50)
    print(" 베어글스 콘텐츠 자동 적재 (1단계)")
    print("=" * 50)

    client = Anthropic()
    made: list[int] = []

    if do_plan:
        print("\n[content-plan.yaml 지정 글]")
        made += generate_post.run_plan(client, cfg)

    print(f"\n[상시 소재 {count}개 쌓기]")
    made += generate_post.run_stockpile(client, cfg, count)

    print("\n[이미지 계획/생성 + 촬영목록]")
    for item_id in made:
        try:
            generate_image.process_item(cfg, item_id)
        except Exception as e:
            print(f"  #{item_id:04d} 이미지 단계 건너뜀: {e}")

    ready = library.list_items(status=library.STATUS_READY)
    print(f"\n완료! 이번에 {len(made)}개 적재. 창고 총 발행대기: {len(ready)}개")
    print("발행·예약은 네이버에서 직접 골라서 해주세요 🐻")


if __name__ == "__main__":
    main()
