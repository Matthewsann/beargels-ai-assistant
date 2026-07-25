"""베어글스 운영 대시보드 (웹) — 로컬 실행용 Flask 앱.

집 PC(봇 서버)에서 띄우고 브라우저로 localhost 접속해 매출·리뷰·답글을 본다.
데이터는 Supabase(스케줄러가 수집·저장)에서 읽고, '리뷰 수집' 버튼으로 즉시
크롤링을 돌릴 수 있다. 답글 게시는 WRITE_DRY_RUN 을 따른다(기본 dry-run).

실행:
    python -m web.app          # http://127.0.0.1:5000
전제: attach 모드 Chrome(launch_chrome.bat) — 수집/게시 시 필요.
"""

import logging
import threading
from collections import OrderedDict
from datetime import date, datetime, timedelta

from flask import Flask, jsonify, render_template, request

from assistant.beargels import (
    classify_review, complaint_reason, compute_order_stats,
    compute_review_stats, generate_review_reply, is_serious_review,
)
from crawler.review_reply import ReplyToReviewAction
from crawler.write_guard import _default_dry_run
from database import supabase_client as db

logger = logging.getLogger(__name__)
app = Flask(__name__)

# 백그라운드 수집 상태(간단 인메모리).
_collect = {"running": False, "msg": "", "at": None}
# 실제 답글 가능한(버튼 살아있는) 미답변 리뷰 캐시 — 수집 시 갱신.
_repliable = {"reviews": [], "at": None}

_PLAT_KO = {"baemin": "배민", "coupang": "쿠팡"}

# 답글 등록 기한(일). 배민=30일(무응답 30일 게시중단 기준). 쿠팡은 공식 수치 불명확
# → 보수적으로 30일. 실제 게시 가능 여부는 플랫폼 답글 버튼이 최종 기준.
REPLY_DEADLINE_DAYS = {"baemin": 30, "coupang": 30}


def _days_since(written_date):
    try:
        d = datetime.strptime(str(written_date), "%Y-%m-%d").date()
        return (date.today() - d).days
    except Exception:  # noqa: BLE001
        return None


def _safe(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        logger.exception("대시보드 데이터 조회 실패")
        return default


@app.route("/")
def dashboard():
    today = date.today()
    yday = today - timedelta(days=1)
    o_today = _safe(lambda: db.get_orders_by_date(today), [])
    o_yday = _safe(lambda: db.get_orders_by_date(yday), [])
    reviews = _safe(lambda: db.get_reviews(limit=100), [])

    ostat_today = compute_order_stats(o_today)
    ostat_yday = compute_order_stats(o_yday)
    rstat = compute_review_stats(reviews)

    # 문제 리뷰(보고): DB 기준으로 사유 라벨 부여.
    for r in reviews:
        r["_reason"] = complaint_reason(r) if is_serious_review(r) else None
        r["_escalate"] = classify_review(r) == "escalate"
    serious = [r for r in reviews if is_serious_review(r)]

    # 답글 대상: '실제 답글 가능한(버튼 살아있는)' 미답변 리뷰 캐시 사용.
    # → 기한 만료·이미 답변한 리뷰는 자동으로 빠진다(수집 시 라이브 판별).
    repliable = list(_repliable["reviews"])
    for r in repliable:
        r["_reason"] = complaint_reason(r) if is_serious_review(r) else None
        r["_escalate"] = classify_review(r) == "escalate"

    by_date = OrderedDict()
    for r in sorted(repliable, key=lambda x: x.get("written_date") or "",
                    reverse=True):
        by_date.setdefault(r.get("written_date") or "날짜미상", []).append(r)

    return render_template(
        "dashboard.html",
        today=today.isoformat(),
        dry_run=_default_dry_run(),
        ostat_today=ostat_today, ostat_yday=ostat_yday,
        rstat=rstat, plat_ko=_PLAT_KO,
        serious=serious, by_date=by_date,
        repliable_cnt=len(repliable),
        repliable_at=_repliable["at"],
        collect=_collect, review_count=len(reviews),
    )


def _run_collect():
    from datetime import datetime as _dt

    from crawler.review_reply import fetch_repliable
    from scheduler.jobs import crawl_job
    try:
        orders, reviews = crawl_job()          # 매출·평점용 DB 저장
        rep = fetch_repliable()                # 실제 답글 가능한 미답변(라이브)
        _repliable["reviews"] = rep
        _repliable["at"] = _dt.now().strftime("%m/%d %H:%M")
        _collect["msg"] = (f"수집 완료: 주문 {len(orders)}건, 리뷰 {len(reviews)}건, "
                           f"답글 가능 {len(rep)}건")
    except Exception as e:  # noqa: BLE001
        logger.exception("수집 실패")
        _collect["msg"] = f"수집 실패: {str(e)[:100]}"
    finally:
        _collect["running"] = False


@app.route("/collect", methods=["POST"])
def collect():
    if _collect["running"]:
        return jsonify({"ok": False, "msg": "이미 수집 중입니다."})
    _collect.update(running=True, msg="수집 중… (배민·쿠팡 크롤링, 1~2분)")
    threading.Thread(target=_run_collect, daemon=True).start()
    return jsonify({"ok": True, "msg": _collect["msg"]})


@app.route("/collect/status")
def collect_status():
    return jsonify(running=_collect["running"], msg=_collect["msg"])


def _find_review(platform, review_no):
    """답글 대상 캐시 우선, 없으면 DB 에서 리뷰를 찾는다."""
    for r in _repliable["reviews"]:
        if str(r.get("review_no")) == str(review_no) and r.get("platform") == platform:
            return r
    return _safe(
        lambda: next(r for r in db.get_reviews(limit=100)
                     if str(r.get("review_no")) == str(review_no)
                     and r.get("platform") == platform),
        None)


@app.route("/draft", methods=["POST"])
def draft():
    """리뷰 하나의 답글 초안을 생성해 반환(브라우저가 지연 요청)."""
    data = request.get_json(force=True)
    review = _find_review(data.get("platform"), data.get("review_no"))
    if not review:
        return jsonify({"ok": False, "text": ""})
    return jsonify({"ok": True, "text": generate_review_reply(review)})


@app.route("/reply", methods=["POST"])
def reply():
    """답글 게시(WRITE_DRY_RUN 준수). body: platform, review_no, text."""
    data = request.get_json(force=True)
    review = _find_review(data.get("platform"), data.get("review_no"))
    if not review:
        return jsonify({"ok": False, "msg": "리뷰를 찾지 못했습니다(수집 후 재시도)."})
    text = (data.get("text") or "").strip()
    action = ReplyToReviewAction(review, reply_text=text or None)
    try:
        res = action.run(confirm=True)  # dry_run 은 .env 따름
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "msg": f"게시 실패: {str(e)[:150]}"})
    if res.get("applied"):
        return jsonify({"ok": True, "msg": "게시 완료 ✅"})
    return jsonify({"ok": True, "dry_run": True,
                    "msg": "🧪 dry-run 모드 — 실제 게시 안 됨 "
                           "(.env WRITE_DRY_RUN=false 필요)"})


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    app.run(host="127.0.0.1", port=5000, debug=False)


if __name__ == "__main__":
    main()
