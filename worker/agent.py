"""집 PC 일꾼 — 직원이 웹에서 누른 '리뷰수집' 요청을 대신 처리한다.

왜 필요한가:
    배민·쿠팡은 사장님 계정으로 **로그인된 브라우저**가 있어야 리뷰를 볼 수 있다.
    그 브라우저는 집 PC 에만 있으므로, 클라우드 웹앱은 크롤링을 할 수 없다.
    그래서 집 PC 가 Supabase 를 주기적으로 확인해 "수집 요청이 있으면" 대신
    긁어와 답글 초안까지 만들어 DB 에 넣어준다.

흐름:
    [직원 웹앱] --요청--> [Supabase jobs] <--확인-- [이 프로그램(집 PC)]
                                                        |
                              배민·쿠팡 크롤링 → 답글 초안 생성 → reviews 저장

안전:
    · 집 PC 로 들어오는 연결이 없다(밖으로 나가기만 함) → 방화벽·터널 불필요.
    · 답글을 **게시하지 않는다**. 초안만 만든다. 게시는 직원이 복사해서 직접.
    · 민감(에스컬레이션) 리뷰는 초안 대신 '직접 대응 필요' 문구가 저장된다.

실행: worker\run_agent.bat  (또는 python worker/agent.py)
중지: 창에서 Ctrl+C
"""
from __future__ import annotations

import logging
import os
import pathlib
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assistant.beargels import (  # noqa: E402
    classify_review, generate_review_reply, order_count_of,
)
from assistant.meeting_ai import MeetingAIUnavailable, organize as ai_organize  # noqa: E402
from alerts import notify_owner  # noqa: E402
from database import meeting_store  # noqa: E402
from database import supabase_client as db  # noqa: E402

logger = logging.getLogger("worker")

POLL_SECONDS = int(os.getenv("WORKER_POLL_SECONDS", "15"))
# 빠른 박자 — 직원이 화면 앞에서 기다리는 잡(등록 등)을 살피는 주기.
# 15초에서 1.5초로: 실측(2026-08-29)에서 등록 1건당 큐 대기가 15.3초로
# 실행(11.7초)보다 길었다. main() 의 '두 박자 루프' 주석 참고.
FAST_POLL_SECONDS = float(os.getenv("WORKER_FAST_POLL_SECONDS", "1.5"))
COUPANG_DAYS = int(os.getenv("WORKER_COUPANG_DAYS", "14"))
BAEMIN_SCROLL = int(os.getenv("WORKER_BAEMIN_SCROLL", "3"))
MAX_DRAFTS_PER_RUN = int(os.getenv("WORKER_MAX_DRAFTS", "20"))


# ---------------------------------------------------------------------------
# 수집 + 초안 생성
# ---------------------------------------------------------------------------

CHROME_BAT = ROOT / "scripts" / "launch_chrome.bat"
CDP_URL = "http://127.0.0.1:{}/json/version"


def cdp_alive(port=None, timeout=2.0) -> bool:
    """크롤링용 Chrome(원격 디버깅)이 살아있는지 확인한다."""
    import urllib.request
    port = port or os.getenv("CDP_PORT", "9222")
    try:
        with urllib.request.urlopen(CDP_URL.format(port), timeout=timeout):
            return True
    except Exception:  # noqa: BLE001
        return False


def ensure_chrome(wait_seconds=60) -> bool:
    """크롤링용 Chrome 이 꺼져 있으면 직접 켜고, 뜰 때까지 기다린다.

    사장님이 매번 launch_chrome.bat 을 눌러야 하는 걸 없애기 위함. 로그인
    세션은 전용 프로필(.browser_profile)에 남아 있어 다시 켜도 유지된다.

    ⚠️ 로그인 자체가 만료된 경우는 여기서 해결할 수 없다 — 크롤링 단계에서
       SessionExpiredError 로 잡혀 화면에 사유가 표시된다.

    Returns: 최종적으로 Chrome 이 붙을 수 있는 상태면 True.
    """
    if cdp_alive():
        return True
    if not CHROME_BAT.exists():
        logger.warning("launch_chrome.bat 을 찾을 수 없음: %s", CHROME_BAT)
        return False

    logger.info("크롤링용 Chrome 이 꺼져 있어 직접 켭니다...")
    db.worker_ping("working", "크롬 켜는 중")
    try:
        # ⚠️ 콘솔 창을 새로 띄우지 않는다(CREATE_NO_WINDOW) — 예전엔
        # CREATE_NEW_CONSOLE 이라 일꾼 창 옆에 이 배치파일용 cmd 창이
        # 하나 더 떴다(사장님 보고 2026-08-30, "cmd 두개 뜨는데 뭐야").
        # 크롬 자체는 콘솔이 필요 없어서 숨겨도 로그인용 크롬 창은 그대로
        # 뜬다 — 없어지는 건 그 크롬을 실행만 하고 마는 껍데기 cmd 뿐이다.
        subprocess.Popen(
            [str(CHROME_BAT)], cwd=str(CHROME_BAT.parent),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Chrome 실행 실패: %s", e)
        db.log_error("worker", f"Chrome 자동 실행 실패: {e}",
                     kind=type(e).__name__, path="ensure_chrome",
                     detail=traceback.format_exc())
        return False

    for _ in range(int(wait_seconds / 2)):
        time.sleep(2)
        if cdp_alive():
            logger.info("Chrome 이 준비됐습니다.")
            return True
    logger.warning("Chrome 을 켰지만 %d초 안에 준비되지 않았습니다.", wait_seconds)
    return False


def _profile_chrome_pids():
    """전용 프로필(.browser_profile)로 띄운 chrome.exe 의 PID 목록.

    사장님이 평소 쓰는 다른 Chrome 은 건드리면 안 되므로, 명령줄에 우리
    프로필 경로가 들어 있는 프로세스만 고른다.
    """
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
          "Where-Object { $_.CommandLine -like '*.browser_profile*' } | "
          "Select-Object -ExpandProperty ProcessId")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:  # noqa: BLE001
        return []
    return [int(x) for x in out.split() if x.strip().isdigit()]


def restart_chrome(reason="") -> bool:
    """먹통이 된 크롤링용 Chrome 을 껐다 켠다.

    ⚠️ 왜 필요한가: CDP 포트(9222)는 HTTP 응답을 계속 주는데 정작 붙지는
       못하는 '반쯤 죽은' 상태가 된다. cdp_alive() 는 HTTP 만 보므로 살아
       있다고 판단했고, 수집이 몇 시간째 조용히 실패했다(2026-08-21, 탭이
       76개까지 쌓여 Chrome 이 마비된 뒤). 그래서 붙기에 실패하면 여기서
       프로세스를 정리하고 다시 띄운다. 로그인 세션은 프로필에 남는다.
    """
    if os.getenv("WORKER_CHROME_AUTORESTART", "true").lower() == "false":
        return False
    pids = _profile_chrome_pids()
    logger.warning("크롬이 응답하지 않아 재시작합니다(%s, 프로세스 %d개)",
                   reason or "attach 실패", len(pids))
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=20)
        except Exception:  # noqa: BLE001
            continue
    time.sleep(3)
    ok = ensure_chrome()
    notify_owner(
        "크롬이 응답하지 않아 자동으로 껐다 켰어요. "
        + ("수집을 이어서 진행합니다." if ok
           else "다시 켜지지 않았어요 — 집 PC 에서 launch_chrome.bat 을 실행해 주세요."),
        kind="Notice", source="worker")
    return ok


def _is_browser_gone(e) -> bool:
    """브라우저/탭이 도중에 끊긴 오류인지 — 다시 붙으면 대개 그냥 된다.

    실제 사례(2026-08-25): 답글 등록 #557 이
    "Page.query_selector: Target page, context or browser has been closed" 로
    실패했는데, 직원이 다시 누른 #558 은 4초 만에 성공했다. 붙었다 끊었다를
    반복하는 CDP 연결 특성상 가끔 난다 — 사람에게 다시 누르게 하지 말고
    코드가 한 번 더 시도한다.
    """
    m = str(e)
    return ("has been closed" in m or "Target closed" in m
            or "TargetClosedError" in type(e).__name__
            or "browser has been closed" in m)


def _is_attach_failure(e) -> bool:
    """크롬에 붙지 못해서 난 오류인지 — 재시작으로 고칠 수 있는 종류."""
    return "CDP attach 실패" in str(e) or "connect_over_cdp" in str(e)


# '전체 수집' 범위 — 평소 수집(최근분)과 달리 남아 있는 리뷰를 끝까지 긁는다.
# 크롤러가 리뷰 소진 시 스스로 멈추므로 상한만 넉넉히 준다.
FULL_COUPANG_DAYS = int(os.getenv("WORKER_FULL_COUPANG_DAYS", "1095"))
FULL_COUPANG_PAGES = int(os.getenv("WORKER_FULL_COUPANG_PAGES", "300"))
FULL_BAEMIN_SCROLL = int(os.getenv("WORKER_FULL_BAEMIN_SCROLL", "300"))


def collect_reviews(full=False) -> tuple[int, list[str]]:
    """배민·쿠팡 리뷰를 긁어 DB 에 저장한다. (저장 건수, 경고 메시지들)

    Args:
        full: True 면 최근분이 아니라 **남아 있는 전체 리뷰**를 수집한다
              (전체 리뷰 관리 화면용. 수 분~수십 분 걸릴 수 있다).

    한쪽 플랫폼이 실패해도 다른 쪽은 계속한다(로그인 만료 등).
    """
    saved, warnings = 0, []

    # 크롬이 꺼져 있으면 먼저 켠다(사장님이 손으로 켜지 않아도 되게).
    if not ensure_chrome():
        warnings.append("크롤링용 Chrome 을 켜지 못했습니다 — 집 PC 확인 필요")
        return 0, warnings

    def _baemin():
        from crawler.baemin import BaeminCrawler
        with BaeminCrawler() as c:
            return c.fetch_reviews(
                max_scroll=FULL_BAEMIN_SCROLL if full else BAEMIN_SCROLL)

    try:
        try:
            revs = _baemin()
        except Exception as e:  # noqa: BLE001
            # 크롬이 먹통이라 못 붙은 거라면 껐다 켜고 한 번만 다시 해 본다.
            # 브라우저가 도중에 끊긴 경우(탭/컨텍스트 종료)는 그냥 다시,
            # 아예 못 붙는 경우는 크롬을 껐다 켜고 다시.
            if _is_browser_gone(e):
                logger.warning("수집 중 브라우저가 끊겨 다시 시도합니다")
                time.sleep(3)
                ensure_chrome()
            elif not (_is_attach_failure(e) and restart_chrome(str(e)[:60])):
                raise
            revs = _baemin()
        saved += db.save_reviews(revs)
        logger.info("배민 리뷰 %d건 수집", len(revs))
    except Exception as e:  # noqa: BLE001 — 한쪽 실패가 전체를 막지 않게
        warnings.append(f"배민 수집 실패: {str(e)[:120]}")
        logger.warning("배민 수집 실패: %s", e)
        db.log_error("worker", f"배민 수집 실패: {e}", kind=type(e).__name__,
                     path="collect/baemin", detail=traceback.format_exc())

    def _coupang():
        from crawler.coupang import CoupangCrawler
        with CoupangCrawler() as c:
            return (c.fetch_reviews(days=FULL_COUPANG_DAYS,
                                    max_pages=FULL_COUPANG_PAGES) if full
                    else c.fetch_reviews(days=COUPANG_DAYS))

    try:
        try:
            revs = _coupang()
        except Exception as e:  # noqa: BLE001
            if _is_browser_gone(e):
                logger.warning("수집 중 브라우저가 끊겨 다시 시도합니다")
                time.sleep(3)
                ensure_chrome()
            elif not (_is_attach_failure(e) and restart_chrome(str(e)[:60])):
                raise
            revs = _coupang()
        saved += db.save_reviews(revs)
        logger.info("쿠팡 리뷰 %d건 수집", len(revs))
    except Exception as e:  # noqa: BLE001
        warnings.append(f"쿠팡 수집 실패: {str(e)[:120]}")
        logger.warning("쿠팡 수집 실패: %s", e)
        db.log_error("worker", f"쿠팡 수집 실패: {e}", kind=type(e).__name__,
                     path="collect/coupang", detail=traceback.format_exc())

    return saved, warnings


# ---------------------------------------------------------------------------
# 주문(매출) 수집 — MKT 캘린더의 '진행 중인 달' 을 살리는 유일한 수단
# ---------------------------------------------------------------------------
#
# 왜 필요한가 (2026-08-30 발견): 포스 장부(TOS 엑셀)는 사장님이 **월 1회**
# 올린다. 그래서 8월에 마케팅을 기록해도 9월 초까지 매출이 한 줄도 안 잡혀
# MKT 캘린더가 통째로 백지였다. mkt_page 에는 '장부 미반영 구간은 배달
# 크롤러 잠정치로 보완' 하는 코드가 있었지만, 정작 **그 orders 를 채우는
# 수집을 아무도 부르지 않아** 죽은 코드였다(orders 테이블 52행, 최신 7/25).
# 크롤러에는 fetch_orders 가 이미 완성돼 있으므로 여기서 부르기만 하면
# 캠페인 효과를 다음 날 아침에 볼 수 있다(피드백 30일 → 1일).
ORDER_DAYS = int(os.getenv("WORKER_ORDER_DAYS", "3"))


def collect_orders(days=None) -> tuple[int, list[str]]:
    """배민·쿠팡 주문(매출)을 긁어 orders 에 저장한다. (저장 건수, 경고들)

    리뷰 수집과 같은 안전 계약: 한쪽 플랫폼이 실패해도 다른 쪽은 계속하고,
    브라우저가 끊기거나 붙지 못하면 한 번만 되살려 재시도한다.
    """
    days = days or ORDER_DAYS
    saved, warnings = 0, []

    if not ensure_chrome():
        warnings.append("크롤링용 Chrome 을 켜지 못했습니다 — 집 PC 확인 필요")
        return 0, warnings

    def _retry_once(fn, what):
        """브라우저 끊김/attach 실패면 한 번만 되살려 재시도."""
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if _is_browser_gone(e):
                logger.warning("%s 중 브라우저가 끊겨 다시 시도합니다", what)
                time.sleep(3)
                ensure_chrome()
            elif not (_is_attach_failure(e) and restart_chrome(str(e)[:60])):
                raise
            return fn()

    def _baemin():
        from crawler.baemin import BaeminCrawler
        with BaeminCrawler() as c:
            return c.fetch_orders(
                start_date=datetime.now().date() - timedelta(days=days),
                end_date=datetime.now().date())

    def _coupang():
        from crawler.coupang import CoupangCrawler
        with CoupangCrawler() as c:
            return c.fetch_orders(days=days)

    for name, fn in (("배민", _baemin), ("쿠팡", _coupang)):
        try:
            orders = _retry_once(fn, f"{name} 주문 수집")
            saved += db.save_orders(orders)
            logger.info("%s 주문 %d건 수집", name, len(orders))
        except Exception as e:  # noqa: BLE001 — 한쪽 실패가 전체를 막지 않게
            warnings.append(f"{name} 주문 수집 실패: {str(e)[:120]}")
            logger.warning("%s 주문 수집 실패: %s", name, e)
            db.log_error("worker", f"{name} 주문 수집 실패: {e}",
                         kind=type(e).__name__, path=f"collect_orders/{name}",
                         detail=traceback.format_exc())
    return saved, warnings


# 이보다 오래된 미답변 리뷰는 답글 기한이 지나 등록할 수 없다 —
# 초안을 만들지 않고 목록에서 정리한다. 실측(2026-08-13): 쿠팡은 9일 된
# 리뷰는 등록 성공, 17일 된 리뷰는 기한만료(20051) 거절. 실제 한도보다
# 넉넉히 잡아 '아직 되는데 걸러버리는' 일이 없게 한다.
REPLY_WINDOW_DAYS = int(os.getenv("WORKER_REPLY_WINDOW_DAYS", "30"))


def _too_old_to_reply(row) -> bool:
    d = row.get("written_date")
    if not d:
        return False                      # 모르면 건드리지 않는다
    try:
        age = (datetime.now().date() - datetime.fromisoformat(d).date()).days
    except Exception:  # noqa: BLE001
        return False
    return age > REPLY_WINDOW_DAYS


def make_drafts() -> int:
    """초안이 아직 없는 미답변 리뷰에 답글 초안을 만들어 저장한다. 만든 수 반환.

    답글 기한이 지난 옛 리뷰는 초안을 만들지 않고 '넘어가기'로 정리한다 —
    전체 수집 뒤 옛 리뷰가 직원 화면을 덮고 AI 호출도 낭비됐다(2026-08-13).
    """
    made, retired = 0, 0
    for row in db.get_pending_reviews(limit=100):
        if row.get("reply_draft"):
            continue                      # 이미 초안 있음(직원이 고친 것 포함)
        if row.get("platform_replied"):
            continue                      # 플랫폼에 이미 답글이 달림
        if _too_old_to_reply(row):
            db.mark_skipped(row["id"])    # 기한 지남 → 목록에서 정리
            retired += 1
            continue
        if made >= MAX_DRAFTS_PER_RUN:
            logger.info("한 번에 %d건까지만 생성 — 나머지는 다음 수집 때",
                        MAX_DRAFTS_PER_RUN)
            break
        review = {
            "platform": row.get("platform"),
            "review_no": row.get("review_no"),
            "author": row.get("author"),
            "rating": row.get("rating"),
            "content": row.get("content"),
            "menus": row.get("menus") or [],
            "raw": row.get("raw"),  # 사진 유무 판별용(classify_review)
            # 주문 횟수는 단골·VIP 판단의 핵심 지표 — raw 에서 뽑아 넘긴다
            # (예전엔 None 으로 고정돼 38번 주문한 단골도 몰랐다).
            "order_count": order_count_of(row),
        }
        try:
            draft = generate_review_reply(review)
        except Exception as e:  # noqa: BLE001 — 한 건 실패가 전체를 막지 않게
            logger.warning("초안 생성 실패(리뷰 %s): %s", row.get("id"), e)
            db.log_error("worker", f"초안 생성 실패(리뷰 {row.get('id')}): {e}",
                         kind=type(e).__name__, path="make_drafts",
                         detail=traceback.format_exc())
            continue
        # AI 원본(ai_draft)과 유형(kind)을 함께 보존 — 직원이 고치면
        # reply_draft 만 바뀌므로, 나중에 '얼마나 고쳤나(수정률)'를 잴 수 있다.
        db.save_ai_draft(row["id"], draft, kind=classify_review(review))
        made += 1
    if retired:
        logger.info("답글 기한이 지난 옛 리뷰 %d건을 목록에서 정리했습니다", retired)
    return made


def run_blog_job(job) -> None:
    """블로그 작업 1건 처리 (글감추천·초안·네이버 임시저장·순위확인)."""
    jid, kind = job["id"], job.get("kind")
    logger.info("블로그 작업 #%s (%s) 시작", jid, kind)
    db.worker_ping("working", f"블로그 작업 중 ({kind})")
    try:
        import blog_jobs
        count, msg = blog_jobs.run(job)
        db.finish_job(jid, "done", msg, count)
        logger.info("블로그 작업 #%s 완료 — %s", jid, msg)
    except Exception as e:  # noqa: BLE001
        logger.error("블로그 작업 #%s 실패: %s", jid, e)
        logger.debug(traceback.format_exc())
        db.log_error("worker", f"블로그 작업 #{jid}({kind}) 실패: {e}",
                     kind=type(e).__name__, path="run_blog_job",
                     detail=traceback.format_exc())
        db.finish_job(jid, "error", str(e)[:400], 0)
    finally:
        db.worker_ping("idle", "대기 중")


def collect_menus() -> tuple[int, list[str]]:
    """채널(배민/쿠팡/네이버)에 노출 중인 메뉴를 긁어 스냅샷으로 저장."""
    from crawler import menu_scrape

    total, warnings = 0, []
    # 채널별로 '어떻게 끝났는지' 를 남긴다. 건수만 보면 수집 실패와
    # '채널에 진짜 그것뿐' 을 구분할 수 없어, 화면이 엉뚱한 경고를 낸다.
    status = {}
    for channel, fetch in (("baemin", menu_scrape.fetch_baemin_menus),
                           ("coupang", menu_scrape.fetch_coupang_menus),
                           ("naver", menu_scrape.fetch_naver_menus)):
        try:
            rows = fetch()
            status[channel] = {"ok": True, "count": len(rows),
                               "at": datetime.utcnow().isoformat() + "Z"}
            if channel == "naver":
                status[channel]["source"] = menu_scrape.LAST_NAVER_SOURCE
            if rows:
                total += db.save_menu_snapshots(channel, rows)
            else:
                warnings.append(f"{channel} 0건(덤프 확인)")
        except Exception as e:  # noqa: BLE001 — 채널 하나 실패해도 나머지는 진행
            status[channel] = {"ok": False, "count": 0, "error": str(e)[:120],
                               "at": datetime.utcnow().isoformat() + "Z"}
            warnings.append(f"{channel} 실패: {str(e)[:80]}")
            db.log_error("worker", f"채널 메뉴 수집 실패({channel}): {e}",
                         kind=type(e).__name__, path=f"menu_collect/{channel}",
                         detail=traceback.format_exc())
    try:
        db.menu_set_setting("collect_status", status)
    except Exception:  # noqa: BLE001 — 기록 실패가 수집을 망치면 안 된다
        pass
    return total, warnings


def run_menu_job(job) -> None:
    """채널 메뉴 수집 요청 1건 처리."""
    jid = job["id"]
    db.worker_ping("working", "채널 메뉴 수집 중")
    try:
        total, warnings = collect_menus()
        msg = f"채널 메뉴 {total}건 수집"
        if warnings:
            msg += " / " + " · ".join(warnings)
        db.finish_job(jid, "error" if total == 0 else "done", msg, total)
        logger.info("메뉴 수집 요청 #%s 완료 — %s", jid, msg)
    except Exception as e:  # noqa: BLE001
        db.log_error("worker", f"메뉴 수집 요청 #{jid} 실패: {e}",
                     kind=type(e).__name__, path="run_menu_job",
                     detail=traceback.format_exc())
        db.finish_job(jid, "error", str(e)[:400], 0)
    finally:
        db.worker_ping("idle", "대기 중")


def run_regen_job(job) -> None:
    """웹의 'AI 재생성' 요청 — 리뷰 1건의 초안을 새로 만들어 덮어쓴다.

    대상 리뷰 id 는 message 에 담겨 온다(jobs 에 payload 컬럼이 없어서).
    크롤링 없이 DB 의 리뷰로만 생성하므로 빠르다(수 초).
    """
    jid = job["id"]
    try:
        rid = int(job.get("message") or 0)
        row = db.get_review(rid)
        if not row:
            db.finish_job(jid, "error", f"리뷰 {rid} 를 찾을 수 없습니다", 0)
            return
        db.worker_ping("working", "답글 재생성 중")
        review = {
            "platform": row.get("platform"),
            "review_no": row.get("review_no"),
            "author": row.get("author"),
            "rating": row.get("rating"),
            "content": row.get("content"),
            "menus": row.get("menus") or [],
            "raw": row.get("raw"),  # 사진 유무 판별용(classify_review)
            # 주문 횟수는 단골·VIP 판단의 핵심 지표 — raw 에서 뽑아 넘긴다
            # (예전엔 None 으로 고정돼 38번 주문한 단골도 몰랐다).
            "order_count": order_count_of(row),
        }
        draft = generate_review_reply(review)
        # 이미 등록한 답글을 고치려는 재생성이면 상태를 유지한다 — 안 그러면
        # '등록한 답글' 화면에서 그 답글이 사라진다(2026-08-24).
        db.save_ai_draft(rid, draft, kind=classify_review(review),
                         keep_status=(row.get("reply_status") == "posted"))
        db.finish_job(jid, "done", f"리뷰 {rid} 초안 재생성 완료", 1)
        logger.info("재생성 #%s 완료 (리뷰 %s)", jid, rid)
    except Exception as e:  # noqa: BLE001
        logger.error("재생성 #%s 실패: %s", jid, e)
        db.log_error("worker", f"재생성 #{jid} 실패: {e}",
                     kind=type(e).__name__, path="run_regen_job",
                     detail=traceback.format_exc())
        db.finish_job(jid, "error", str(e)[:400], 0)
    finally:
        db.worker_ping("idle", "대기 중")


def run_meeting_organize_job(job) -> None:
    """웹의 '✨ AI로 정리' 요청 — 논의 내용에서 결정사항·업무를 제안해 덧붙인다.

    무료 AI(Gemini)만 쓴다(사장님 지시 2026-08-30, "api key로 유료면 사용 x")
    — assistant.meeting_ai 가 llm.complete(only=("gemini",)) 로 부르므로 이
    잡은 유료 크레딧을 절대 건드리지 않는다. 무료 한도가 찼으면 그대로
    실패로 끝내고, 직원 화면이 그 사유를 보여준다(직접 적으면 된다).

    기존에 적어 둔 결정사항·할 일은 지우지 않는다 — AI 제안은 "(AI 제안 …)"
    표시를 붙여 뒤에 덧붙일 뿐이다(사장님이 확인 후 고치거나 지운다).
    """
    jid = job["id"]
    try:
        mid = int(job.get("message") or 0)
        m = meeting_store.get_meeting(mid)
        if not m:
            db.finish_job(jid, "error", f"회의 {mid} 를 찾을 수 없습니다", 0)
            return
        db.worker_ping("working", "회의 내용 정리 중")
        result = ai_organize(m)

        added_d = added_t = 0
        if result["decisions"]:
            existing = [ln for ln in (m.get("decisions") or "").splitlines()
                       if ln.strip()]
            new_lines = [f"{d} (AI 제안 — 확인 필요)" for d in result["decisions"]]
            meeting_store.update_meeting(
                mid, decisions="\n".join(existing + new_lines))
            added_d = len(new_lines)

        if result["tasks"]:
            current = meeting_store.get_tasks(mid)
            items = [{"id": t["id"], "content": t["content"],
                     "owner": t.get("owner"), "due_date": t.get("due_date"),
                     "memo": t.get("memo"), "done": t.get("done")}
                    for t in current]
            for t in result["tasks"]:
                items.append({"id": None, "content": f"{t['content']} (AI 제안)",
                             "owner": "", "due_date": "",
                             "memo": t.get("memo") or "", "done": False})
            meeting_store.save_tasks(mid, items)
            added_t = len(result["tasks"])

        if not added_d and not added_t:
            db.finish_job(jid, "done",
                          "정리할 내용을 찾지 못했어요 — 내용을 조금 더 "
                          "구체적으로 적어보세요.", 0)
        else:
            db.finish_job(jid, "done",
                          f"결정사항 {added_d}건 · 업무 {added_t}건 제안했어요.",
                          added_d + added_t)
        logger.info("회의 AI정리 #%s 완료 (회의 %s, 결정 %s·업무 %s)",
                   jid, mid, added_d, added_t)
    except MeetingAIUnavailable as e:
        db.finish_job(jid, "error",
                      f"지금은 무료 AI 사용량이 다 찼어요. 잠시 뒤 다시 "
                      f"시도하거나 직접 적어주세요. ({str(e)[:100]})", 0)
        logger.warning("회의 AI정리 #%s — 무료 AI 사용 불가: %s", jid, e)
    except Exception as e:  # noqa: BLE001
        logger.error("회의 AI정리 #%s 실패: %s", jid, e)
        db.log_error("worker", f"회의 AI정리 #{jid} 실패: {e}",
                     kind=type(e).__name__, path="run_meeting_organize_job",
                     detail=traceback.format_exc())
        db.finish_job(jid, "error", str(e)[:400], 0)
    finally:
        db.worker_ping("idle", "대기 중")


# ---------------------------------------------------------------------------
# 자동 수집 — 직원이 버튼을 안 눌러도 몇 시간마다 알아서 수집+초안 준비
# ---------------------------------------------------------------------------

# 몇 시간마다 자동 수집할지. 0 이면 끔(버튼으로만).
AUTO_COLLECT_HOURS = float(os.getenv("WORKER_AUTO_COLLECT_HOURS", "2"))
# 심야(주문·리뷰가 거의 없는 시간)엔 안 돈다. "시작-끝" 시각(끝 미포함).
QUIET_HOURS = os.getenv("WORKER_QUIET_HOURS", "0-7")


def _in_quiet_hours(now) -> bool:
    try:
        start, end = (int(x) for x in QUIET_HOURS.split("-"))
    except ValueError:
        return False
    if start <= end:
        return start <= now.hour < end
    return now.hour >= start or now.hour < end   # 예: "23-7" (자정 걸침)


def auto_collect_due(now, last_requested_at) -> bool:
    """자동 수집을 걸 때가 됐는지 — 순수 판단 로직(테스트 대상).

    마지막 '수집 잡'의 요청 시각 기준이라, 직원이 방금 버튼을 눌렀으면
    그만큼 미뤄지고, 실패한 잡도 간격만큼 기다렸다 재시도한다(스팸 방지).
    """
    if AUTO_COLLECT_HOURS <= 0 or _in_quiet_hours(now):
        return False
    if not last_requested_at:
        return True
    return (now - last_requested_at) >= timedelta(hours=AUTO_COLLECT_HOURS)


# 블로그 반응·순위 자동 수집 시각(HH:MM). 성과 데이터가 매일 쌓여야
# 글감 추천이 "반응 좋은 패턴"을 배울 수 있다 — 버튼만 있으면 아무도 안 누른다.
BLOG_REACT_TIME = os.getenv("BLOG_REACT_TIME", "09:30")
_last_react_day = None


def maybe_blog_react() -> None:
    """하루 한 번 blog_react(반응 수집)와 blog_rank(순위)를 스스로 대기열에 넣는다."""
    global _last_react_day
    try:
        now = datetime.now()
        if now.strftime("%H:%M") < BLOG_REACT_TIME:
            return
        today = now.date().isoformat()
        if _last_react_day == today:
            return
        _last_react_day = today
        from database import blog_store
        blog_store.request_blog_job("blog_react", by="자동")
        blog_store.request_blog_job("blog_rank", by="자동")
        logger.info("블로그 반응·순위 자동 수집 요청 (%s)", BLOG_REACT_TIME)
    except Exception as e:  # noqa: BLE001 — 자동 수집 실패가 루프를 막으면 안 된다
        logger.warning("블로그 자동 수집 판단 실패: %s", e)




def maybe_auto_collect() -> None:
    """때가 됐으면 수집 잡을 스스로 대기열에 넣는다(처리는 기존 잡 흐름 그대로)."""
    try:
        last = None
        job = db.latest_job()
        if job and job.get("requested_at"):
            last = datetime.fromisoformat(
                job["requested_at"].replace("Z", "+00:00")).astimezone()
        if auto_collect_due(datetime.now().astimezone(), last):
            db.request_collect(by="자동")
            logger.info("자동 수집 요청을 넣었습니다 (%.1f시간 간격)", AUTO_COLLECT_HOURS)
    except Exception as e:  # noqa: BLE001 — 자동 수집 실패가 루프를 막으면 안 된다
        logger.warning("자동 수집 판단 실패: %s", e)


# ---------------------------------------------------------------------------
# 자동 답글 등록 — 직원이 '수정 완료'한 답글을 정해진 시간에 일괄 게시
# ---------------------------------------------------------------------------

# (2026-08-29 정리) 옛 '정시 일괄 게시'(AUTO_POST_TIMES·run_auto_post)는
# 지웠다. 2026-08-10 이후 기본값이 꺼짐("")이라 부팅된 적 없는 죽은 경로였고,
# 무엇보다 되켜는 순간 사고가 난다 — approved 전부를 잡 큐의 중복 방지
# (_request_review_job)를 거치지 않고 직접 게시해서, 아침 예약(release_
# scheduled)·버튼 등록(run_post_job)과 **같은 리뷰를 두 번** 게시할 수 있다
# (2026-08-27 실제로 겪은 중복 답글 사고와 같은 유형). 정시 일괄이 다시
# 필요하면 release_scheduled 처럼 '잡을 줄 세우는' 방식으로 만들 것.


def slot_due(times, now, last_slot, window_minutes=10):
    """지금이 정해진 시각(슬롯 시작 후 window_minutes 안)이고 아직 안 돈
    슬롯이면 그 슬롯 키("YYYY-MM-DD HH:MM")를, 아니면 None 을 반환한다.

    window_minutes 를 넓히는 이유(2026-08-28): 이 판정은 일꾼이 **한가할 때만**
    돌아본다. 마침 그 10분에 수집이나 답글 등록이 물려 있으면 슬롯을 통째로
    놓치고, 다음 기회는 **내일**이다. 놓치면 곤란한 일(아침 일괄 등록)은
    창을 넉넉히 준다. 순수 로직.
    """
    for t in (times or "").split(","):
        t = t.strip()
        if not t:
            continue
        try:
            hh, mm = (int(x) for x in t.split(":"))
        except ValueError:
            continue
        slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if slot <= now < slot + timedelta(minutes=window_minutes):
            key = slot.strftime("%Y-%m-%d %H:%M")
            return None if key == last_slot else key
    return None


def _notify_replaced(row, removed=0) -> None:
    """이미 달려 있던 답글을 덮어쓴 사실을 알린다(화면 오류기록 + 로그).

    사장님이 배민·쿠팡 앱에서 직접 단 답글일 수도 있으므로 조용히 넘기지
    않는다. 기록이 실패해도 등록 자체는 이미 끝났으니 예외를 올리지 않는다.
    """
    how = (f"기존 답글 {removed}개를 지우고 새로 등록했습니다"
           if removed else "직원이 등록한 내용으로 수정했습니다")
    notify_owner(
        f"[{row.get('platform')}] {row.get('author')} 님 리뷰에 이미 답글이 "
        f"달려 있어, {how}. 앱에서 직접 답글을 "
        f"다셨다면 내용이 바뀌었을 수 있어요.",
        kind="ReplyReplaced", path="run_post_job")


def run_post_job(job) -> None:
    """웹의 '답글 등록' 버튼 — 리뷰 1건을 지금 바로 배민·쿠팡에 게시한다.

    성공 → posted(수정률 데이터). 실패/리허설 → drafted 로 되돌려 카드가
    다시 나타나게 한다(직원이 재시도 가능).
    """
    jid = job["id"]
    rid = int(job.get("message") or 0)
    row = db.get_review(rid)
    status = (row or {}).get("reply_status")
    if not row or status != "approved":
        # 같은 리뷰에 등록 요청이 여러 번 쌓였을 때(연타·자동복구 겹침) 두 번째
        # 이후 잡이 여기로 온다. 앞선 잡이 이미 처리했으므로 실패가 아니다 —
        # '오류'로 보고하면 화면에 엉뚱한 사유가 뜬다(사장님 제보 2026-08-16:
        # '리뷰 2783 가 등록 대기 상태가 아닙니다'). 조용히 건너뛴다.
        note = {
            "posted": f"리뷰 {rid} 는 이미 등록됨 — 중복 요청 건너뜀",
            "skipped": f"리뷰 {rid} 는 넘어가기 처리됨 — 건너뜀",
            "drafted": f"리뷰 {rid} 는 앞선 요청에서 처리됨 — 중복 요청 건너뜀",
        }.get(status, f"리뷰 {rid} 는 등록 대상이 아님({status or '없음'}) — 건너뜀")
        logger.info("답글 등록 #%s 건너뜀 — %s", jid, note)
        db.finish_job(jid, "done", note, 0)
        return
    db.worker_ping("working", "답글 등록 중")
    try:
        from crawler.review_reply import ReplyToReviewAction
        ensure_chrome()
        review = {
            "platform": row.get("platform"),
            "review_no": row.get("review_no"),
            "author": row.get("author"),
            "rating": row.get("rating"),
            "content": row.get("content"),
            "menus": row.get("menus") or [],
            "raw": row.get("raw"),  # 사진 유무 판별용(classify_review)
        }
        def _post():
            return ReplyToReviewAction(
                review, reply_text=row.get("reply_draft")).run(confirm=True)

        try:
            res = _post()
        except Exception as e:  # noqa: BLE001
            # 브라우저가 도중에 끊긴 경우만 한 번 더 — 다른 오류는 그대로 올린다.
            if not _is_browser_gone(e):
                raise
            logger.warning("등록 중 브라우저가 끊겨 한 번 다시 시도합니다: %s",
                           str(e)[:80])
            time.sleep(3)
            ensure_chrome()
            res = _post()
        if res.get("applied"):
            db.mark_replied(rid)
            # 시간차로 이미 답글이 달려 있어 '수정'으로 맞춘 경우 — 조용히
            # 덮어쓰면 사장님이 앱에서 직접 단 답글이 바뀐 걸 모른다.
            detail = res.get("result") if isinstance(res.get("result"), dict) else {}
            replaced = bool(detail.get("replaced"))
            removed = int(detail.get("removed") or 0)
            note = ""
            if replaced:
                note = (f" (기존 답글 {removed}개를 지우고 새로 등록)" if removed
                        else " (이미 있던 답글을 이 내용으로 수정)")
            db.finish_job(jid, "done", f"리뷰 {rid} 답글 등록 완료{note}", 1)
            logger.info("답글 등록 #%s 완료 (리뷰 %s)%s", jid, rid, note)
            if replaced:
                _notify_replaced(row, removed)
        else:   # 리허설(WRITE_DRY_RUN=true) — 게시 안 됨
            db.mark_drafted(rid)
            db.finish_job(jid, "done",
                          "[리허설] WRITE_DRY_RUN=true — 실제 등록 안 함", 0)
            logger.info("답글 등록 #%s 리허설 — 게시 생략 (리뷰 %s)", jid, rid)
    except Exception as e:  # noqa: BLE001
        # 기한 만료는 재시도해도 영영 실패한다 — 카드를 되돌리면 직원이
        # 계속 누르고 자동복구가 계속 줄 세운다. '넘어가기'로 정리한다.
        # (지연 import 라 클래스 이름으로 판별한다)
        if type(e).__name__ == "ReplyDeadlineError":
            logger.info("답글 등록 #%s — 기한 만료로 정리 (리뷰 %s)", jid, rid)
            try:
                db.mark_skipped(rid)
            except Exception:  # noqa: BLE001
                pass
            db.finish_job(jid, "done",
                          f"리뷰 {rid} 답글 기한 만료 — 목록에서 정리함", 0)
            db.worker_ping("idle", "대기 중")
            return
        logger.error("답글 등록 #%s 실패: %s", jid, e)
        db.log_error("worker", f"답글 등록 실패(리뷰 {rid}): {e}",
                     kind=type(e).__name__, path="run_post_job",
                     detail=traceback.format_exc())
        try:
            db.mark_drafted(rid)    # 카드 복귀 → 직원 재시도 가능
        except Exception:  # noqa: BLE001
            pass
        # ⚠️ 문구는 '리뷰 {id} ' 로 시작해야 한다 — latest_review_job 이 이걸로
        #    잡을 찾고, 자동복구도 '잡이 있는지'를 그걸로 판단한다. 접두가
        #    없으면 실패한 잡이 안 보여 같은 요청이 계속 다시 쌓인다.
        db.finish_job(jid, "error", f"리뷰 {rid} 답글 등록 실패: {str(e)[:360]}", 0)
    finally:
        db.worker_ping("idle", "대기 중")


def _refresh_reply_id(row):
    """쿠팡 수정에 필요한 답글 id 가 raw 에 없으면 즉시 재수집해 채운다.

    방금 등록한 답글은 raw(마지막 수집분)에 아직 replies 가 없어 수정이
    '다음 수집(최대 2시간) 뒤에나' 가능했다 — 직원이 오타를 바로 못 고쳐
    답답하다(사장님 보고 2026-08-13). 수정 직전에 한 번 긁어 해결한다.
    """
    if row.get("platform") != "coupang":
        return row
    try:
        import json as _json
        raw = _json.loads(row["raw"]) if row.get("raw") else {}
        if raw.get("replies"):
            return row                     # 이미 답글 정보 있음
    except Exception:  # noqa: BLE001
        pass
    try:
        from crawler.coupang import CoupangCrawler
        logger.info("수정용 답글 정보가 없어 쿠팡 리뷰를 다시 긁습니다 (리뷰 %s)",
                    row.get("id"))
        db.worker_ping("working", "답글 정보 새로고침 중")
        # 기본 페이지 수(5)로는 2주치를 다 못 훑어 대상 리뷰를 놓쳤다
        # (실측: 5페이지=25건, 12페이지=48건 — 2026-08-13). 넉넉히 훑는다.
        with CoupangCrawler() as c:
            db.save_reviews(c.fetch_reviews(days=COUPANG_DAYS, max_pages=15))
        return db.get_review(row["id"]) or row
    except Exception as e:  # noqa: BLE001 — 실패해도 아래에서 안내 메시지가 뜬다
        logger.warning("답글 정보 새로고침 실패(리뷰 %s): %s", row.get("id"), e)
        return row


def run_post_edit_job(job) -> None:
    """'답글 수정' — 이미 게시된 답글을 새 내용(reply_draft)으로 재게시한다.

    쿠팡=같은 reply API 재호출(덮어쓰기), 배민=답글박스 '수정'→'저장'.
    실패해도 리뷰는 posted 그대로(기존 답글이 살아 있으므로) — 잡 상태로만
    결과를 알린다.
    """
    jid = job["id"]
    rid = int(job.get("message") or 0)
    row = db.get_review(rid)
    if not row or row.get("reply_status") != "posted":
        db.finish_job(jid, "error", f"리뷰 {rid} 는 게시된 답글이 아닙니다", 0)
        return
    db.worker_ping("working", "답글 수정 중")
    try:
        from crawler.review_reply import ReplyToReviewAction
        ensure_chrome()
        row = _refresh_reply_id(row) or row
        review = {
            "platform": row.get("platform"),
            "review_no": row.get("review_no"),
            "author": row.get("author"),
            "rating": row.get("rating"),
            "content": row.get("content"),
            "menus": row.get("menus") or [],
            "raw": row.get("raw"),
        }
        res = ReplyToReviewAction(
            review, reply_text=row.get("reply_draft"),
            allow_edit=True).run(confirm=True)
        if res.get("applied"):
            db.mark_replied(rid)    # 수정 시각으로 갱신 → 새벽 공부 대상에 포함
            db.finish_job(jid, "done", f"리뷰 {rid} 답글 수정 완료", 1)
            logger.info("답글 수정 #%s 완료 (리뷰 %s)", jid, rid)
        else:
            # 연습 모드 = 실제 답글이 그대로다 → 'done'(성공)으로 보고하면
            # 화면이 '✅ 수정 완료!'를 띄워 바뀐 줄 알게 된다. 실패로 알린다.
            # ⚠️ 문구는 '리뷰 {id} ' 로 시작해야 화면 폴링이 이 잡을 찾는다
            #    (latest_review_job 의 like 조건).
            db.finish_job(jid, "error",
                          f"리뷰 {rid} 연습 모드(WRITE_DRY_RUN=true)라 실제 "
                          f"답글은 그대로예요. 집 PC에서 "
                          f"5_자동등록_고치기.bat 을 실행해 주세요.", 0)
    except Exception as e:  # noqa: BLE001
        # 기한 만료는 재시도해도 영영 실패한다 — run_post_job 과 같은 이유로
        # error_log 에 남기지 않는다(매일 새벽 점검이 매번 같은 걸 또 봄).
        # (지연 import 라 클래스 이름으로 판별한다)
        if type(e).__name__ == "ReplyDeadlineError":
            logger.info("답글 수정 #%s — 기한 만료 (리뷰 %s)", jid, rid)
            db.finish_job(jid, "error", f"리뷰 {rid} 답글 수정 실패: {e}", 0)
            db.worker_ping("idle", "대기 중")
            return
        logger.error("답글 수정 #%s 실패: %s", jid, e)
        db.log_error("worker", f"답글 수정 실패(리뷰 {rid}): {e}",
                     kind=type(e).__name__, path="run_post_edit_job",
                     detail=traceback.format_exc())
        # '리뷰 {rid}' 를 남겨야 화면 폴링(latest_review_job)이 찾는다.
        db.finish_job(jid, "error", f"리뷰 {rid} 답글 수정 실패: {str(e)[:350]}", 0)
    finally:
        db.worker_ping("idle", "대기 중")




# ---------------------------------------------------------------------------
# 아침 일괄 등록 — 직원이 '아침에 등록'으로 재워 둔 답글 (2026-08-28)
# ---------------------------------------------------------------------------
# 왜 아침 9시인가: 답글을 달면 손님 폰에 푸시가 간다. 그 푸시는 **주문을
# 정하기 직전**에 닿아야 힘이 있다. 베어글스 주문은 오전 10~12시에 몰린다
# (실측 1,039건: 11시 160 · 10시 117 · 12시 115 · 8시 100 · 9시 93).
# 그래서 9시부터 순차로 올려 9~10시 사이에 푸시가 닿게 한다.
# 새벽에 쓴 답글이 새벽에 나가는 것도 이걸로 막힌다(사장님 요청 2026-08-28).
SCHEDULED_POST_TIMES = os.getenv("WORKER_SCHEDULED_POST_TIMES", "09:00")
# 9시에 수집이나 다른 등록이 물려 있으면 좁은 창은 그냥 지나간다 — 그러면
# 예약분이 **하루를 통째로** 밀린다. 그래서 아침 내내(9~12시) 한 번만 열리는
# 넓은 창으로 본다. 오후에는 열리지 않는다 — 낮에 일꾼이 재시작돼도 '아침
# 예약'이 엉뚱한 시간에 쏟아지지 않게(재시작하면 아래 기억이 비기 때문).
SCHEDULED_POST_WINDOW_MIN = int(
    os.getenv("WORKER_SCHEDULED_POST_WINDOW_MIN", "180"))
_last_scheduled_slot = None


def release_scheduled() -> int:
    """'아침에 등록'으로 재워 둔 답글을 등록 줄에 세운다. 세운 건수 반환.

    직접 게시하지 않고 **등록 잡(post)을 넣는다** — 버튼으로 등록할 때와
    똑같은 길을 타야 실패 처리·기한 만료 정리·중복 방지가 모두 그대로
    적용된다. 잡은 한 건씩 순서대로 처리돼 자연히 간격이 생긴다.
    """
    rows = db.get_scheduled_reviews()
    if not rows:
        return 0
    logger.info("아침 일괄 등록 — 예약된 답글 %d건을 줄 세웁니다", len(rows))
    queued = 0
    for row in rows:
        rid = row.get("id")
        if rid is None:
            continue
        try:
            # ⚠️ 예약은 밤새 묵는다 — 풀기 전에 밤사이 바뀐 것을 재확인한다
            #    (2026-08-30 감사). 등록 경로는 이미 답글이 있으면 지우고
            #    덮는데, 그게 사장님이 앱에서 직접 단 답글일 수 있다.
            if row.get("platform_replied"):
                db.mark_skipped(rid)
                notify_owner(
                    f"[{row.get('platform')}] {row.get('author')} 님 리뷰는 "
                    f"밤사이 이미 답글이 달려 있어(앱에서 직접?) 아침 등록을 "
                    f"건너뛰었어요. 예약해 둔 초안은 등록되지 않았습니다.",
                    kind="Notice", source="worker")
                continue
            # 손님이 밤사이 리뷰를 고쳐 민감(이물질 등) 내용이 됐을 수도
            # 있다 — content 는 재수집 때 갱신되지만 분류는 예약 시점 것이다.
            if classify_review(row) == "escalate":
                db.mark_drafted(rid)
                notify_owner(
                    f"[{row.get('platform')}] {row.get('author')} 님 리뷰가 "
                    f"민감 내용으로 재분류돼 아침 자동 등록에서 뺐어요 — "
                    f"직접 확인해 주세요.",
                    kind="SeriousReview", source="worker")
                continue
            if _too_old_to_reply(row):
                db.mark_skipped(rid)      # 기한 지남 — 등록해도 거절된다
                continue
            db.mark_approved(rid)       # 이제부터는 평소의 '등록 대기'
            db.request_post(rid, by="아침예약")
            queued += 1
        except Exception as e:  # noqa: BLE001 — 한 건 실패가 전체를 막지 않게
            logger.warning("예약 등록 줄세우기 실패(리뷰 %s): %s", rid, e)
    if queued:
        db.log_error("worker",
                     f"아침 일괄 등록 — 예약해 두신 답글 {queued}건을 지금부터 "
                     f"순서대로 등록합니다.",
                     kind="ScheduledPostStarted", path="release_scheduled")
    return queued


def maybe_release_scheduled() -> None:
    global _last_scheduled_slot
    try:
        slot = slot_due(SCHEDULED_POST_TIMES, datetime.now(),
                        _last_scheduled_slot, SCHEDULED_POST_WINDOW_MIN)
        if slot:
            _last_scheduled_slot = slot
            release_scheduled()
    except Exception as e:  # noqa: BLE001
        logger.warning("아침 일괄 등록 판단 실패: %s", e)


# 전파 안 된 고객 요청 알림 — 11시 이후, 하루 한 번만(중복 방지는 kv 대장).
REQUEST_NAG_AFTER_HOUR = int(os.getenv("WORKER_REQUEST_NAG_HOUR", "11"))
REQUEST_NAG_STALE_DAYS = int(os.getenv("WORKER_REQUEST_NAG_DAYS", "3"))


def maybe_request_nag() -> None:
    """전파 안 된 고객 요청이 3일 넘게 묵으면 알림함으로 알린다(하루 1회).

    '놓치지 않게'(CLAUDE.md 목표 3)의 마지막 안전망 — 요청 패널은 /review 를
    열어야만 보이는데, 아무도 안 열면 그걸로 끝이었다(2026-08-30 감사).
    새 요청마다 울리면 알림함이 시끄러워져 진짜 위험 신호(민감 리뷰·세션
    만료)가 묻히므로, **묵은 건이 있을 때 하루 한 번**으로 제한한다.
    """
    now = datetime.now()
    if now.hour < REQUEST_NAG_AFTER_HOUR:
        return
    today = now.strftime("%Y-%m-%d")
    try:
        if db.get_setting("request_nag_day") == today:
            return
        from assistant.customer_requests import find_requests
        rows, _total = db.search_reviews(days=30, limit=300, sort="new")
        shared = {int(x) for x in (db.get_setting("request_shared", []) or [])}
        cut = (now - timedelta(days=REQUEST_NAG_STALE_DAYS)).strftime("%Y-%m-%d")
        stale = [i for i in find_requests(rows, limit=999)
                 if i.get("id") not in shared
                 and max(i.get("date") or "", i.get("collected") or "") <= cut]
        if not stale:
            return
        db.menu_set_setting("request_nag_day", today)
        tops = " · ".join(f"[{i['topic']}] {i['quote'][:24]}…" for i in stale[:3])
        notify_owner(
            f"단톡방에 전파 안 된 고객 요청 {len(stale)}건이 {REQUEST_NAG_STALE_DAYS}일 "
            f"넘게 묵고 있어요 — {tops} (리뷰 현황 화면에서 [복사]→[공유 완료])",
            kind="Notice", source="worker")
    except Exception as e:  # noqa: BLE001 — 알림 실패가 루프를 막으면 안 된다
        logger.warning("고객 요청 알림 판단 실패: %s", str(e)[:120])


# ---------------------------------------------------------------------------
# 문제(심각) 리뷰 정기 보고 — 구 스케줄러(14/22시)에서 이식 (2026-08-16)
# ---------------------------------------------------------------------------
# 스케줄러가 퇴역하면서 이 보고도 함께 죽어, 민감 리뷰가 며칠씩 조용히
# 방치됐다. 크롤은 하지 않는다 — 2시간 자동 수집이 이미 채워 둔 DB 를 읽는다.

COMPLAINT_TIMES = os.getenv("WORKER_COMPLAINT_TIMES", "14:00,22:00")
_last_complaint_slot = None


def run_complaint_report(label="") -> None:
    """최근 리뷰 중 심각(불만·민감·별점≤3) 리뷰를 알림함으로 보고한다.

    같은 리뷰를 두 번 보고하지 않는다(daily_summaries 'complaint_alert_log',
    구 스케줄러와 같은 키라 과거 보고분도 이어진다).
    """
    import json as _json
    from assistant.beargels import format_complaint_report, is_serious_review

    since = (datetime.now().date() - timedelta(days=3)).isoformat()
    rows = (db.get_client().table("reviews").select("*")
            .gte("written_date", since).limit(300).execute().data)
    serious = [r for r in rows if is_serious_review(r)]

    logrow = db.get_summary("complaint_alert_log")
    try:
        alerted = set(_json.loads(logrow["content"])) if logrow else set()
    except Exception:  # noqa: BLE001
        alerted = set()
    new = [r for r in serious
           if r.get("review_no") and str(r["review_no"]) not in
           {str(a) for a in alerted}]
    if not new:
        logger.info("문제 리뷰 점검%s — 신규 없음", f"({label})" if label else "")
        return

    report = format_complaint_report(new, label)
    notify_owner(f"문제 리뷰 {len(new)}건 — 주문 확인이 필요합니다. "
                 f"내용은 아래 상세 참고.\n\n{report[:1200]}",
                 kind="SeriousReview", source="worker",
                 path="run_complaint_report")
    alerted |= {str(r["review_no"]) for r in new}
    try:
        db.save_summary("complaint_alert_log", _json.dumps(sorted(alerted)))
    except Exception:  # noqa: BLE001
        logger.warning("문제 리뷰 보고 로그 저장 실패")
    logger.info("문제 리뷰 보고: 신규 %d건", len(new))


def maybe_complaint_report() -> None:
    global _last_complaint_slot
    try:
        slot = slot_due(COMPLAINT_TIMES, datetime.now(), _last_complaint_slot)
        if slot:
            _last_complaint_slot = slot
            run_complaint_report(slot[-5:])
    except Exception as e:  # noqa: BLE001
        logger.warning("문제 리뷰 보고 실패: %s", e)


# ---------------------------------------------------------------------------
# 포스 장부 자동 반영 — 드라이브 동기화 폴더에서 새 TOS/IMU 엑셀을 찾아
# 마케팅 캘린더용 매출 테이블(sales_daily 등)에 넣는다. (worker/pos_import.py)
# ---------------------------------------------------------------------------

# 장부는 한 달에 한 번 올라온다 — 하루 1회 스캔이면 충분(사장님 2026-08-27).
# 급하면 웹의 [장부 지금 반영] 버튼이 있다.
POS_IMPORT_TIMES = os.getenv("WORKER_POS_IMPORT_TIMES", "10:20")
_last_pos_slot = None


def run_pos_import_job(job) -> None:
    """웹 '매출 지금 반영' 버튼 요청 처리 — 배달 주문 + 포스 장부 둘 다."""
    from worker import pos_import
    jid = job["id"]
    db.worker_ping("working", "매출 반영 중")
    try:
        # ① 배달 주문(진행 중인 달을 채우는 잠정치) — 실패해도 장부는 계속
        order_msg = ""
        try:
            n_ord, ord_warn = collect_orders()
            order_msg = f"배달 주문 {n_ord}건"
            if ord_warn:
                order_msg += " (" + " · ".join(ord_warn)[:120] + ")"
        except Exception as e:  # noqa: BLE001
            order_msg = f"배달 주문 수집 실패: {str(e)[:80]}"
            logger.warning("주문 수집 실패(장부는 계속): %s", e)

        # ② 포스 장부(월 1회 올라오는 확정 매출)
        db.worker_ping("working", "장부 파일 반영 중")
        res = pos_import.scan_ledger()
        if not res.get("ok") and res.get("error"):
            db.finish_job(jid, "error",
                          f"{order_msg} / {res['error']}"[:400], 0)
            return
        n = len(res.get("imported") or [])
        msg = f"{order_msg} / " + (f"새 장부 {n}건 반영" if n else "새 장부 없음")
        if res.get("errors"):
            msg += " / 실패: " + " · ".join(res["errors"])[:150]
        db.finish_job(jid, "error" if res.get("errors") and not n else "done",
                      msg, n)
    except Exception as e:  # noqa: BLE001
        db.log_error("worker", f"장부 반영 실패: {e}", kind=type(e).__name__,
                     path="run_pos_import_job")
        db.finish_job(jid, "error", str(e)[:400], 0)
    finally:
        db.worker_ping("idle", "대기 중")


# ---------------------------------------------------------------------------
# PythonAnywhere 무료 사이트 만료 감시 — 갱신 버튼을 놓치면 웹이 통째로 꺼진다
# ---------------------------------------------------------------------------
#
# 무료 티어는 사장님이 주기적으로 PA 에 로그인해 "Run until 1 month from
# today" 버튼을 눌러야 유지된다(다음 만료 2026-09-27). 이 갱신을 챙겨주는
# 장치가 아무 데도 없었다(2026-08-30 비용 감사) — 놓치면 직원 화면 전체가
# 소리 없이 꺼진다. 하루 한 번 PA API 로 만료일을 읽어 14일 안이면 직원
# 화면 알림함(notify_owner)에 배너를 띄운다. 돈 드는 것 없음(API 무료).
_last_pa_expiry_day = None


def maybe_pa_expiry_check() -> None:
    global _last_pa_expiry_day
    token = os.getenv("PA_API_TOKEN", "").strip()
    if not token:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    if today == _last_pa_expiry_day:
        return
    _last_pa_expiry_day = today
    try:
        import json as _json
        import urllib.request
        req = urllib.request.Request(
            "https://www.pythonanywhere.com/api/v0/user/beargels/webapps/",
            headers={"Authorization": f"Token {token}"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            apps = _json.loads(resp.read().decode())
        for app_info in apps or []:
            expiry = (app_info.get("expiry") or "")[:10]
            if not expiry:
                continue
            days_left = (datetime.fromisoformat(expiry).date()
                         - datetime.now().date()).days
            if days_left <= 14:
                notify_owner(
                    f"⏰ 직원 웹사이트가 {expiry} ({days_left}일 뒤)에 꺼져요 — "
                    "pythonanywhere.com 에 로그인해서 Web 탭의 "
                    "[Run until 1 month from today] 버튼을 눌러주세요. "
                    "(무료 호스팅 유지 조건, 1분이면 됩니다)",
                    kind="Notice", source="worker", path="pa_expiry")
                logger.warning("PA 만료 임박: %s (%d일)", expiry, days_left)
    except Exception as e:  # noqa: BLE001 — 감시 실패가 루프를 막으면 안 된다
        logger.warning("PA 만료 확인 실패: %s", e)


PLACE_AUDIT_TIME = os.getenv("WORKER_PLACE_AUDIT_TIME", "09:40")
_last_place_slot = None


def run_place_audit() -> dict:
    """네이버 플레이스 현황을 진단해 DB 에 저장한다(웹 /place 가 읽는다).

    스마트플레이스 목표 1단계('최적화')를 사람 눈 대신 화면이 하게 만드는 장치.
    읽기 전용 크롤이라 플레이스에는 아무것도 쓰지 않는다.
    """
    from crawler.place_audit import audit

    result = audit()
    db.menu_set_setting("place_audit", result)
    todo = result.get("todo") or []
    logger.info("플레이스 진단: %d/%d 통과%s",
                result["score"]["done"], result["score"]["total"],
                f" — 고칠 것: {', '.join(todo)}" if todo else "")
    return result


def maybe_place_audit() -> None:
    """하루 한 번 플레이스를 진단한다.

    slot_due(10분 창)를 쓰지 않는 이유는 maybe_pos_import 와 같다 — 그 10분에
    일꾼이 바쁘면 그날 진단이 통째로 밀린다.
    """
    global _last_place_slot
    if not os.getenv("NAVER_PLACE_ID", "").strip():
        return
    try:
        now = datetime.now()
        try:
            hh, mm = (int(x) for x in PLACE_AUDIT_TIME.split(":"))
        except ValueError:
            hh, mm = 9, 40
        if now < now.replace(hour=hh, minute=mm, second=0, microsecond=0):
            return
        slot = now.strftime("%Y-%m-%d")          # 하루 1회 키
        if slot == _last_place_slot:
            return
        _last_place_slot = slot
        run_place_audit()
    except Exception as e:  # noqa: BLE001 — 진단 실패가 일꾼을 멈추면 안 된다
        logger.warning("플레이스 진단 실패: %s", e)


PLACE_STATS_WEEKDAY = int(os.getenv("WORKER_PLACE_STATS_WEEKDAY", "0"))  # 0=월
_last_stats_week = None


def run_place_stats() -> dict:
    """스마트플레이스 유입 키워드를 수집해 저장한다(목표 2단계 '노출 상승').

    지난번 스냅샷을 함께 넘겨 **변화량**까지 계산해 둔다 — 지금 순위보다
    "최적화 뒤에 늘었나"가 이 단계의 질문이라서다.
    """
    from crawler.place_stats import collect

    prev = db.get_setting("place_keywords")
    result = collect(previous=prev if isinstance(prev, dict) else None)
    if isinstance(prev, dict):
        db.menu_set_setting("place_keywords_prev", prev)
    db.menu_set_setting("place_keywords", result)
    logger.info("플레이스 유입 키워드: %d개(합 %d)",
                len(result.get("keywords") or []), result.get("total") or 0)
    return result


def maybe_place_stats() -> None:
    """주 1회(기본 월요일) 유입 키워드를 수집한다.

    로그인이 풀린 것과 '키워드가 0개'인 것은 완전히 다른 사건이라, 로그인
    문제는 알림함(SessionExpired)으로 따로 띄운다 — 조용히 0으로 보이면
    노출이 떨어진 걸로 오해한다.
    """
    global _last_stats_week
    try:
        from crawler.place_stats import NaverLoginRequired
        now = datetime.now()
        if now.weekday() != PLACE_STATS_WEEKDAY:
            return
        week = now.strftime("%G-W%V")            # 주 1회 키
        if week == _last_stats_week:
            return
        _last_stats_week = week
        try:
            run_place_stats()
        except NaverLoginRequired as e:
            notify_owner(
                f"스마트플레이스 통계를 못 읽었습니다 — {e}",
                kind="SessionExpired", source="worker", path="maybe_place_stats")
            logger.warning("플레이스 통계: 네이버 로그인 필요")
    except Exception as e:  # noqa: BLE001 — 수집 실패가 일꾼을 멈추면 안 된다
        logger.warning("플레이스 통계 수집 실패: %s", e)


def maybe_pos_import() -> None:
    """하루 한 번(기본 10:20 이후 첫 한가한 때) 매출을 반영한다 —
    배달 주문 + 포스 장부.

    장부는 월 1회라 이것만으로는 진행 중인 달이 백지가 된다. 그래서 같은
    슬롯에서 배달 주문도 긁어 어제까지의 매출이 다음 날 아침에 보이게 한다.

    ⚠️ slot_due(10분 창)를 쓰지 않는다 — 일꾼이 마침 그 10분에 다른 일을
       하고 있으면 그날 반영이 통째로 밀렸다(2026-08-30 감사). '오늘 아직
       안 했고 기준 시각이 지났으면 실행'이라 몇 시간 바빠도 놓치지 않는다.
    """
    global _last_pos_slot
    try:
        now = datetime.now()
        first_time = (POS_IMPORT_TIMES.split(",")[0].strip() or "10:20")
        try:
            hh, mm = (int(x) for x in first_time.split(":"))
        except ValueError:
            hh, mm = 10, 20
        if now < now.replace(hour=hh, minute=mm, second=0, microsecond=0):
            return
        slot = now.strftime("%Y-%m-%d")          # 하루 1회 키
        if slot == _last_pos_slot:
            return
        _last_pos_slot = slot

        # ① 배달 주문 — 실패해도 장부 반영은 계속한다
        try:
            n_ord, ord_warn = collect_orders()
            logger.info("배달 주문 자동 수집: %d건", n_ord)
            if ord_warn:
                db.log_error("worker",
                             "배달 주문 수집 경고: " + " · ".join(ord_warn)[:300],
                             kind="OrderCollectWarning", path="maybe_pos_import")
        except Exception as e:  # noqa: BLE001
            logger.warning("배달 주문 자동 수집 실패(장부는 계속): %s", e)

        # ② 포스 장부
        from worker import pos_import
        res = pos_import.scan_ledger()
        if res.get("imported"):
            logger.info("장부 자동 반영: %s", " · ".join(res["imported"]))
        if res.get("errors"):
            db.log_error("worker",
                         "장부 파일 반영 실패: " + " · ".join(res["errors"])[:300],
                         kind="PosImportError", path="maybe_pos_import")
    except Exception as e:  # noqa: BLE001
        logger.warning("매출 자동 반영 실패: %s", e)


# 방치된 approved 를 다시 줄 세우는 주기(초) — 매 루프(15초)마다 DB 를
# 훑을 필요는 없다.
RESCUE_EVERY_SECONDS = int(os.getenv("WORKER_RESCUE_SECONDS", "300"))
_last_rescue = 0.0


def rescue_stuck_approved() -> int:
    """등록 잡이 사라진 'approved' 리뷰를 다시 줄 세운다. 되살린 건수 반환.

    왜 필요한가: '답글 등록' 버튼은 mark_approved 후 request_post 를 부르는데,
    그 사이에 통신이 끊기면 잡 없이 approved 로 남는다. 옛 '정시 일괄 등록'
    시절에 쌓인 approved 도 마찬가지다(그 경로는 2026-08-29 에 지웠다). 화면엔 '등록 진행 중'으로
    보이지만 실제로는 아무도 처리하지 않는 상태다.

    안전: **잡이 아예 없는 건만** 다시 넣는다. 대기·진행 중인 잡이 있으면
    건드리지 않으므로 같은 답글을 두 번 등록할 위험이 없다.
    """
    revived = 0
    for row in db.get_approved_reviews(limit=100):
        rid = row.get("id")
        if rid is None:
            continue
        try:
            if db.latest_review_job("post", rid):
                continue                      # 이미 줄 서 있음/처리된 이력 있음
            db.request_post(rid, by="자동복구")
            revived += 1
            logger.info("방치된 등록 대기 리뷰 %s 를 다시 줄 세웠습니다", rid)
        except Exception as e:  # noqa: BLE001 — 한 건 실패가 전체를 막지 않게
            logger.warning("등록 대기 복구 실패(리뷰 %s): %s", rid, e)
    if revived:
        db.log_error("worker",
                     f"등록 잡이 없던 답글 {revived}건을 자동으로 다시 "
                     f"등록 요청했습니다(방치 방지).",
                     kind="StuckApprovedRevived", path="rescue_stuck_approved")
    return revived


def maybe_rescue_stuck() -> None:
    global _last_rescue
    now = time.monotonic()
    if now - _last_rescue < RESCUE_EVERY_SECONDS:
        return
    _last_rescue = now
    try:
        rescue_stuck_approved()
    except Exception as e:  # noqa: BLE001
        logger.warning("등록 대기 점검 실패: %s", e)


def run_job(job) -> None:
    """요청 1건 처리. 종류(kind)에 따라 리뷰 수집 / 블로그 / 메뉴 수집으로 나뉜다."""
    if job.get("kind") == "wake":
        # 웹의 '프로그램 깨우기' 요청 — 이 코드가 도는 것 자체가 답이다.
        db.finish_job(job["id"], "done", "일꾼이 켜졌습니다")
        return None
    if job.get("kind") == "regen":
        return run_regen_job(job)
    if job.get("kind") == "post":
        return run_post_job(job)
    if job.get("kind") == "post_edit":
        return run_post_edit_job(job)
    if job.get("kind") == "menu_collect":
        return run_menu_job(job)
    if job.get("kind") == "pos_import":
        return run_pos_import_job(job)
    if job.get("kind") == "meeting_organize":
        return run_meeting_organize_job(job)
    # ⚠️ 블로그 분기는 '알 수 없는 잡' 가드보다 반드시 먼저 —
    # 가드가 앞에 있던 동안 blog_* 잡 전부가 에러로 죽어 블로그 버튼이
    # 통째로 먹통이었다(2026-08-30 감사에서 발견).
    if str(job.get("kind") or "").startswith("blog_"):
        return run_blog_job(job)
    if job.get("kind") not in (None, "", "collect", "collect_all"):
        # 모르는 종류를 수집으로 오처리하지 않는다 — 구버전 일꾼이 새 종류의
        # 잡(post_edit)을 수집으로 돌려버린 사고(2026-08-12).
        db.finish_job(job["id"], "error",
                      f"알 수 없는 잡 종류: {job.get('kind')} — 일꾼 업데이트 필요", 0)
        return None
    jid = job["id"]
    full = job.get("kind") == "collect_all"
    logger.info("%s 요청 #%s 처리 시작 (요청자: %s)",
                "전체 수집" if full else "수집", jid,
                job.get("requested_by") or "?")
    db.worker_ping("working", "전체 리뷰 수집 중" if full else "리뷰 수집 중")
    try:
        saved, warnings = collect_reviews(full=full)
        db.worker_ping("working", "답글 초안 만드는 중")
        made = make_drafts()
        msg = f"리뷰 {saved}건 저장, 답글 초안 {made}건 생성"
        if warnings:
            msg += " / " + " · ".join(warnings)
        status = "error" if (warnings and saved == 0) else "done"
        db.finish_job(jid, status, msg, made)
        logger.info("수집 요청 #%s 완료 — %s", jid, msg)
    except Exception as e:  # noqa: BLE001
        logger.error("수집 요청 #%s 실패: %s", jid, e)
        logger.debug(traceback.format_exc())
        db.log_error("worker", f"수집 요청 #{jid} 실패: {e}",
                     kind=type(e).__name__, path="run_job",
                     detail=traceback.format_exc())
        db.finish_job(jid, "error", str(e)[:400], 0)
    finally:
        db.worker_ping("idle", "대기 중")


# ---------------------------------------------------------------------------
# 메인 루프
# ---------------------------------------------------------------------------

def prune_old_files(days=30) -> int:
    """오래된 진단 파일을 지운다(지운 개수).

    크롤이 빈 화면을 만나면 debug/ 에 그때 화면(html+png)을 통째로 남긴다.
    원인을 볼 땐 요긴하지만 며칠만 지나면 쓸모가 없는데 아무도 안 지운다
    (2026-08-28 정리: 1.6MB, 가장 오래된 게 8월 10일 것). 새벽 점검 로그도
    같이 정리한다.
    """
    cutoff = time.time() - days * 86400
    gone = 0
    for pat in ((ROOT / "debug").glob("*"),
                (ROOT / "logs").glob("nightly-*.log"),
                (ROOT / "logs").glob("menu-diff-*.log")):
        for f in pat:
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
                    gone += 1
            except Exception:  # noqa: BLE001 — 청소가 일을 막으면 안 된다
                continue
    if gone:
        logger.info("오래된 진단 파일 %d개를 정리했습니다(%d일 지난 것)", gone, days)
    return gone


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s",
                        datefmt="%H:%M:%S")
    # ⚠️ Supabase 클라이언트(httpx)가 **모든 요청을 INFO 로** 찍는다. 일꾼은
    #    15초마다 4번씩 물어보므로 하루 2만 줄이 쌓인다 — 실측 2026-08-28:
    #    worker.log 472,958줄 중 465,957줄(98.5%)이 이 한 줄짜리 HTTP 기록,
    #    파일은 76MB. 정작 봐야 할 '수집 완료·등록 실패'가 그 사이에 묻힌다.
    #    문제 생겼을 때 필요한 건 실패(WARNING 이상)뿐이라 그것만 남긴다.
    for noisy in ("httpx", "httpcore", "hpack", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    print("=" * 56)
    print(" 베어글스 집 PC 일꾼 — 대기 중")
    print(f" {POLL_SECONDS}초마다 수집 요청을 확인합니다.")
    print(" 이 창을 열어두세요. (끄려면 Ctrl+C)")
    print("=" * 56)

    try:
        prune_old_files()
    except Exception as e:  # noqa: BLE001 — 청소 실패가 일꾼을 막으면 안 된다
        logger.warning("오래된 파일 정리 실패(무시): %s", str(e)[:100])

    try:
        db.worker_ping("idle", "시작됨")
    except Exception as e:  # noqa: BLE001
        print(f"[!] Supabase 연결 실패: {str(e)[:200]}")
        print("    .env 의 SUPABASE_URL / SUPABASE_SERVICE_KEY 를 확인하세요.")
        print("    그리고 database/schema_v2.sql 을 SQL Editor 에서 실행했는지 확인.")
        return 1

    # ── 두 박자 루프 (2026-08-29) ──────────────────────────────────
    # 예전엔 한 박자(15초)뿐이라, 직원이 [등록]을 누르면 잡이 큐에 들어가고도
    # 일꾼이 낮잠에서 깰 때까지 기다렸다. 그것도 하필 직원의 클릭 리듬과
    # 어긋나서 실측 대기가 평균 15.3초/건 — 등록 실행(11.7초)보다 길었다
    # (2026-08-29 실측: 잡 200건, 화면 일괄 등록 10건에 4.5분의 57%가 이 대기).
    #
    # 그래서 박자를 쪼갠다:
    #   빠른 박자(1.5초) — 직원이 화면 앞에서 기다리는 잡(등록·수정·재생성·
    #       깨우기)만 확인. 인덱스 조회 1회(실측 60ms)라 자주 물어도 싸다.
    #   느린 박자(15초) — 배경 잡(수집·블로그 등) + 정기 점검 묶음 + 상태
    #       보고. 여기엔 무거운 조회(get_approved_reviews 등)가 있어 예전
    #       리듬을 그대로 지킨다 — 빠른 박자에 얹으면 왕복이 몇 배로 튄다.
    last_slow = 0.0
    while True:
        busy = False
        try:
            # 빠른 박자 — 직원이 기다리는 잡부터.
            job = db.claim_next_job(interactive_only=True)
            if job is None and time.monotonic() - last_slow >= POLL_SECONDS:
                # 느린 박자 — 배경 잡과 정기 점검.
                last_slow = time.monotonic()
                job = db.claim_next_job()
                if job is None:
                    maybe_auto_collect()
                    maybe_release_scheduled()
                    maybe_complaint_report()
                    maybe_request_nag()
                    maybe_pos_import()
                    maybe_place_audit()
                    maybe_place_stats()
                    maybe_pa_expiry_check()
                    maybe_blog_react()
                    maybe_rescue_stuck()
                    db.worker_ping("idle", "대기 중")
            if job:
                # 일감이 있으면 쉬지 않고 바로 다음 것을 집는다. 예전엔 한 건
                # 끝낼 때마다 15초를 그냥 쉬어서, 27건 재생성에 생성 시간
                # (건당 20초)만큼의 대기가 더 붙었다 — 전체의 43%가 낭비였다
                # (사장님 "왤케 오래 걸려?" 2026-08-26).
                busy = True
                run_job(job)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001 — 일시적 네트워크 오류로 멈추지 않게
            logger.warning("확인 실패(무시하고 계속): %s", str(e)[:150])
        try:
            if not busy:                 # 할 일이 없을 때만 쉰다
                time.sleep(FAST_POLL_SECONDS)
        except KeyboardInterrupt:
            raise


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n일꾼을 종료합니다.")
        sys.exit(0)
