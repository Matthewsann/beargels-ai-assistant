"""
[2단계] 주 1회 · 창고에서 골라 '예약 발행' 걸기.

창고(라이브러리)에 ready 로 쌓인 글 목록을 보여주고, 사장님이 번호를 고르면
네이버 에디터에 글을 입력한 뒤 '예약 발행' 설정까지 진행합니다.

    python src/schedule_publish.py
        → ready 목록 표시 → 발행할 번호 입력 → 시작일/매일 시간 입력 → 자동 진행

    python src/schedule_publish.py --pick 1,3,5 --daily 08:00 --start 2026-07-28
        → 지정 항목을 하루 간격 오전 8시로 예약

발행 설정 마지막 단계(정확한 시간 지정·최종 클릭)는 네이버 화면 구조가 특히 자주 바뀌므로,
기본값은 'assist' 모드입니다:  시스템이 글 입력 + 발행창 열기 + '예약' 선택까지 하고,
사장님은 시간만 확인하고 '예약 발행'을 누르면 됩니다. (config 의 publish.mode: full 로 완전 자동 시도)
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

import naver_autodraft as na
import library


def show_ready() -> list[dict]:
    metas = library.list_items(status=library.STATUS_READY)
    if not metas:
        sys.exit("창고에 ready 상태 글이 없습니다. 먼저 generate_post.py 로 쌓아주세요.")
    print("\n📦 발행 대기(ready) 목록")
    print("-" * 60)
    for m in metas:
        print(f"  [{m['id']:>3}] ({m.get('type','')}) {m.get('title','')}")
    print("-" * 60)
    return metas


def parse_selection(pick: str, metas: list[dict]) -> list[int]:
    valid = {m["id"] for m in metas}
    ids = []
    for tok in pick.replace(" ", "").split(","):
        if tok.isdigit() and int(tok) in valid:
            ids.append(int(tok))
        else:
            print(f"  ⚠ 무시: '{tok}' (목록에 없음)")
    return ids


def build_schedule(ids: list[int], daily: str, start: str, at: str | None) -> list[dt.datetime]:
    if at:
        times = []
        for s in at.split(","):
            times.append(dt.datetime.strptime(s.strip(), "%Y-%m-%d %H:%M"))
        return times[: len(ids)]
    # daily + start: 하루 간격 배치
    start_date = dt.datetime.strptime(start, "%Y-%m-%d").date()
    hh, mm = map(int, daily.split(":"))
    return [dt.datetime.combine(start_date + dt.timedelta(days=i), dt.time(hh, mm))
            for i in range(len(ids))]


def reserve_publish(page, frame, cfg, when: dt.datetime) -> bool:
    """발행 설정 레이어를 열고 '예약'을 선택한다. mode=full 이면 최종 클릭까지 시도."""
    sel = cfg["_selectors"]
    mode = cfg.get("publish", {}).get("mode", "assist")

    open_btn = na.first_working(frame, sel["publish_open"])
    if not open_btn:
        na.save_debug(page, "no_publish_btn")
        print("    ✗ 발행 버튼을 찾지 못했습니다. --inspect 로 확인 후 selectors.publish_open 조정.")
        return False
    open_btn.click()
    frame.wait_for_timeout(1200)

    radio = na.first_working(frame, sel["reserve_radio"])
    if radio:
        try:
            radio.click()
            frame.wait_for_timeout(600)
        except Exception:
            pass

    # 날짜 입력 시도(가능한 경우)
    date_loc = na.first_working(frame, sel["reserve_date"])
    if date_loc:
        try:
            date_loc.fill(when.strftime("%Y.%m.%d"))
        except Exception:
            pass

    print(f"    🕒 예약 목표: {when.strftime('%Y-%m-%d %H:%M')}")

    if mode == "full":
        confirm = na.first_working(frame, sel["reserve_confirm"])
        if confirm:
            try:
                confirm.click()
                frame.wait_for_timeout(1500)
                print("    ✓ 예약 발행 완료(full)")
                return True
            except Exception as e:
                na.save_debug(page, "reserve_confirm_fail")
                print(f"    full 모드 최종 클릭 실패 → assist 로 전환: {e}")

    # assist: 사장님이 시간 확인 후 직접 '예약 발행' 클릭
    print("    ▶ 발행창에서 시간을 확인/조정하고 '예약 발행'을 눌러주세요.")
    input("      완료하면 Enter … ")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="창고에서 골라 예약 발행")
    ap.add_argument("--pick", help="발행할 id 목록 (예: 1,3,5)")
    ap.add_argument("--daily", default="08:00", help="매일 발행 시간 HH:MM (기본 08:00)")
    ap.add_argument("--start", help="예약 시작일 YYYY-MM-DD")
    ap.add_argument("--at", help="개별 지정: 'YYYY-MM-DD HH:MM,YYYY-MM-DD HH:MM' (pick 순서대로)")
    args = ap.parse_args()

    cfg = na.load_config()
    metas = show_ready()

    pick = args.pick or input("\n발행할 번호(쉼표로, 예: 1,3,5): ").strip()
    ids = parse_selection(pick, metas)
    if not ids:
        sys.exit("선택된 항목이 없습니다.")

    if not args.at:
        start = args.start or input("예약 시작일 (YYYY-MM-DD): ").strip()
        daily = args.daily
        if not args.pick:  # 대화형이면 시간도 물어봄
            daily = (input(f"매일 발행 시간 HH:MM [{daily}]: ").strip() or daily)
        schedule = build_schedule(ids, daily, start, None)
    else:
        schedule = build_schedule(ids, args.daily, args.start or "", args.at)

    print(f"\n총 {len(ids)}건 예약 진행:")
    for i, when in zip(ids, schedule):
        m = library.load_meta(i)
        print(f"  #{i:04d} → {when.strftime('%Y-%m-%d %H:%M')}  | {m.get('title','')}")

    headful = bool(cfg.get("naver", {}).get("headful", True))
    pw, ctx, page = na.launch(cfg, headful=headful)
    done = 0
    try:
        for i, when in zip(ids, schedule):
            post = library.load_post(i)
            frame = na.fill_editor(page, cfg, post)
            if frame is None:
                continue
            if reserve_publish(page, frame, cfg, when):
                library.set_status(i, library.STATUS_SCHEDULED,
                                   scheduled_time=when.strftime("%Y-%m-%d %H:%M"))
                done += 1
    finally:
        ctx.close(); pw.stop()

    print(f"\n끝! {done}/{len(ids)}건 예약 처리(status=scheduled). 남은 창고는 다음 주에 또 고르세요 🐻")


if __name__ == "__main__":
    main()
