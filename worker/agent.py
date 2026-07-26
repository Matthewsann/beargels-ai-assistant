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
import sys
import time
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assistant.beargels import generate_review_reply  # noqa: E402
from database import supabase_client as db  # noqa: E402

logger = logging.getLogger("worker")

POLL_SECONDS = int(os.getenv("WORKER_POLL_SECONDS", "15"))
COUPANG_DAYS = int(os.getenv("WORKER_COUPANG_DAYS", "14"))
BAEMIN_SCROLL = int(os.getenv("WORKER_BAEMIN_SCROLL", "3"))
MAX_DRAFTS_PER_RUN = int(os.getenv("WORKER_MAX_DRAFTS", "20"))


# ---------------------------------------------------------------------------
# 수집 + 초안 생성
# ---------------------------------------------------------------------------

def collect_reviews() -> tuple[int, list[str]]:
    """배민·쿠팡 리뷰를 긁어 DB 에 저장한다. (저장 건수, 경고 메시지들)

    한쪽 플랫폼이 실패해도 다른 쪽은 계속한다(로그인 만료 등).
    """
    saved, warnings = 0, []

    try:
        from crawler.baemin import BaeminCrawler
        with BaeminCrawler() as c:
            revs = c.fetch_reviews(max_scroll=BAEMIN_SCROLL)
        saved += db.save_reviews(revs)
        logger.info("배민 리뷰 %d건 수집", len(revs))
    except Exception as e:  # noqa: BLE001 — 한쪽 실패가 전체를 막지 않게
        warnings.append(f"배민 수집 실패: {str(e)[:120]}")
        logger.warning("배민 수집 실패: %s", e)

    try:
        from crawler.coupang import CoupangCrawler
        with CoupangCrawler() as c:
            revs = c.fetch_reviews(days=COUPANG_DAYS)
        saved += db.save_reviews(revs)
        logger.info("쿠팡 리뷰 %d건 수집", len(revs))
    except Exception as e:  # noqa: BLE001
        warnings.append(f"쿠팡 수집 실패: {str(e)[:120]}")
        logger.warning("쿠팡 수집 실패: %s", e)

    return saved, warnings


def make_drafts() -> int:
    """초안이 아직 없는 미답변 리뷰에 답글 초안을 만들어 저장한다. 만든 수 반환."""
    made = 0
    for row in db.get_pending_reviews(limit=100):
        if row.get("reply_draft"):
            continue                      # 이미 초안 있음(직원이 고친 것 포함)
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
            continue
        db.save_reply_draft(row["id"], draft)
        made += 1
    return made


def run_job(job) -> None:
    """수집 요청 1건 처리."""
    jid = job["id"]
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
