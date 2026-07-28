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
    label = {
        "pending": "수집 요청 접수됨 — 집 PC가 곧 시작해요",
        "running": "수집 중이에요… (30초쯤 걸려요)",
        "done": job.get("message") or "수집 완료",
        "error": _friendly_fail(job.get("message")),
    }.get(job.get("status"), job.get("status"))
    return {"status": job.get("status"), "text": label,
            "busy": job.get("status") in ("pending", "running")}


def _review_view(r: dict) -> dict:
    draft = r.get("reply_draft") or ""
    return {
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
    try:
        rows = [_review_view(r) for r in db.get_pending_reviews(limit=100)]
        # 초안이 있는 것만 보여준다 — 초안 없는 카드가 화면을 덮으면
        # 직원이 무엇을 해야 하는지 알 수 없다. 나머지는 건수로만 알린다.
        reviews = [r for r in rows if r["has_draft"]]
        waiting = len(rows) - len(reviews)
        job = _job_view(db.latest_job())
    except Exception as e:  # noqa: BLE001
        error = f"데이터를 불러오지 못했어요: {str(e)[:150]}"
        job = None
    return render_template(
        "staff.html", key=path_key, reviews=reviews, worker=_worker_view(),
        job=job, error=error, waiting=waiting,
    )


@app.route("/<path_key>/collect", methods=["POST"])
def collect(path_key):
    check(path_key)
    try:
        db.request_collect()
    except Exception as e:  # noqa: BLE001 — 화면은 상태로 안내, 원인은 기록
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


@app.route("/<path_key>/review/<int:review_id>/done", methods=["POST"])
def done(path_key, review_id):
    check(path_key)
    try:
        db.mark_replied(review_id)
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"등록완료 표시 실패(review {review_id}): {e}",
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
