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
from alerts import notify_owner  # noqa: E402
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

    try:
        from crawler.baemin import BaeminCrawler
        with BaeminCrawler() as c:
            revs = c.fetch_reviews(
                max_scroll=FULL_BAEMIN_SCROLL if full else BAEMIN_SCROLL)
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
            revs = (c.fetch_reviews(days=FULL_COUPANG_DAYS,
                                    max_pages=FULL_COUPANG_PAGES) if full
                    else c.fetch_reviews(days=COUPANG_DAYS))
        saved += db.save_reviews(revs)
        logger.info("쿠팡 리뷰 %d건 수집", len(revs))
    except Exception as e:  # noqa: BLE001
        warnings.append(f"쿠팡 수집 실패: {str(e)[:120]}")
        logger.warning("쿠팡 수집 실패: %s", e)
        db.log_error("worker", f"쿠팡 수집 실패: {e}", kind=type(e).__name__,
                     path="collect/coupang", detail=traceback.format_exc())

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
            "raw": row.get("raw"),  # 사진 유무 판별용(classify_review)
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


# ---------------------------------------------------------------------------
# 자동 답글 등록 — 직원이 '수정 완료'한 답글을 정해진 시간에 일괄 게시
# ---------------------------------------------------------------------------

# 하루 중 게시 시각(HH:MM, 쉼표 구분). 빈 값이면 끔.
# 2026-08-10 사장님 결정: 정시 일괄 대신 '답글 등록' 버튼 즉시 게시(run_post_job)
# 가 기본 — 정시 일괄은 꺼 둔다(.env 로 재활성 가능).
AUTO_POST_TIMES = os.getenv("WORKER_AUTO_POST_TIMES", "")
_last_post_slot = None   # 같은 슬롯을 두 번 돌지 않게(메모리 — 재시작해도
                         # 이미 게시된 건 posted 라 재게시는 없다)


def slot_due(times, now, last_slot):
    """지금이 정해진 시각(슬롯 시작 후 10분 안)이고 아직 안 돈 슬롯이면 그
    슬롯 키("YYYY-MM-DD HH:MM")를, 아니면 None 을 반환한다. 순수 로직."""
    for t in (times or "").split(","):
        t = t.strip()
        if not t:
            continue
        try:
            hh, mm = (int(x) for x in t.split(":"))
        except ValueError:
            continue
        slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if slot <= now < slot + timedelta(minutes=10):
            key = slot.strftime("%Y-%m-%d %H:%M")
            return None if key == last_slot else key
    return None


def post_slot_due(now, last_slot):
    """(호환 유지) 자동 일괄 게시 슬롯 판정 — AUTO_POST_TIMES 기준."""
    return slot_due(AUTO_POST_TIMES, now, last_slot)


def run_auto_post() -> None:
    """'수정 완료(approved)' 답글을 배민·쿠팡에 실제 게시한다.

    안전 계약(crawler/review_reply.ReplyToReviewAction):
      · .env WRITE_DRY_RUN=true 면 게시하지 않고 미리보기 로그만 남긴다.
      · 에스컬레이션 리뷰는 액션이 스스로 거부한다(직원 화면에서도 승인 불가).
    성공한 건만 posted 로 바꾸고, 실패한 건은 approved 로 남아 다음
    슬롯에 재시도된다. 결과는 로그와 화면(오류기록)에 남는다.
    """
    approved = db.get_approved_reviews()
    if not approved:
        return
    from crawler.browser import BrowserSession
    from crawler.review_reply import ReplyToReviewAction

    dry = (os.getenv("WRITE_DRY_RUN", "true").strip().lower()
           not in ("false", "0", "no"))
    logger.info("자동 등록 시작 — 대기 %d건 (dry_run=%s)", len(approved), dry)
    db.worker_ping("working", f"답글 자동 등록 중 ({len(approved)}건)")
    ensure_chrome()
    posted, failed = [], []
    try:
        with BrowserSession() as session:
            for row in approved:
                review = {
                    "platform": row.get("platform"),
                    "review_no": row.get("review_no"),
                    "author": row.get("author"),
                    "rating": row.get("rating"),
                    "content": row.get("content"),
                    "menus": row.get("menus") or [],
                    "raw": row.get("raw"),  # 사진 유무 판별용(classify_review)
                }
                try:
                    res = ReplyToReviewAction(
                        review, reply_text=row.get("reply_draft"),
                        session=session).run(confirm=True)
                    if res.get("applied"):
                        db.mark_replied(row["id"])
                        posted.append(row)
                    else:   # dry-run — 게시 안 됨, approved 유지
                        logger.info("[DRY-RUN] 리뷰 %s 게시 생략", row["id"])
                except Exception as e:  # noqa: BLE001 — 한 건 실패가 배치를 안 막게
                    failed.append((row, str(e)[:150]))
                    db.log_error("worker", f"자동 등록 실패(리뷰 {row['id']}): {e}",
                                 kind=type(e).__name__, path="run_auto_post",
                                 detail=traceback.format_exc())
                time.sleep(2)   # 연속 게시 간 사람같은 간격
    except Exception as e:  # noqa: BLE001 — 브라우저 자체가 안 열리는 경우 등
        db.log_error("worker", f"자동 등록 배치 실패: {e}",
                     kind=type(e).__name__, path="run_auto_post",
                     detail=traceback.format_exc())
    finally:
        db.worker_ping("idle", "대기 중")

    if dry:
        # ⚠️ 조용히 끝내지 않는다: 연습 모드면 대기열이 영영 줄지 않는데
        #    화면에 아무 표시가 없어 '왜 등록이 안 되지'로 이어진다
        #    (사장님 제보 2026-08-11). 상태와 오류로그(화면)에 남긴다.
        msg = (f"자동 등록이 '연습 모드(WRITE_DRY_RUN=true)'라 {len(approved)}건이 "
               f"등록되지 않고 대기로 남았습니다. 집 PC .env 에서 "
               f"WRITE_DRY_RUN=false 로 바꾸고 일꾼을 재시작하세요.")
        logger.warning(msg)
        db.worker_ping("idle", f"⚠️ {msg}")
        db.log_error("worker", msg, kind="DryRunSkipped", path="run_auto_post")
        return
    logger.info("답글 자동 등록 결과 — 성공 %d건, 실패 %d건",
                len(posted), len(failed))
    if failed:
        notify_owner(
            f"답글 자동 등록 {len(failed)}건 실패 — "
            + " · ".join(f"[{r.get('platform')}] {r.get('author')}: {m}"
                         for r, m in failed[:5]),
            kind="AutoPostFailed", path="run_auto_post")


def _notify_replaced(row) -> None:
    """이미 달려 있던 답글을 덮어쓴 사실을 알린다(화면 오류기록 + 로그).

    사장님이 배민·쿠팡 앱에서 직접 단 답글일 수도 있으므로 조용히 넘기지
    않는다. 기록이 실패해도 등록 자체는 이미 끝났으니 예외를 올리지 않는다.
    """
    notify_owner(
        f"[{row.get('platform')}] {row.get('author')} 님 리뷰에 이미 답글이 "
        f"달려 있어, 직원이 등록한 내용으로 수정했습니다. 앱에서 직접 답글을 "
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
    if not row or row.get("reply_status") != "approved":
        db.finish_job(jid, "error", f"리뷰 {rid} 가 등록 대기 상태가 아닙니다", 0)
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
        res = ReplyToReviewAction(
            review, reply_text=row.get("reply_draft")).run(confirm=True)
        if res.get("applied"):
            db.mark_replied(rid)
            # 시간차로 이미 답글이 달려 있어 '수정'으로 맞춘 경우 — 조용히
            # 덮어쓰면 사장님이 앱에서 직접 단 답글이 바뀐 걸 모른다.
            detail = res.get("result")
            replaced = bool(detail.get("replaced")) if isinstance(detail, dict) \
                else False
            note = " (이미 있던 답글을 이 내용으로 수정)" if replaced else ""
            db.finish_job(jid, "done", f"리뷰 {rid} 답글 등록 완료{note}", 1)
            logger.info("답글 등록 #%s 완료 (리뷰 %s)%s", jid, rid, note)
            if replaced:
                _notify_replaced(row)
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
        db.finish_job(jid, "error", str(e)[:400], 0)
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
        logger.error("답글 수정 #%s 실패: %s", jid, e)
        db.log_error("worker", f"답글 수정 실패(리뷰 {rid}): {e}",
                     kind=type(e).__name__, path="run_post_edit_job",
                     detail=traceback.format_exc())
        # '리뷰 {rid}' 를 남겨야 화면 폴링(latest_review_job)이 찾는다.
        db.finish_job(jid, "error", f"리뷰 {rid} 답글 수정 실패: {str(e)[:350]}", 0)
    finally:
        db.worker_ping("idle", "대기 중")


def maybe_auto_post() -> None:
    global _last_post_slot
    try:
        slot = post_slot_due(datetime.now(), _last_post_slot)
        if slot:
            _last_post_slot = slot
            run_auto_post()
    except Exception as e:  # noqa: BLE001
        logger.warning("자동 등록 판단 실패: %s", e)


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


# 방치된 approved 를 다시 줄 세우는 주기(초) — 매 루프(15초)마다 DB 를
# 훑을 필요는 없다.
RESCUE_EVERY_SECONDS = int(os.getenv("WORKER_RESCUE_SECONDS", "300"))
_last_rescue = 0.0


def rescue_stuck_approved() -> int:
    """등록 잡이 사라진 'approved' 리뷰를 다시 줄 세운다. 되살린 건수 반환.

    왜 필요한가: '답글 등록' 버튼은 mark_approved 후 request_post 를 부르는데,
    그 사이에 통신이 끊기면 잡 없이 approved 로 남는다. 옛 '정시 일괄 등록'
    시절에 쌓인 approved 도 마찬가지다(지금은 AUTO_POST_TIMES 가 비어 있어
    일괄 등록이 돌지 않으므로 영영 방치된다). 화면엔 '등록 진행 중'으로
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
    if job.get("kind") not in (None, "", "collect", "collect_all"):
        # 모르는 종류를 수집으로 오처리하지 않는다 — 구버전 일꾼이 새 종류의
        # 잡(post_edit)을 수집으로 돌려버린 사고(2026-08-12).
        db.finish_job(job["id"], "error",
                      f"알 수 없는 잡 종류: {job.get('kind')} — 일꾼 업데이트 필요", 0)
        return None
    jid = job["id"]
    if str(job.get("kind") or "").startswith("blog_"):
        return run_blog_job(job)
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
                maybe_auto_post()
                maybe_complaint_report()
                maybe_rescue_stuck()
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
