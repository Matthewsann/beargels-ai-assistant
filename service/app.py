"""베어글스 직원용 리뷰 답글 웹서비스 (클라우드 배포용).

직원·점장·매니저가 휴대폰으로 열어 쓰는 화면. 하는 일은 딱 세 가지:
    1. [리뷰수집] 버튼 → 집 PC 일꾼에게 수집 요청(Supabase jobs)
    2. 수집된 미답변 리뷰 + 답글 초안 목록 보기 / 고치기
    3. 📋 복사 → 배민·쿠팡에 직접 붙여넣기 → [답글 등록함] 체크

⚠️ 자동 게시하지 않는다. 복사만 도와준다(실고객 노출 사고 방지).

접속: 로그인 화면 없음. 대신 **주소 자체가 비밀번호** 역할을 한다.
      https://.../<SERVICE_PATH>/  로만 열리고, 나머지 주소는 전부 404.
      SERVICE_PATH 를 모르면 못 들어온다. 검색엔진에도 안 잡히게 막아둔다.
      (AI 답글은 사장님 API 크레딧을 쓰므로 최소한의 잠금은 필요하다.)

필요한 환경변수:
    SERVICE_PATH          비밀 주소 조각 (예: k7m2x9qp)
    SUPABASE_URL          Supabase 프로젝트 URL
    SUPABASE_SERVICE_KEY  service_role 키 (RLS 우회 · 서버에만 둔다)

답글 '생성'은 이 앱이 하지 않는다 — 집 PC 일꾼(worker/agent.py)이 만들어 DB 에
넣어둔 걸 보여주기만 한다. 그래서 이 앱에는 AI 키가 필요 없다.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import traceback
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
from flask import (  # noqa: E402
    Flask, abort, jsonify, redirect, render_template, request, url_for,
)

# 설정은 service/.env 를 먼저 본다(클라우드 서버에는 이 파일만 올린다 —
# 집 PC 의 .env 에는 배민·쿠팡 비밀번호까지 들어 있어 올리면 안 된다).
# 집 PC 에서 테스트할 때는 service/.env 가 없으므로 루트 .env 를 쓴다.
load_dotenv(pathlib.Path(__file__).resolve().parent / ".env")
load_dotenv(ROOT / ".env")

from database import supabase_client as db  # noqa: E402

app = Flask(__name__)

# 비밀 주소 조각 — 없으면 앱이 뜨지 않는다(실수로 전체 공개되는 걸 막는다).
SERVICE_PATH = (os.getenv("SERVICE_PATH") or "").strip().strip("/")

# 집 PC 일꾼이 이 시간 안에 신호를 보냈으면 '켜져 있음'으로 본다.
WORKER_ALIVE_SECONDS = 90

PLAT = {"baemin": "배민", "coupang": "쿠팡이츠"}


# ---------------------------------------------------------------------------
# 공통
# ---------------------------------------------------------------------------

@app.after_request
def no_index(resp):
    """검색엔진 수집 금지 — 비밀 주소가 검색에 노출되면 의미가 없다."""
    resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return resp


@app.errorhandler(Exception)
def record_error(e):
    """화면에서 난 모든 오류를 DB(error_log)에 남긴다.

    집 PC 의 새벽 자동 점검이 이 기록을 읽어 원인을 고친다. 404 는 비밀 주소를
    모르는 접근이라 정상 동작이므로 기록하지 않는다(로그가 쓰레기로 찬다).
    """
    code = getattr(e, "code", 500)
    if code == 404:
        return e
    db.log_error(
        "service", str(e), kind=type(e).__name__,
        path=request.path, detail=traceback.format_exc(),
    )
    if code != 500:
        return e
    return ("문제가 생겼어요. 잠시 뒤 다시 시도해주세요. "
            "계속 그러면 사장님께 알려주세요."), 500


@app.route("/robots.txt")
def robots():
    return "User-agent: *\nDisallow: /\n", 200, {"Content-Type": "text/plain"}


@app.route("/")
def root():
    """비밀 주소를 모르는 사람에겐 아무것도 알려주지 않는다."""
    abort(404)


def check(path_key: str) -> None:
    if not SERVICE_PATH or path_key != SERVICE_PATH:
        abort(404)


def _worker_view() -> dict:
    """집 PC 일꾼 상태를 화면용으로 정리."""
    try:
        st = db.worker_status()
    except Exception:  # noqa: BLE001
        return {"alive": False, "text": "상태 확인 실패", "state": "error"}
    if not st:
        return {"alive": False, "text": "집 PC 일꾼이 아직 한 번도 안 켜졌어요",
                "state": "off"}
    try:
        seen = datetime.fromisoformat(st["last_seen"].replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return {"alive": False, "text": "상태 확인 실패", "state": "error"}
    gap = datetime.now(timezone.utc) - seen.astimezone(timezone.utc)
    alive = gap < timedelta(seconds=WORKER_ALIVE_SECONDS)
    if alive:
        working = st.get("state") == "working"
        return {"alive": True, "state": st.get("state"),
                "text": st.get("message") or ("작업 중" if working else "대기 중")}
    mins = int(gap.total_seconds() // 60)
    return {"alive": False, "state": "off",
            "text": f"집 PC가 꺼져 있어요 (마지막 신호 {mins}분 전)"}


def _friendly_fail(msg: str) -> str:
    """실패 사유를 직원이 이해할 수 있는 말로 바꾼다.

    'CDP attach 실패(127.0.0.1:9222)' 같은 원문을 그대로 보여주면 직원은
    무엇을 해야 할지 알 수 없다. 원인별로 '누가 무엇을 하면 되는지'만 남긴다.
    """
    m = msg or ""
    if "CDP" in m or "attach" in m or "Chrome" in m or "크롬" in m:
        return "집 PC 크롬을 켜는 중 문제가 있었어요. 잠시 뒤 다시 눌러주세요."
    if "세션" in m or "로그인" in m or "SessionExpired" in m:
        return "배민·쿠팡 로그인이 풀렸어요. 사장님께 알려주세요."
    if "credit" in m.lower() or "크레딧" in m:
        return "AI 답글 생성이 잠시 멈췄어요. 사장님께 알려주세요."
    return "수집에 실패했어요. 잠시 뒤 다시 눌러주세요."


def _job_view(job) -> dict | None:
    if not job:
        return None
    if job.get("kind") == "wake":
        label = {
            "pending": "프로그램을 깨우는 중… (최대 5분, 화면이 자동으로 새로고침돼요)",
            "done": "프로그램이 깨어났어요! 이제 리뷰수집을 누를 수 있습니다.",
        }.get(job.get("status"), job.get("status"))
        return {"status": job.get("status"), "text": label,
                "busy": job.get("status") in ("pending", "running")}
    label = {
        "pending": "수집 요청 접수됨 — 집 PC가 곧 시작해요",
        "running": "수집 중이에요… (30초쯤 걸려요)",
        "done": job.get("message") or "수집 완료",
        "error": _friendly_fail(job.get("message")),
    }.get(job.get("status"), job.get("status"))
    return {"status": job.get("status"), "text": label,
            "busy": job.get("status") in ("pending", "running")}


# '그대로 등록해도 좋아요' 배지 기준 — 그 유형을 직원이 거의 안 고치게 됐을 때.
# 표본이 적을 때 우연으로 켜지지 않게 최소 10건을 요구한다.
TRUST_MIN_SAMPLES = 10
TRUST_MAX_EDIT_RATE = 0.05


def _trusted_kinds() -> set:
    """수정률이 충분히 낮은 리뷰 유형 집합. 민감/불만은 무조건 제외."""
    try:
        stats = db.edit_rate_by_kind()
    except Exception:  # noqa: BLE001 — 통계 실패가 화면을 막으면 안 된다
        return set()
    return {k for k, s in stats.items()
            if k not in ("escalate", "complaint")
            and s["n"] >= TRUST_MIN_SAMPLES
            and s["rate"] <= TRUST_MAX_EDIT_RATE}


# 플랫폼 리뷰 관리 페이지 — '실제 답글 보러가기' 바로가기용(리뷰별 딥링크는
# 두 플랫폼 다 제공하지 않아 리뷰 목록 페이지로 보낸다).
PLATFORM_REVIEW_URL = {
    "baemin": "https://self.baemin.com/shops/reviews",
    "coupang": "https://store.coupangeats.com/merchant/management/reviews",
}


def _review_view(r: dict) -> dict:
    draft = r.get("reply_draft") or ""
    return {
        "kind": r.get("kind"),
        "id": r.get("id"),
        "platform": PLAT.get(r.get("platform"), r.get("platform") or ""),
        "rating": r.get("rating"),
        "author": r.get("author") or "고객",
        "content": (r.get("content") or "").strip(),
        "menus": ", ".join(r.get("menus") or []) if isinstance(r.get("menus"), list) else "",
        "date": r.get("written_date") or "",
        "draft": draft,
        "escalate": draft.strip().startswith("⚠️"),
        "has_draft": bool(draft),
        "platform_url": PLATFORM_REVIEW_URL.get(r.get("platform"), ""),
        # 주문 정보 — 배민·쿠팡 리뷰 관리 화면과 같은 항목을 보여준다.
        **_order_info(r),
    }


def _order_info(r: dict) -> dict:
    """리뷰 카드에 띄울 주문 정보(주문번호·주문일·수령방식·주문횟수).

    저장 컬럼에 없으면 raw(플랫폼 원본 JSON)에서 보완한다.
    """
    raw = {}
    try:
        if r.get("raw"):
            raw = json.loads(r["raw"]) if isinstance(r["raw"], str) else r["raw"]
    except Exception:  # noqa: BLE001
        raw = {}
    order_no = r.get("order_no") or raw.get("abbrOrderId") or ""
    ordered = (r.get("ordered_at") or raw.get("orderedAt") or "")
    count = r.get("order_count") or raw.get("orderCount")
    delivery = r.get("delivery_type") or raw.get("orderType") or ""
    return {
        "order_no": order_no,
        "ordered_at": str(ordered).replace("T", " ")[:16],
        "order_count": count if isinstance(count, int) and count > 0 else None,
        "delivery": {"REGULAR": "배달", "TAKE_OUT": "포장"}.get(delivery, delivery),
    }


# ---------------------------------------------------------------------------
# 화면
# ---------------------------------------------------------------------------

@app.route("/<path_key>/")
def home(path_key):
    check(path_key)
    error = None
    reviews = []
    waiting = 0
    plat = (request.args.get("plat") or "").strip()   # baemin|coupang|빈값(전체)
    try:
        raw_rows = db.get_pending_reviews(limit=100)
        if plat:                       # 쿠팡만/배민만 보기
            raw_rows = [r for r in raw_rows if r.get("platform") == plat]
        rows = [_review_view(r) for r in raw_rows]
        # 초안이 있는 것만 보여준다 — 초안 없는 카드가 화면을 덮으면
        # 직원이 무엇을 해야 하는지 알 수 없다. 나머지는 건수로만 알린다.
        reviews = [r for r in rows if r["has_draft"]]
        trusted = _trusted_kinds()
        for r in reviews:
            r["trusted"] = (not r["escalate"]) and r.get("kind") in trusted
        waiting = len(rows) - len(reviews)
        job = _job_view(db.latest_job())
        approved_count = len(db.get_approved_reviews(limit=100))
    except Exception as e:  # noqa: BLE001
        error = f"데이터를 불러오지 못했어요: {str(e)[:150]}"
        job = None
        approved_count = 0
    return render_template(
        "staff.html", key=path_key, reviews=reviews, worker=_worker_view(),
        job=job, error=error, waiting=waiting, approved_count=approved_count,
        plat=plat,
    )


@app.route("/<path_key>/guide")
def guide(path_key):
    """직원용 사용법 안내 — 정적 페이지(DB 안 씀)."""
    check(path_key)
    return render_template("guide.html", key=path_key)


@app.route("/<path_key>/collect", methods=["POST"])
def collect(path_key):
    check(path_key)
    try:
        db.request_collect()
    except Exception as e:  # noqa: BLE001 — 화면은 상태로 안내, 원인은 기록
        db.log_error("service", str(e), kind=type(e).__name__,
                     path=request.path, detail=traceback.format_exc())
    return redirect(url_for("home", path_key=path_key))


@app.route("/<path_key>/wake", methods=["POST"])
def wake(path_key):
    """'프로그램 깨우기' — 집 PC 감시견이 5분 안에 일꾼을 되살린다.

    PC 전원이 켜져 있고 일꾼 프로그램만 꺼진 경우용. PC 전원 자체가 꺼져
    있으면 이 버튼으로는 켤 수 없다(화면에 그렇게 안내).
    """
    check(path_key)
    try:
        db.request_wake()
    except Exception as e:  # noqa: BLE001
        db.log_error("service", str(e), kind=type(e).__name__,
                     path=request.path, detail=traceback.format_exc())
    return redirect(url_for("home", path_key=path_key))


@app.route("/<path_key>/status")
def status(path_key):
    """화면이 5초마다 물어보는 진행 상황(JSON). 수집이 끝나면 새로고침한다."""
    check(path_key)
    try:
        return jsonify({"worker": _worker_view(), "job": _job_view(db.latest_job())})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)[:150]}), 200


@app.route("/<path_key>/review/<int:review_id>/save", methods=["POST"])
def save_draft(path_key, review_id):
    check(path_key)
    text = (request.form.get("draft") or "").strip()
    try:
        db.save_reply_draft(review_id, text)
    except Exception as e:  # noqa: BLE001 — 저장 실패를 조용히 넘기면 직원이
        # 고친 답글이 사라진 걸 모른다. 원인을 반드시 남긴다.
        db.log_error("service", f"답글 저장 실패(review {review_id}): {e}",
                     kind=type(e).__name__, path=request.path,
                     detail=traceback.format_exc())
    return redirect(url_for("home", path_key=path_key) + f"#r{review_id}")


@app.route("/<path_key>/review/<int:review_id>/regen", methods=["POST"])
def regen(path_key, review_id):
    """'AI 재생성' — 집 PC 일꾼에게 초안 재생성을 요청한다(잡 큐).

    생성은 일꾼이 하므로(웹서버엔 AI 키·생성 코드가 없다) 보통 15~30초 걸린다.
    화면 쪽 JS 가 draft_state 를 폴링해 새 초안이 오면 바꿔 넣는다.
    """
    check(path_key)
    try:
        db.request_regen(review_id)
        return jsonify({"ok": True})
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"재생성 요청 실패(review {review_id}): {e}",
                     kind=type(e).__name__, path=request.path,
                     detail=traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)[:150]}), 200


@app.route("/<path_key>/review/<int:review_id>/draft")
def draft_state(path_key, review_id):
    """초안 현재 상태 — 재생성 폴링용(JS 가 3초마다 확인)."""
    check(path_key)
    try:
        r = db.get_review(review_id) or {}
        # 오래 걸릴 때 화면이 '왜 안 되는지' 말해줄 수 있게 일꾼 상태도 함께.
        w = _worker_view()
        return jsonify({"draft": r.get("reply_draft") or "",
                        "at": r.get("draft_updated_at") or "",
                        "reply_status": r.get("reply_status") or "",
                        "worker_alive": bool(w.get("alive")),
                        "worker_text": w.get("text") or ""})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)[:150]}), 200


@app.route("/<path_key>/review/<int:review_id>/done", methods=["POST"])
def done(path_key, review_id):
    """(구 흐름 호환) 직접 등록 완료 표시 — 지금은 approve/skip 이 주 경로."""
    check(path_key)
    try:
        db.mark_replied(review_id)
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"등록완료 표시 실패(review {review_id}): {e}",
                     kind=type(e).__name__, path=request.path,
                     detail=traceback.format_exc())
    return redirect(url_for("home", path_key=path_key))


@app.route("/<path_key>/review/<int:review_id>/post", methods=["POST"])
def post_reply(path_key, review_id):
    """'답글 등록' — 이 초안 그대로 지금 바로 게시하라고 일꾼에게 요청.

    (2026-08-10: 정시 일괄 등록 대신 버튼 즉시 등록)
    approved 로 표시하고 post 잡을 넣는다. 결과는 JS 가 draft_state 를
    폴링해 posted(성공)/drafted(실패·리허설 복귀)로 확인한다.
    """
    check(path_key)
    try:
        db.mark_approved(review_id)
        db.request_post(review_id)
        return jsonify({"ok": True})
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"답글 등록 요청 실패(review {review_id}): {e}",
                     kind=type(e).__name__, path=request.path,
                     detail=traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)[:150]}), 200


@app.route("/<path_key>/history")
def history(path_key):
    """등록한 답글 확인·수정 화면 — 최근 등록순.

    등록 후에도 마음에 안 들면 여기서 고쳐 재게시할 수 있다(사장님 요청
    2026-08-12).
    """
    check(path_key)
    error, rows = None, []
    plat = (request.args.get("plat") or "").strip()   # baemin|coupang|빈값(전체)
    sort = (request.args.get("sort") or "").strip()   # rating|빈값(최신순)
    try:
        for r in db.get_posted_reviews(limit=100):
            if plat and r.get("platform") != plat:
                continue
            v = _review_view(r)
            v["posted_at"] = (r.get("posted_at") or "")[:16].replace("T", " ")
            rows.append(v)
        if sort == "rating":
            # 낮은 별점 먼저 — 문제 리뷰의 답글을 먼저 점검하기 위함.
            rows.sort(key=lambda v: (v.get("rating") or 0, v.get("posted_at") or ""))
    except Exception as e:  # noqa: BLE001
        error = f"데이터를 불러오지 못했어요: {str(e)[:150]}"
    return render_template("history.html", key=path_key, rows=rows,
                           error=error, plat=plat, sort=sort)


PAGE_SIZE = 30


@app.route("/<path_key>/reviews")
def reviews_all(path_key):
    """전체 리뷰 관리 — 수집된 모든 리뷰를 조건으로 찾아본다(사장님 요청).

    답글 화면(/)은 '지금 답글 달 것'만 보여주므로, 지난 리뷰를 찾아보거나
    답글 유무를 확인할 데가 없었다. 여기서 플랫폼·별점·답글유무·검색어로
    전체를 훑는다.
    """
    check(path_key)
    plat = request.args.get("plat") or None
    sort = request.args.get("sort") or "new"
    q = (request.args.get("q") or "").strip() or None
    rating = request.args.get("rating", type=int)
    rep = request.args.get("replied")
    replied = True if rep == "y" else False if rep == "n" else None
    page = max(1, request.args.get("page", default=1, type=int))

    error, rows, total = None, [], 0
    try:
        found, total = db.search_reviews(
            platform=plat, rating=rating, replied=replied, q=q, sort=sort,
            limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
        for r in found:
            v = _review_view(r)
            v["replied"] = (r.get("reply_status") == "posted"
                            or bool(r.get("platform_replied")))
            v["reply_text"] = (r.get("reply_draft")
                               or r.get("platform_reply") or "")
            v["status"] = r.get("reply_status") or ""
            rows.append(v)
    except Exception as e:  # noqa: BLE001
        error = f"리뷰를 불러오지 못했어요: {str(e)[:150]}"

    return render_template(
        "reviews.html", key=path_key, rows=rows, error=error, total=total,
        plat=plat, sort=sort, q=q or "", rating=rating, rep=rep or "",
        page=page, pages=max(1, -(-total // PAGE_SIZE)),
        worker=_worker_view(), job=_job_view(db.latest_job()),
    )


@app.route("/<path_key>/collect-all", methods=["POST"])
def collect_all(path_key):
    """'전체 리뷰 수집' — 남아 있는 리뷰를 끝까지 긁어오라고 요청한다."""
    check(path_key)
    try:
        db.request_collect_all(by="직원")
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"전체 수집 요청 실패: {e}",
                     kind=type(e).__name__, path=request.path,
                     detail=traceback.format_exc())
    return redirect(url_for("reviews_all", path_key=path_key))


@app.route("/<path_key>/review/<int:review_id>/edit_post", methods=["POST"])
def edit_post(path_key, review_id):
    """게시된 답글 수정 — 새 내용 저장 후 일꾼에게 재게시 요청."""
    check(path_key)
    text = (request.form.get("draft") or "").strip()
    try:
        if not text:
            return jsonify({"ok": False, "error": "내용이 비어 있어요"}), 200
        db.save_reply_draft(review_id, text, status="posted")
        job = db.request_post_edit(review_id)
        # 잡 id 를 넘겨 화면이 '그 잡'을 정확히 따라가게 한다 — 문구로 찾으면
        # 일꾼이 남긴 오류(예: 잡 종류를 모름)를 놓친다.
        return jsonify({"ok": True, "job": (job or {}).get("id")})
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"답글 수정 요청 실패(review {review_id}): {e}",
                     kind=type(e).__name__, path=request.path,
                     detail=traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)[:150]}), 200


@app.route("/<path_key>/review/<int:review_id>/edit_state")
def edit_state(path_key, review_id):
    """답글 수정 진행 상태 — 최근 post_edit 잡의 status/message."""
    check(path_key)
    job_id = request.args.get("job", type=int)
    try:
        j = (db.get_job(job_id) if job_id
             else db.latest_review_job("post_edit", review_id)) or {}
        msg = (j.get("message") or "")[:300]
        if "알 수 없는 잡 종류" in msg:
            msg = ("집 PC 프로그램이 옛 버전이라 '답글 수정'을 모릅니다. "
                   "집 PC에서 5_자동등록_고치기.bat 을 한 번 실행해 주세요.")
        return jsonify({"status": j.get("status") or "", "message": msg,
                        "worker_alive": bool(_worker_view().get("alive"))})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)[:150]}), 200


@app.route("/<path_key>/review/<int:review_id>/skip", methods=["POST"])
def skip(path_key, review_id):
    """'넘어가기' — 이미 앱에서 직접 등록했거나 답글 불필요(학습 제외)."""
    check(path_key)
    try:
        db.mark_skipped(review_id)
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"넘어가기 표시 실패(review {review_id}): {e}",
                     kind=type(e).__name__, path=request.path,
                     detail=traceback.format_exc())
    return redirect(url_for("home", path_key=path_key))


# ---------------------------------------------------------------------------
# 블로그 (📝) — 화면·버튼은 여기, 무거운 일은 집 PC 일꾼이 한다
# ---------------------------------------------------------------------------

from database import blog_store as blog  # noqa: E402


def _blog_job_view(job) -> dict | None:
    """블로그 작업 진행 상태를 화면용으로."""
    if not job:
        return None
    label = {
        "blog_recommend": "글감 추천", "blog_draft": "초안 작성",
        "blog_publish": "네이버 초안 넣기", "blog_rank": "순위 확인",
    }.get(job.get("kind"), job.get("kind") or "")
    return {
        "kind": label,
        "status": job.get("status"),
        "busy": job.get("status") in ("pending", "running"),
        "message": job.get("message") or "",
    }


@app.route("/<path_key>/blog/")
def blog_home(path_key):
    check(path_key)
    error = None
    posts, recs, ranks, job = [], [], [], None
    try:
        posts = blog.list_posts(limit=50)
        recs = blog.list_recommendations()
        ranks = blog.latest_ranks()
        job = _blog_job_view(blog.latest_blog_job())
    except Exception as e:  # noqa: BLE001
        error = f"데이터를 불러오지 못했어요: {str(e)[:150]}"
        db.log_error("service", f"블로그 화면 로드 실패: {e}",
                     kind=type(e).__name__, path=request.path,
                     detail=traceback.format_exc())
    return render_template("blog.html", key=path_key, posts=posts, recs=recs,
                           ranks=ranks, job=job, worker=_worker_view(), error=error)


def _ask_worker(path_key, kind, payload=None):
    """집 PC 일꾼에게 작업을 요청하고 블로그 홈으로 돌아간다."""
    try:
        blog.request_blog_job(kind, payload or {}, by="web")
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"블로그 작업 요청 실패({kind}): {e}",
                     kind=type(e).__name__, path=request.path,
                     detail=traceback.format_exc())
    return redirect(url_for("blog_home", path_key=path_key))


@app.route("/<path_key>/blog/recommend", methods=["POST"])
def blog_recommend(path_key):
    check(path_key)
    return _ask_worker(path_key, "blog_recommend")


@app.route("/<path_key>/blog/rank", methods=["POST"])
def blog_rank(path_key):
    check(path_key)
    return _ask_worker(path_key, "blog_rank")


@app.route("/<path_key>/blog/draft", methods=["POST"])
def blog_draft(path_key):
    check(path_key)
    subs = [s.strip() for s in (request.form.get("sub_keywords") or "").split(",") if s.strip()]
    payload = {
        "topic": request.form.get("title") or "",
        "title": request.form.get("title") or "",
        "post_type": request.form.get("post_type") or "정보성",
        "main_keyword": request.form.get("main_keyword") or "",
        "sub_keywords": subs,
    }
    return _ask_worker(path_key, "blog_draft", payload)


@app.route("/<path_key>/blog/post/<int:post_id>")
def blog_post(path_key, post_id):
    check(path_key)
    post = None
    error = None
    try:
        post = blog.get_post(post_id)
    except Exception as e:  # noqa: BLE001
        error = f"글을 불러오지 못했어요: {str(e)[:150]}"
    if post is None and error is None:
        abort(404)
    return render_template("blog_post.html", key=path_key, post=post or {}, error=error)


@app.route("/<path_key>/blog/post/<int:post_id>/save", methods=["POST"])
def blog_post_save(path_key, post_id):
    check(path_key)
    try:
        blog.update_post(post_id, body=request.form.get("body", ""))
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"블로그 글 저장 실패(post {post_id}): {e}",
                     kind=type(e).__name__, path=request.path,
                     detail=traceback.format_exc())
    return redirect(url_for("blog_post", path_key=path_key, post_id=post_id))


@app.route("/<path_key>/blog/post/<int:post_id>/publish", methods=["POST"])
def blog_post_publish(path_key, post_id):
    """집 PC 일꾼에게 '네이버 임시저장' 요청. 실제 발행 버튼은 사장님이 직접."""
    check(path_key)
    try:
        blog.request_blog_job("blog_publish", {"post_id": post_id}, by="web")
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"네이버 초안 요청 실패(post {post_id}): {e}",
                     kind=type(e).__name__, path=request.path,
                     detail=traceback.format_exc())
    return redirect(url_for("blog_post", path_key=path_key, post_id=post_id))


@app.route("/<path_key>/blog/post/<int:post_id>/status", methods=["POST"])
def blog_post_status(path_key, post_id):
    check(path_key)
    new = request.form.get("status", "ready")
    try:
        if new == "scheduled":
            blog.set_status(post_id, "scheduled", request.form.get("scheduled_at") or None)
        else:
            blog.set_status(post_id, new)
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"블로그 상태 변경 실패(post {post_id}): {e}",
                     kind=type(e).__name__, path=request.path,
                     detail=traceback.format_exc())
    if new == "trashed":
        return redirect(url_for("blog_home", path_key=path_key))
    return redirect(url_for("blog_post", path_key=path_key, post_id=post_id))


# ---------------------------------------------------------------------------
# 메뉴 정본 · 원가/마진 관리
# ---------------------------------------------------------------------------

@app.route("/<path_key>/menu")
def menu_page(path_key):
    check(path_key)
    return render_template("menu.html", key=path_key)


@app.route("/<path_key>/menu/tasks")
def menu_tasks(path_key):
    """채널별 수정 작업지시서 — 정본 vs 채널 스냅샷 차이를 체크리스트로."""
    check(path_key)
    return render_template("menu_tasks.html", key=path_key)


@app.route("/<path_key>/menu/ingredients")
def menu_ingredients(path_key):
    """자재(원부자재)·레시피 관리 — 원가 자동 계산의 근거."""
    check(path_key)
    return render_template("menu_ingredients.html", key=path_key)


@app.route("/<path_key>/menu/ingredients/seed", methods=["POST"])
def menu_ingredients_seed(path_key):
    """자재·레시피 초기 데이터 주입 — 웹에서 1회 클릭. 재실행해도 안전."""
    check(path_key)
    try:
        import json as _json
        spec_path = ROOT / "data" / "ingredients_seed.json"
        spec = _json.loads(spec_path.read_text(encoding="utf-8"))
        result = db.seed_ingredients_bulk(spec)
        return jsonify({"ok": True, **result})
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"자재 시드 실패: {e}", kind=type(e).__name__,
                     path=request.path, detail=traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/<path_key>/menu/ingredient", methods=["POST"])
def menu_ingredient_save(path_key):
    check(path_key)
    body = request.get_json(force=True) or {}
    try:
        row = db.ingredient_upsert(body, body.get("id"))
    except db.DuplicateIngredient as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    try:
        affected = db.skus_using_ingredient(row["id"]) if row else []
        updated = db.recompute_costs(affected) if affected else {}
        updated = _prep_cascade(list(updated), updated)
        return jsonify({"ok": True, "ingredient": row, "recomputed": updated})
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"자재 저장 실패: {e}", kind=type(e).__name__,
                     path=request.path, detail=traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/<path_key>/menu/ingredient/<int:ing_id>/delete", methods=["POST"])
def menu_ingredient_delete(path_key, ing_id):
    check(path_key)
    try:
        db.ingredient_delete(ing_id)
        return jsonify({"ok": True})
    except Exception as e:  # noqa: BLE001 — 레시피에서 사용 중이면 여기로 온다
        return jsonify({"ok": False,
                        "error": "레시피에서 사용 중이라 삭제할 수 없습니다. "
                                 "먼저 해당 메뉴 레시피에서 빼주세요."}), 400


@app.route("/<path_key>/menu/prep", methods=["POST"])
def menu_prep_create(path_key):
    """반제품 만들기 — 자재 1줄 + 제조 레시피용 항목 1줄을 한 번에."""
    check(path_key)
    body = request.get_json(force=True) or {}
    try:
        out = db.prep_create(body.get("name"), body.get("yield_qty"),
                             body.get("unit") or "g")
        return jsonify({"ok": True, **out})
    except db.DuplicateIngredient as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"반제품 생성 실패: {e}", kind=type(e).__name__,
                     path=request.path, detail=traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


def _prep_cascade(skus, updated):
    """반제품 원가가 움직였으면 그 자재값과 하위 메뉴 원가까지 흘려보낸다.

    두 갈래 모두 여기로 온다 — 반제품 레시피를 직접 고친 경우, 그리고
    반제품에 들어가는 자재(크림치즈 등) 가격을 고쳐 반제품 원가가 바뀐 경우.
    """
    if isinstance(skus, str):
        skus = [skus]
    skus = [s for s in (skus or []) if s]
    if not skus:
        return updated
    try:
        _, more = db.prep_sync(skus)
        updated = {**updated, **more}
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"반제품 연쇄 재계산 실패({skus}): {e}",
                     kind=type(e).__name__, detail=traceback.format_exc())
    return updated


@app.route("/<path_key>/menu/ingredients/batch", methods=["POST"])
def menu_ingredients_batch(path_key):
    """자재 여러 줄 한 번에 저장 — 엑셀처럼 붙여넣은 뒤 한 번에 커밋."""
    check(path_key)
    body = request.get_json(force=True) or {}
    rows = body.get("rows") or []
    saved, errors = [], []
    affected = set()
    for r in rows:
        try:
            row = db.ingredient_upsert(r, r.get("id"))
            saved.append(row)
            for sku in db.skus_using_ingredient(row["id"]):
                affected.add(sku)
        except db.DuplicateIngredient as e:
            errors.append({"id": r.get("id"), "name": r.get("name"), "error": str(e)})
        except Exception as e:  # noqa: BLE001
            errors.append({"id": r.get("id"), "name": r.get("name"), "error": str(e)[:200]})
    try:
        updated = db.recompute_costs(list(affected)) if affected else {}
        updated = _prep_cascade(list(updated), updated)
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"자재 일괄저장 재계산 실패: {e}", kind=type(e).__name__,
                     path=request.path, detail=traceback.format_exc())
        updated = {}
    return jsonify({"ok": True, "saved": saved, "errors": errors, "recomputed": updated})


@app.route("/<path_key>/menu/recipe/batch", methods=["POST"])
def menu_recipe_batch(path_key):
    """한 메뉴의 레시피 사용량 여러 줄 한 번에 저장."""
    check(path_key)
    body = request.get_json(force=True) or {}
    sku = body.get("sku")
    lines = body.get("lines") or []
    saved, errors = [], []
    for ln in lines:
        try:
            row = db.recipe_upsert(sku, ln["ingredient_id"], ln["qty"])
            saved.append(row)
        except Exception as e:  # noqa: BLE001
            errors.append({"ingredient_id": ln.get("ingredient_id"), "error": str(e)[:200]})
    try:
        updated = db.recompute_costs([sku], force=True) if sku else {}
        if sku:
            updated = _prep_cascade(sku, updated)
    except Exception as e:  # noqa: BLE001
        updated = {}
    return jsonify({"ok": True, "saved": saved, "errors": errors, "recomputed": updated})


@app.route("/<path_key>/menu/recipe", methods=["POST"])
def menu_recipe_save(path_key):
    check(path_key)
    body = request.get_json(force=True) or {}
    try:
        row = db.recipe_upsert(body["sku"], body["ingredient_id"], body["qty"])
        # 레시피를 사람이 직접 고친 것 — 수기 원가보다 레시피가 최신 의사표시.
        updated = db.recompute_costs([body["sku"]], force=True)
        updated = _prep_cascade(body["sku"], updated)
        return jsonify({"ok": True, "line": row, "recomputed": updated})
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"레시피 저장 실패: {e}", kind=type(e).__name__,
                     path=request.path, detail=traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/<path_key>/menu/recipe/<int:rid>/delete", methods=["POST"])
def menu_recipe_delete(path_key, rid):
    check(path_key)
    try:
        sku = db.recipe_delete(rid)
        updated = db.recompute_costs([sku], force=True) if sku else {}
        if sku:
            updated = _prep_cascade(sku, updated)
        return jsonify({"ok": True, "recomputed": updated})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


def _safe(fn):
    """마이그레이션 전(테이블 없음)이면 빈 목록 — 화면이 죽지 않게."""
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return []


@app.route("/<path_key>/menu/data")
def menu_data(path_key):
    check(path_key)
    try:
        snapshots = _safe(db.menu_snapshots_all)
        return jsonify({
            "items": db.menu_all(),
            "channels": db.menu_channels_all(),
            "settings": db.menu_settings_all(),
            "snapshots": snapshots,
            "ingredients": _safe(db.ingredients_all),
            "recipes": _safe(db.recipes_all),
        })
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)[:200]}), 500


@app.route("/<path_key>/menu/aov")
def menu_aov(path_key):
    """실측 객단가 — orders 테이블의 실제 주문금액 평균(채널별)."""
    check(path_key)
    try:
        return jsonify(db.order_stats(days=90))
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)[:200]}), 200


@app.route("/<path_key>/menu/collect", methods=["POST"])
def menu_collect(path_key):
    """채널 노출 메뉴 수집을 집 PC 일꾼에게 요청."""
    check(path_key)
    try:
        db.request_menu_collect()
        return jsonify({"ok": True})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/<path_key>/menu/item/<sku>", methods=["POST"])
def menu_item_save(path_key, sku):
    check(path_key)
    try:
        db.menu_update_item(sku, request.get_json(force=True) or {})
        return jsonify({"ok": True})
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"메뉴 저장 실패({sku}): {e}",
                     kind=type(e).__name__, path=request.path,
                     detail=traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/<path_key>/menu/channel/<sku>/<channel>", methods=["POST"])
def menu_channel_save(path_key, sku, channel):
    check(path_key)
    if channel not in ("store", "baemin", "coupang", "naver"):
        abort(400)
    try:
        db.menu_upsert_channel(sku, channel, request.get_json(force=True) or {})
        return jsonify({"ok": True})
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"채널 오버라이드 저장 실패({sku}/{channel}): {e}",
                     kind=type(e).__name__, path=request.path,
                     detail=traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/<path_key>/menu/settings/<key>", methods=["POST"])
def menu_settings_save(path_key, key):
    check(path_key)
    if key not in ("channel_fees", "target_cost_rates", "order_model", "task_done"):
        abort(400)
    try:
        db.menu_set_setting(key, request.get_json(force=True))
        return jsonify({"ok": True})
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"메뉴 설정 저장 실패({key}): {e}",
                     kind=type(e).__name__, path=request.path,
                     detail=traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


if __name__ == "__main__":
    if not SERVICE_PATH:
        print("[!] SERVICE_PATH 가 없습니다. .env 에 비밀 주소 조각을 넣어주세요.")
        print("    예) SERVICE_PATH=k7m2x9qp")
        sys.exit(1)
    port = int(os.getenv("PORT", "5060"))
    print("=" * 56)
    print(" 베어글스 직원용 리뷰 답글 서비스")
    print(f" 열기 →  http://localhost:{port}/{SERVICE_PATH}/")
    print("=" * 56)
    app.run(host="0.0.0.0", port=port, debug=False)
