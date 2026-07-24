"""네이버 로그인 도우미 — 터미널 Enter 없이 '로그인 완료'를 자동 감지해 세션 저장.

원래 naver_autodraft.py --login 은 터미널 input() 으로 대기하지만,
비대화형 실행 환경에서는 그 Enter 를 받을 수 없어 창이 바로 닫힌다.
이 도우미는 로그인 성공(네이버 인증 쿠키 생성)을 폴링으로 감지해 자동 종료한다.

    python src/login_helper.py            # 최대 5분 대기
    python src/login_helper.py 600        # 최대 600초 대기
"""
from __future__ import annotations

import sys
import time

import naver_autodraft as na

AUTH_COOKIES = {"NID_AUT", "NID_SES"}


def is_logged_in(ctx) -> bool:
    try:
        names = {c["name"] for c in ctx.cookies("https://www.naver.com")}
    except Exception:
        return False
    return AUTH_COOKIES.issubset(names)


def main() -> None:
    max_sec = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 300
    cfg = na.load_config()
    pw, ctx, page = na.launch(cfg, headful=True)
    print("=" * 50)
    print(" 네이버 로그인 창이 열렸습니다.")
    print(" 크롬 창에서 직접 로그인하세요 (2차인증 포함).")
    print(" Enter 누를 필요 없이, 로그인되면 자동으로 저장/종료됩니다.")
    print("=" * 50)
    try:
        page.goto("https://nid.naver.com/nidlogin.login")
    except Exception as e:
        print(f"페이지 이동 경고: {e}")

    status_file = na.ROOT / "login_status.txt"
    state_file = na.ROOT / "naver_state.json"
    result = "TIMEOUT"
    waited = 0
    step = 3
    try:
        while waited < max_sec:
            if is_logged_in(ctx):
                # 세션 쿠키까지 포함해 파일로 저장(디스크 미보존 문제 우회).
                try:
                    ctx.storage_state(path=str(state_file))
                    print(f"  · 세션 쿠키를 {state_file.name} 에 저장했습니다.")
                except Exception as e:
                    print(f"  · 세션 저장 경고: {e}")
                print(f"\n✅ 로그인 감지! ({waited}초)")
                print("이제 발행 자동화(schedule_publish.py)를 쓸 수 있습니다.")
                result = "OK"
                break
            time.sleep(step)
            waited += step
            if waited % 30 == 0:
                print(f"  … 로그인 대기 중 ({waited}/{max_sec}초)")
        else:
            print(f"\n⏱ {max_sec}초 내 로그인이 감지되지 않았습니다. 다시 실행해 주세요.")
    finally:
        # persistent context 는 종료 시 user_data_dir 에 세션을 저장한다.
        ctx.close()
        pw.stop()
        try:
            status_file.write_text(result, encoding="utf-8")
        except Exception:
            pass


if __name__ == "__main__":
    main()
