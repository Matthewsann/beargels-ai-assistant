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

from assistant.beargels import classify_review, generate_review_reply  # noqa: E402
from database import supabase_client as db  # noqa: E402

logger = logging.getLogger("worker")

POLL_SECONDS = int(os.getenv("WORKER_POLL_SECONDS", "15"))
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
        subprocess.Popen(
            [str(CHROME_BAT)], cwd=str(CHROME_BAT.parent),
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
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


def collect_reviews() -> tuple[int, list[str]]:
    """배민·쿠팡 리뷰를 긁어 DB 에 저장한다. (저장 건수, 경고 메시지들)

    한쪽 플랫폼이 실패해도 다른 쪽은 계속한다(로그인 만료 등).
    """
    saved, warnings = 0, []

    # 크롬이 꺼져 있으면 먼저 켠다(사장님이 손으로 켜지 않아도 되게).
    if not ensure_chrome():
        warnings.append("크롤링용 Chrome 을 켜지 못했습니다 — 집 PC 확인 필요")
        return 0, warnings

    try:
        from crawler.baemin import BaeminCrawler
        with BaeminCrawler() as c:
            revs = c.fetch_reviews(max_scroll=BAEMIN_SCROLL)
        saved += db.save_reviews(revs)
        logger.info("배민 리뷰 %d건 수집", len(revs))
    except Exception as e:  # noqa: BLE001 — 한쪽 실패가 전체를 막지 않게
        warnings.append(f"배민 수집 실패: {str(e)[:120]}")
        logger.warning("배민 수집 실패: %s", e)
        db.log_error("worker", f"배민 수집 실패: {e}", kind=type(e).__name__,
                     path="collect/baemin", detail=traceback.format_exc())

    try:
        from crawler.coupang import CoupangCrawler
        with CoupangCrawler() as c:
            revs = c.fetch_reviews(days=COUPANG_DAYS)
        saved += db.save_reviews(revs)
        logger.info("쿠팡 리뷰 %d건 수집", len(revs))
    except Exception as e:  # noqa: BLE001
        warnings.append(f"쿠팡 수집 실패: {str(e)[:120]}")
        logger.warning("쿠팡 수집 실패: %s", e)
        db.log_error("worker", f"쿠팡 수집 실패: {e}", kind=type(e).__name__,
                     path="collect/coupang", detail=traceback.format_exc())

    return saved, warnings


def make_drafts() -> int:
    """초안이 아직 없는 미답변 리뷰에 답글 초안을 만들어 저장한다. 만든 수 반환."""
    made = 0
    for row in db.get_pending_reviews(limit=100):
        if row.get("reply_draft"):
            continue                      # 이미 초안 있음(직원이 고친 것 포함)
        if row.get("platform_replied"):
            continue                      # 플랫폼에 이미 답글이 달림
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
            "order_count": None,
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
    for channel, fetch in (("baemin", menu_scrape.fetch_baemin_menus),
                           ("coupang", menu_scrape.fetch_coupang_menus),
                           ("naver", menu_scrape.fetch_naver_menus)):
        try:
            rows = fetch()
            if rows:
                total += db.save_menu_snapshots(channel, rows)
            else:
                warnings.append(f"{channel} 0건(덤프 확인)")
        except Exception as e:  # noqa: BLE001 — 채널 하나 실패해도 나머지는 진행
            warnings.append(f"{channel} 실패: {str(e)[:80]}")
            db.log_error("worker", f"채널 메뉴 수집 실패({channel}): {e}",
                         kind=type(e).__name__, path=f"menu_collect/{channel}",
                         detail=traceback.format_exc())
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
            "order_count": None,
        }
        draft = generate_review_reply(review)
        db.save_ai_draft(rid, draft, kind=classify_review(review))
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


def run_job(job) -> None:
    """요청 1건 처리. 종류(kind)에 따라 리뷰 수집 / 블로그 / 메뉴 수집으로 나뉜다."""
    if job.get("kind") == "wake":
        # 웹의 '프로그램 깨우기' 요청 — 이 코드가 도는 것 자체가 답이다.
        db.finish_job(job["id"], "done", "일꾼이 켜졌습니다")
        return None
    if job.get("kind") == "regen":
        return run_regen_job(job)
    if job.get("kind") == "menu_collect":
        return run_menu_job(job)
    jid = job["id"]
    if str(job.get("kind") or "").startswith("blog_"):
        return run_blog_job(job)
    logger.info("수집 요청 #%s 처리 시작 (요청자: %s)", jid, job.get("requested_by") or "?")
    db.worker_ping("working", "리뷰 수집 중")
    try:
        saved, warnings = collect_reviews()
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

def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s",
                        datefmt="%H:%M:%S")
    print("=" * 56)
    print(" 베어글스 집 PC 일꾼 — 대기 중")
    print(f" {POLL_SECONDS}초마다 수집 요청을 확인합니다.")
    print(" 이 창을 열어두세요. (끄려면 Ctrl+C)")
    print("=" * 56)

    try:
        db.worker_ping("idle", "시작됨")
    except Exception as e:  # noqa: BLE001
        print(f"[!] Supabase 연결 실패: {str(e)[:200]}")
        print("    .env 의 SUPABASE_URL / SUPABASE_SERVICE_KEY 를 확인하세요.")
        print("    그리고 database/schema_v2.sql 을 SQL Editor 에서 실행했는지 확인.")
        return 1

    while True:
        try:
            job = db.claim_next_job()
            if job:
                run_job(job)
            else:
                maybe_auto_collect()
                db.worker_ping("idle", "대기 중")
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001 — 일시적 네트워크 오류로 멈추지 않게
            logger.warning("확인 실패(무시하고 계속): %s", str(e)[:150])
        try:
            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            raise


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n일꾼을 종료합니다.")
        sys.exit(0)
