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
import re
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from urllib.parse import parse_qsl  # noqa: E402

from dotenv import load_dotenv  # noqa: E402
from flask import (  # noqa: E402
    Flask, Request, abort, jsonify, redirect, render_template, request,
    url_for,
)
from werkzeug.utils import cached_property  # noqa: E402

# 설정은 service/.env 를 먼저 본다(클라우드 서버에는 이 파일만 올린다 —
# 집 PC 의 .env 에는 배민·쿠팡 비밀번호까지 들어 있어 올리면 안 된다).
# 집 PC 에서 테스트할 때는 service/.env 가 없으므로 루트 .env 를 쓴다.
load_dotenv(pathlib.Path(__file__).resolve().parent / ".env")
load_dotenv(ROOT / ".env")

from database import supabase_client as db  # noqa: E402

class _SafeRequest(Request):
    """query string 에 UTF-8 로 못 읽는 원문 바이트가 섞여 있어도 안 죽는다.

    오류 기록(2026-08-24, /todo): 옛 북마크·기기 인코딩 문제로 EUC-KR 등
    바이트가 그대로 섞여 오면 werkzeug 가 request.args 접근 시
    UnicodeDecodeError 를 던져 요청 전체가 500 으로 죽었다. 못 읽는 바이트만
    U+FFFD 로 바꿔서라도 나머지 파라미터는 정상 처리한다.
    """

    @cached_property
    def args(self):
        try:
            qs = self.query_string.decode()
        except UnicodeDecodeError:
            qs = self.query_string.decode("utf-8", "replace")
        return self.parameter_storage_class(
            parse_qsl(qs, keep_blank_values=True, errors="werkzeug.url_quote"))


app = Flask(__name__)
app.request_class = _SafeRequest

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


# ---------------------------------------------------------------------------
# 화면을 빠르게 — 동시에 조회하고, 자주 같은 답이 나오는 건 잠깐 재사용
# ---------------------------------------------------------------------------
# PythonAnywhere ↔ Supabase 왕복이 한 번에 0.3~1.3초다(서버 실측 2026-08-21).
# 화면 하나가 조회를 6번 하면 그게 그대로 더해져 5초가 된다. 그래서
#   ① 서로 필요 없는 조회는 **동시에** 돌리고(가장 느린 것 하나 시간만 든다)
#   ② 일꾼 상태·작업 상태·알림처럼 몇 초 사이 안 바뀌는 건 잠깐 캐시한다.
_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix="db")


@app.context_processor
def _filter_helpers():
    """화면에서 필터 링크를 만들 때 쓰는 도우미.

    예전엔 필터 링크마다 url_for(...) 에 나머지 조건을 손으로 다 나열했다 —
    조건을 하나 추가할 때마다 모든 링크를 고쳐야 해서, 새 필터를 넣기가
    사실상 불가능했다(그래서 필터가 몇 년째 4종뿐이었다).
    """
    def url_with(**over):
        args = {k: v for k, v in request.args.items() if v not in (None, "")}
        args.update(over)
        # 값이 비면 뺀다(=그 조건 해제). 조건이 바뀌면 1쪽부터 다시 본다.
        args = {k: v for k, v in args.items() if v not in (None, "", "all")}
        if "page" not in over:
            args.pop("page", None)
        return url_for(request.endpoint, **{**(request.view_args or {}), **args})

    def is_on(name, value=None):
        cur = request.args.get(name) or ""
        return cur == (str(value) if value is not None else "")
    return {"url_with": url_with, "is_on": is_on}


def _ajax() -> bool:
    """화면 JS 가 부른 요청인가.

    버튼이 <form> 이면 누를 때마다 POST → 리다이렉트 → **페이지 전체 재생성**
    이라 2초씩 멈춘 것처럼 보였다(사장님 2026-08-21 "액션이 다 느려").
    JS 가 부를 때는 JSON 만 돌려주고, 화면은 그 자리에서 카드를 지운다.
    """
    return request.headers.get("X-Requested-With") == "fetch"


def _done(path_key, target="todo", **extra):
    """동작 완료 응답 — JS 면 JSON, 평범한 폼 전송이면 원래대로 리다이렉트."""
    if _ajax():
        return jsonify({"ok": True, **extra})
    return redirect(url_for(target, path_key=path_key))


def gather(**calls) -> dict:
    """여러 조회를 동시에 실행해 결과를 이름표로 돌려준다.

    하나가 실패해도 나머지는 살린다(그 자리만 None). 화면 전체가 죽는 것보다
    숫자 하나가 비는 편이 낫다.
    """
    futures = {k: _POOL.submit(fn) for k, fn in calls.items()}
    out = {}
    for k, f in futures.items():
        try:
            out[k] = f.result(timeout=25)
        except Exception:  # noqa: BLE001
            out[k] = None
    return out


def cached(seconds: float):
    """같은 답을 몇 초 동안 재사용하는 아주 작은 캐시(인자 없는 함수용)."""
    def deco(fn):
        box = {"t": 0.0, "v": None}

        def wrap():
            now = time.monotonic()
            if box["v"] is not None and now - box["t"] < seconds:
                return box["v"]
            box["v"], box["t"] = fn(), now
            return box["v"]
        wrap.__name__ = fn.__name__
        wrap.__doc__ = fn.__doc__
        return wrap
    return deco


@cached(300)    # 천천히 변하는 값 — 화면 속도를 위해 5분 재사용
def _learning_cached():
    return db.learning_progress()


@cached(4)      # 화면이 5초마다 물어보는 값 — 그 사이엔 같은 답이면 충분하다
def _latest_job_cached():
    return db.latest_job()


@cached(15)
def _tab_counts() -> dict:
    """리뷰 답글 탭 바(할 일·문제·등록함·전체)의 배지 숫자.

    화면 4개(todo/care/history/reviews)가 이제 같은 탭 바를 공유한다
    (사장님 지시 2026-08-27 — "4개 화면이 실무적으로 효과적인가?"). 어느
    탭에 있든 다른 탭에 뭐가 쌓였는지 보여야 해서, 페이지마다 두 건수를
    함께 센다. id 만 받아 가볍게 — 배지는 카드 본문이 필요 없다.
    """
    g = gather(
        todo=lambda: db.count_pending(with_draft=True),
        prob=lambda: db.get_attention_reviews(replied=False, limit=1,
                                              select="id")[1],
    )
    return {"todo": g["todo"] or 0, "prob": g["prob"] or 0}


@cached(4)
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


def _why_post_failed(msg) -> str:
    """답글 등록 실패 사유를 '무엇을 하면 되는지'로 바꾼다.

    원문('CDP attach 실패(127.0.0.1:9222)')을 그대로 보여주면 직원은 할 수
    있는 게 없다. 반대로 사유를 아예 감추면(기존: '등록이 안 됐어요') 사장님도
    원인을 못 찾는다 — 원인별 한 줄 + 할 일로 옮긴다(2026-08-13).
    """
    m = msg or ""
    if not m:
        return ""
    if "알 수 없는 잡 종류" in m:
        return ("집 PC 프로그램이 옛 버전이에요. 집 PC에서 0_업데이트.bat 을 "
                "한 번 실행해 주세요.")
    if "WRITE_DRY_RUN" in m or "리허설" in m or "연습" in m:
        return ("집 PC가 '연습 모드'라 실제로 등록되지 않았어요. "
                "5_자동등록_고치기.bat 을 실행해 주세요.")
    if "CDP" in m or "attach" in m or "Chrome" in m or "크롬" in m:
        return "집 PC 크롬이 안 켜져 있어요. 잠시 뒤 다시 눌러주세요."
    if "세션" in m or "SessionExpired" in m or "로그인" in m:
        return "배민·쿠팡 로그인이 풀렸어요. 사장님께 알려주세요."
    if "에스컬레이션" in m or "민감" in m:
        return "민감한 리뷰라 자동 등록이 막혀 있어요. 사장님이 직접 답해야 해요."
    if "카드" in m or "찾지 못" in m or "일치하지" in m:
        return ("배민·쿠팡 화면에서 이 리뷰를 못 찾았어요. 리뷰수집을 한 번 "
                "누른 뒤 다시 시도해주세요.")
    if "credit" in m.lower() or "크레딧" in m:
        return "AI 사용량이 소진됐어요. 사장님께 알려주세요."
    return m[:160]


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


# ── 알림함 ──────────────────────────────────────────────────────────────
# notify_owner(alerts.py)가 error_log 에 남긴 '사장님이 봐야 할 일'을 화면에
# 띄운다. 텔레그램을 걷어낸 뒤(2026-08-13) 알림을 보여주는 화면이 없어서
# 민감 리뷰·세션 만료가 조용히 묻혔다(2026-08-16 점검). 기술 오류(새벽 점검
# 몫)와 섞이지 않게 사장님용 kind 만 화이트리스트로 고른다.
OWNER_ALERT_KINDS = ("SeriousReview", "SessionExpired", "ReplyReplaced",
                     "Notice", "StuckApprovedRevived")


@cached(15)
def _owner_alerts(limit=5) -> list[dict]:
    """미확인 알림(최신순, 최대 limit건). 실패해도 화면은 뜬다."""
    try:
        rows = db.get_errors(only_unfixed=True, limit=50)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rows:
        if r.get("kind") not in OWNER_ALERT_KINDS:
            continue
        out.append({"id": r.get("id"),
                    "kind": r.get("kind"),
                    "at": (r.get("at") or "")[:16].replace("T", " "),
                    "message": (r.get("message") or "").strip()})
        if len(out) >= limit:
            break
    return out


@app.route("/<path_key>/alert/<int:alert_id>/ack", methods=["POST"])
def ack_alert(path_key, alert_id):
    """알림 [확인] — 다시 안 보이게 닫는다."""
    check(path_key)
    try:
        db.mark_error_fixed(alert_id, "사장님이 화면에서 확인")
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"알림 확인 실패({alert_id}): {e}",
                     kind=type(e).__name__, path=request.path)
    if _ajax():
        return jsonify({"ok": True})
    return redirect(request.referrer or url_for("home", path_key=path_key))


# 플랫폼 리뷰 관리 페이지 — '실제 답글 보러가기' 바로가기용(리뷰별 딥링크는
# 두 플랫폼 다 제공하지 않아 리뷰 목록 페이지로 보낸다).
# 일꾼이 예약분을 올리는 시각(worker.agent.SCHEDULED_POST_TIMES 와 같은 값을
# 본다). 화면에 '언제 올라가는지'를 그대로 적어 주기 위한 것이라, 일꾼과
# 화면이 다른 기계에서 돌아도 .env 만 맞춰 두면 된다.
SCHEDULED_POST_AT = (os.getenv("WORKER_SCHEDULED_POST_TIMES", "09:00")
                     .split(",")[0].strip() or "09:00")
SCHEDULED_POST_LABEL = f"아침 {SCHEDULED_POST_AT}"


def _is_night() -> bool:
    """지금이 '답글이 나가면 곤란한' 시간대인가 — 22시~아침 8시.

    이 시간대에는 [🌙 아침에 등록]을 기본 버튼으로 강조한다. 손님 폰에
    새벽 푸시가 울리는 걸 실수로 보내지 않게 하는 장치다.
    """
    h = datetime.now().hour
    return h >= 22 or h < 8


def scheduled_post_when() -> str:
    """지금 예약하면 언제 올라가는지 — '오늘 아침 9시' / '내일 아침 9시'."""
    try:
        hh, mm = (int(x) for x in SCHEDULED_POST_AT.split(":"))
    except ValueError:
        hh, mm = 9, 0
    now = datetime.now()
    today = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    day = "오늘" if now < today else "내일"
    return f"{day} 아침 {hh}시" + (f" {mm}분" if mm else "")


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
        # 민감 리뷰 판정은 **저장된 유형(kind)** 으로 한다. 예전엔 초안이
        # '⚠️'로 시작하는지로 봤는데, 이제 민감 리뷰에도 1차 가이드 초안을
        # 주므로(2026-08-16) 그 표시가 사라졌다. 옛 데이터 호환으로 ⚠️도 함께 본다.
        "escalate": (r.get("kind") == "escalate"
                     or draft.strip().startswith("⚠️")),
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
    if not isinstance(count, int):
        # 배민 raw 는 JSON 이 아니라 카드 텍스트다 — '3회 주문 고객'에서 뽑는다.
        # 이걸 안 해서 배민 리뷰는 주문 횟수가 아예 안 보였다(2026-08-24).
        m = re.search(r"(\d+)\s*회\s*주문", r.get("raw") or "")
        count = int(m.group(1)) if m else None
    delivery = r.get("delivery_type") or raw.get("orderType") or ""
    count = count if isinstance(count, int) and count > 0 else None
    return {
        "order_no": order_no,
        "ordered_at": str(ordered).replace("T", " ")[:16],
        "order_count": count,
        "visit": _visit_label(count),
        "visit_class": _visit_class(count),
        "delivery": {"REGULAR": "배달", "TAKE_OUT": "포장"}.get(delivery, delivery),
    }


# 답글 수정 기한 — 배민은 등록 후 30일이 지나면 "사장님 댓글을 등록할 수
# 없어요"로 막힌다(플랫폼 안내 문구로 실측). 기한이 지난 답글에 고치기·AI
# 버튼을 띄우면 눌러 봐야 실패한다 → 기본 목록에서 뺀다(사장님 지시
# 2026-08-25). 일꾼 쪽 WORKER_REPLY_WINDOW_DAYS 와 같은 기준.
REPLY_EDIT_DAYS = int(os.getenv("REPLY_EDIT_DAYS", "30"))


def _days_since(date_str):
    try:
        return (datetime.now().date()
                - datetime.fromisoformat(date_str).date()).days
    except Exception:  # noqa: BLE001
        return None


def _is_expired(date_str):
    """이 리뷰의 답글 수정 기한이 지났는지(모르면 False — 막지 않는다)."""
    age = _days_since(date_str)
    return age is not None and age > REPLY_EDIT_DAYS


def _visit_label(n):
    """'몇 번째 주문 고객'인지 한눈에 — 답글 말투가 달라지는 기준이다.

    38번째 주문한 분에게 처음 오신 것처럼 답하면 안 되고, 첫 주문인 분께
    '또 찾아주셔서'라고 하면 더 이상하다(사장님 강조).
    """
    if not n:
        return ""
    if n == 1:
        return "🆕 첫 주문"
    if n < 5:
        return f"🔁 {n}번째 주문"
    if n < 10:
        return f"💛 단골 · {n}번째"
    return f"👑 VIP · {n}번째"


def _visit_class(n):
    if not n:
        return ""
    return "new" if n == 1 else ("vip" if n >= 10 else
                                 ("reg" if n >= 5 else "again"))


# ---------------------------------------------------------------------------
# 화면
# ---------------------------------------------------------------------------

# 홈 바로가기 카드에 담당자를 달 수 있는 프로그램들(사장님 요청 2026-08-26).
HOME_PROGRAMS = ("review", "blog", "menu", "insta", "place", "meeting")

KST = timezone(timedelta(hours=9))


@app.route("/<path_key>/")
def home(path_key):
    """홈 — 비서 전체의 현관(리뉴얼 2026-08-26).

    프로그램 바로가기가 주인공. 리뷰 현황 대시보드는 /review 로 옮겼고
    (없어진 기능 없음), 집 PC 상태·수집 버튼도 그 화면에 있다.
    """
    check(path_key)
    error = None
    # 서버는 UTC(PythonAnywhere) — 인사말·날짜는 매장 시간(KST)으로.
    now = datetime.now(KST)
    if 5 <= now.hour < 11:
        greet = "좋은 아침이에요 ☀️"
    elif 11 <= now.hour < 17:
        greet = "좋은 오후예요 🥯"
    elif 17 <= now.hour < 22:
        greet = "오늘도 수고 많았어요 🌙"
    else:
        greet = "늦은 시간까지 고생 많아요 🌙"
    today = f"{now.month}월 {now.day}일 {'월화수목금토일'[now.weekday()]}요일"

    stat, owners, blog_ready = {}, {}, 0
    try:
        g = gather(
            todo_baemin=lambda: db.count_pending(with_draft=True, platform="baemin"),
            todo_coupang=lambda: db.count_pending(with_draft=True, platform="coupang"),
            escalate=lambda: db.count_pending(with_draft=True, escalate=True),
            oldest=db.oldest_pending_date,
            blog_ready=lambda: blog.count_posts("ready"),
            learning=_learning_cached,
            alerts=_owner_alerts,
            owners=lambda: db.get_setting("home_owners", {}) or {},
            # 회의에서 정한 할 일도 홈에서 챙긴다(사장님 결정 2026-08-27).
            # 표가 아직 없으면 gather 가 None 으로 돌려주고 홈은 그대로 뜬다.
            meet_tasks=lambda: mt.open_tasks(limit=6),
            meet_open=mt.open_task_count,
        )
        stat = {
            "todo": (g["todo_baemin"] or 0) + (g["todo_coupang"] or 0),
            "todo_baemin": g["todo_baemin"] or 0,
            "todo_coupang": g["todo_coupang"] or 0,
            "escalate": g["escalate"] or 0,
        }
        oldest = g["oldest"]
        stat["oldest_days"] = (
            (now.date() - datetime.fromisoformat(oldest).date()).days
            if oldest else None)
        blog_ready = g["blog_ready"] or 0
        owners = g["owners"] or {}
    except Exception as e:  # noqa: BLE001
        error = f"현황을 불러오지 못했어요: {str(e)[:150]}"
        g = {}
    return render_template(
        "home.html", key=path_key, stat=stat, greet=greet, today=today,
        blog_ready=blog_ready, owners=owners,
        learning=g.get("learning"), error=error, alerts=g.get("alerts") or [],
        meet_tasks=g.get("meet_tasks") or [], meet_open=g.get("meet_open") or 0,
    )


@app.route("/<path_key>/home/owner", methods=["POST"])
def home_owner_save(path_key):
    """홈 바로가기 카드의 담당자 태그 저장 — 빈 이름이면 태그 제거."""
    check(path_key)
    data = request.get_json(force=True, silent=True) or {}
    program = data.get("program")
    name = (data.get("name") or "").strip()[:20]
    if program not in HOME_PROGRAMS:
        abort(400)
    try:
        owners = db.get_setting("home_owners", {}) or {}
        if name:
            owners[program] = name
        else:
            owners.pop(program, None)
        db.menu_set_setting("home_owners", owners)
        return jsonify({"ok": True, "name": name})
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"담당자 저장 실패({program}): {e}",
                     kind=type(e).__name__, path=request.path,
                     detail=traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/<path_key>/review")
def review_home(path_key):
    """리뷰 현황 대시보드 — 지금 상태를 한눈에(사장님 요청 2026-08-16).

    2026-08-26 홈 리뉴얼로 첫 화면(/)에서 여기로 이사. 내용은 그대로다.
    실제 작업은 하위 화면에서: ①등록해야 할 답글(/todo) ②등록한 답글(/history).
    """
    check(path_key)
    error, stat = None, {}
    # 숫자만 필요한 화면이다 — 리뷰를 통째로 받지 않고 개수만 세고,
    # 서로 무관한 조회는 동시에 돌린다(2026-08-21 속도 개선: 5.2초 → 1초대).
    try:
        g = gather(
            todo_baemin=lambda: db.count_pending(with_draft=True, platform="baemin"),
            todo_coupang=lambda: db.count_pending(with_draft=True, platform="coupang"),
            escalate=lambda: db.count_pending(with_draft=True, escalate=True),
            waiting=lambda: db.count_pending(with_draft=False),
            posting=lambda: db.count_by_status("approved"),
            posted=lambda: db.count_by_status("posted"),
            oldest=db.oldest_pending_date,
            job=_latest_job_cached,
            learning=_learning_cached,
            worker=_worker_view,
            alerts=_owner_alerts,
        )
        stat = {
            "todo": (g["todo_baemin"] or 0) + (g["todo_coupang"] or 0),
            "todo_baemin": g["todo_baemin"] or 0,
            "todo_coupang": g["todo_coupang"] or 0,
            # 사장님이 직접 대응해야 하는 민감 리뷰 — 가장 급한 항목이라 따로.
            "escalate": g["escalate"] or 0,
            "waiting": g["waiting"] or 0,            # 초안 생성 대기
            "posting": g["posting"] or 0,
            "posted": g["posted"] or 0,
        }
        # 가장 오래 기다린 리뷰 — 답글 기한 감각을 준다.
        oldest = g["oldest"]
        stat["oldest"] = oldest
        stat["oldest_days"] = (
            (datetime.now().date() - datetime.fromisoformat(oldest).date()).days
            if oldest else None)
        job = _job_view(g["job"])
    except Exception as e:  # noqa: BLE001
        error = f"현황을 불러오지 못했어요: {str(e)[:150]}"
        job, g = None, {}
    return render_template(
        "dashboard.html", key=path_key, stat=stat,
        learning=g.get("learning"),
        worker=g.get("worker") or _worker_view(),
        job=job, error=error, alerts=g.get("alerts") or [],
    )


@app.route("/<path_key>/todo")
def todo(path_key):
    """등록해야 할 답글 — 초안을 확인·수정하고 등록하는 실제 작업 화면.

    필터·정렬은 다른 리뷰 화면(전체 리뷰·관리 필요·등록한 답글)과 **똑같은
    방식**을 쓴다(사장님 지시 2026-08-24). 화면마다 필터가 다르면 직원이
    매번 다시 배워야 한다.
    """
    check(path_key)
    error, reviews, waiting = None, [], 0
    plat = (request.args.get("plat") or "").strip() or None
    sort = (request.args.get("sort") or "").strip() or "old"   # 기한 임박 먼저
    q = (request.args.get("q") or "").strip() or None
    rating = request.args.get("rating", type=int)
    rating_max = request.args.get("rating_max", type=int)
    kind = request.args.get("kind") or None
    days = request.args.get("days", type=int)

    filters = dict(platform=plat, rating=rating, rating_max=rating_max,
                   kind=kind, days=days, q=q, pending_only=True)
    try:
        g = gather(
            rows=lambda: db.search_reviews(has_draft=True, sort=sort,
                                           limit=100, **filters),
            waiting=lambda: db.search_reviews(has_draft=False, limit=1,
                                              **filters),
            approved=lambda: db.count_by_status("approved"),
            job=_latest_job_cached, worker=_worker_view, alerts=_owner_alerts)
        found, total = g["rows"] or ([], 0)
        reviews = []
        for row in found:
            v = _review_view(row)
            # '아침에 등록'으로 재워 둔 건 — 카드는 그대로 두고 배지만 바꾼다.
            v["scheduled"] = (row.get("reply_status") == "scheduled")
            reviews.append(v)
        trusted = _trusted_kinds()
        for r in reviews:
            r["trusted"] = (not r["escalate"]) and r.get("kind") in trusted
        # 초안이 아직 없는 리뷰는 카드로 덮지 않고 건수로만 알린다.
        waiting = (g["waiting"] or ([], 0))[1]
        job = _job_view(g["job"])
        approved_count = g["approved"] or 0
    except Exception as e:  # noqa: BLE001
        error = f"데이터를 불러오지 못했어요: {str(e)[:150]}"
        job, g = None, {}
        approved_count = 0
    return render_template(
        "staff.html", key=path_key, reviews=reviews,
        worker=g.get("worker") or _worker_view(),
        job=job, error=error, waiting=waiting, approved_count=approved_count,
        plat=plat, sort=sort, q=q or "", rating=rating, rating_max=rating_max,
        kind=kind, days=days, total=len(reviews),
        alerts=g.get("alerts") or [],
        active_tab="todo", tab_counts=_tab_counts(),
        # '아침에 등록' 버튼에 쓸 문구 + 밤에는 그 버튼을 기본으로 강조한다
        # (사장님 확정 2026-08-28: 22시~아침 8시는 예약이 기본).
        sched_when=scheduled_post_when(), night=_is_night(),
    )


@app.route("/<path_key>/guide")
def guide(path_key):
    """직원용 사용법 안내 — 정적 페이지(DB 안 씀)."""
    check(path_key)
    return render_template("guide.html", key=path_key)


@app.route("/<path_key>/place")
def place_guide(path_key):
    """네이버 스마트플레이스 실행 가이드 — 저장소 루트의 정적 HTML을 그대로 서빙.

    내용 수정은 루트의 `스마트플레이스-직원가이드.html` 을 고치고 git pull + Reload.
    """
    check(path_key)
    page = ROOT / "스마트플레이스-직원가이드.html"
    try:
        body = page.read_text(encoding="utf-8")
    except OSError:
        abort(404)
    sidebar = render_template("_sidebar.html", key=path_key)
    return (
        '<!doctype html>\n<html lang="ko">\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<body>\n{sidebar}\n{body}\n</body>\n</html>"
    )


@app.route("/<path_key>/instagram")
def instagram_info(path_key):
    """인스타 파이프라인 안내 — 파이프라인 자체는 집 PC 웹앱(5051)에서 돌아간다.

    여기(외부 서버)서는 영상 편집·파일 접근이 안 되므로, 메뉴 자리는 만들되
    무엇을 어디서 하는지 알려주는 안내 페이지만 둔다.
    """
    check(path_key)
    sidebar = render_template("_sidebar.html", key=path_key)
    return (
        '<!doctype html>\n<html lang="ko">\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<body style="margin:0;background:#faf9f7;color:#232320;'
        "font-family:-apple-system,'Malgun Gothic',sans-serif;\">\n"
        f"{sidebar}\n"
        '<div style="max-width:640px;margin:0 auto;padding:24px 16px;">\n'
        '<h2 style="font-size:18px;">🎬 인스타 파이프라인</h2>\n'
        '<div style="background:#fff;border:1px solid #e7e5de;border-radius:12px;'
        'padding:18px;line-height:1.8;font-size:14px;">\n'
        '릴스 기획 → 촬영 목록 → 자동 편집 → 완성본까지 만드는 파이프라인은\n'
        '<b>집 PC 웹앱</b>에서 돌아갑니다 (영상 파일을 다뤄야 해서 이 서버에서는 안 돼요).\n'
        '<ul style="margin:12px 0 0;padding-left:20px;">\n'
        '<li>집 안 와이파이: <code>http://집PC주소:5051/instagram</code></li>\n'
        '<li>밖에서: Tailscale 주소로 접속 (webapp/밖에서-쓰기.md 참고)</li>\n'
        '<li>촬영 주제·대본 아이디어는 담당자(사장님)께 요청</li>\n'
        '</ul>\n</div>\n</div>\n</body>\n</html>'
    )


@app.route("/<path_key>/collect", methods=["POST"])
def collect(path_key):
    check(path_key)
    try:
        db.request_collect()
    except Exception as e:  # noqa: BLE001 — 화면은 상태로 안내, 원인은 기록
        db.log_error("service", str(e), kind=type(e).__name__,
                     path=request.path, detail=traceback.format_exc())
        if _ajax():
            return jsonify({"ok": False, "error": str(e)[:150]})
    return _done(path_key)


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
    return redirect(url_for("todo", path_key=path_key))


@app.route("/<path_key>/status")
def status(path_key):
    """화면이 5초마다 물어보는 진행 상황(JSON). 수집이 끝나면 새로고침한다."""
    check(path_key)
    try:
        return jsonify({"worker": _worker_view(), "job": _job_view(db.latest_job())})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)[:150]}), 200


@app.route("/<path_key>/_perf")
def perf(path_key):
    """화면이 왜 느린지 **서버에서** 재 본다(진단용, 사장님 화면엔 안 나옴).

    집에서 재면 빠른데 화면은 느린 이유를 알려면, 실제로 돌아가는 곳
    (PythonAnywhere)에서 조회 하나하나의 시간을 재야 한다. 사장님 보고
    2026-08-21 "버튼이 다 느려".
    """
    check(path_key)
    import time as _t
    out, t0 = [], _t.perf_counter()

    def take(name, fn):
        s = _t.perf_counter()
        try:
            r = fn()
            n = len(r) if isinstance(r, list) else (1 if r else 0)
            err = ""
        except Exception as e:  # noqa: BLE001
            n, err = -1, str(e)[:80]
        out.append({"name": name, "ms": round((_t.perf_counter() - s) * 1000),
                    "rows": n, "error": err})

    take("pending(200)", lambda: db.get_pending_reviews(limit=200))
    take("approved(200)", lambda: db.get_approved_reviews(limit=200))
    take("posted(500)", lambda: db.get_posted_reviews(limit=500))
    take("latest_job", db.latest_job)
    take("worker_status", db.worker_status)
    take("errors(50)", lambda: db.get_errors(limit=50))
    take("search(30)", lambda: db.search_reviews(limit=30))
    take("pending 재조회", lambda: db.get_pending_reviews(limit=200))
    return jsonify({"total_ms": round((_t.perf_counter() - t0) * 1000),
                    "steps": out})


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
    return redirect(url_for("todo", path_key=path_key) + f"#r{review_id}")


@app.route("/<path_key>/regen-all", methods=["POST"])
def regen_all(path_key):
    """'전체 AI 재생성' — 화면에 보이는 대기 답글 초안을 한 번에 다시 만든다.

    말투 규칙이나 생성 로직을 고친 뒤 옛 초안이 남아 있으면 하나씩 눌러야
    했다(사장님 요청 2026-08-16). 민감 리뷰도 초안이 있으므로 함께 돌린다.
    """
    check(path_key)
    plat = (request.args.get("plat") or "").strip()
    try:
        n = 0
        for r in db.get_pending_reviews(limit=100):
            if not r.get("reply_draft"):
                continue                      # 아직 초안이 없으면 수집이 만든다
            if plat and r.get("platform") != plat:
                continue
            db.request_regen(r["id"], by="전체 재생성")
            n += 1
        return jsonify({"ok": True, "count": n})
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"전체 재생성 요청 실패: {e}",
                     kind=type(e).__name__, path=request.path,
                     detail=traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)[:150]}), 200


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
        job_id = request.args.get("job", type=int)
        job = db.get_job(job_id) if job_id else None
        if job is None:
            # 잡 번호를 안 넘겨도 실패 사유를 보여준다 — 화면은 리뷰 단위로
            # 폴링하므로 이 리뷰의 최근 등록 잡을 대신 본다(2026-08-16).
            try:
                job = db.latest_review_job("post", review_id)
            except Exception:  # noqa: BLE001
                job = None
        return jsonify({"draft": r.get("reply_draft") or "",
                        "at": r.get("draft_updated_at") or "",
                        "reply_status": r.get("reply_status") or "",
                        # 실제로 손님에게 나간 답글 본문 — 플랫폼에서 되읽어온
                        # 것(platform_reply)이 진짜이고, 방금 등록해 아직 못
                        # 읽어왔으면 우리가 보낸 초안이 곧 그 내용이다.
                        "posted_reply": (r.get("platform_reply")
                                         or r.get("reply_draft") or ""),
                        "worker_alive": bool(w.get("alive")),
                        "worker_text": w.get("text") or "",
                        "job_status": (job or {}).get("status") or "",
                        # 등록 완료 뒤 '진짜 달렸는지' 확인하러 갈 곳
                        "platform_url": PLATFORM_REVIEW_URL.get(
                            r.get("platform"), ""),
                        "why": _why_post_failed((job or {}).get("message"))})
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
    return redirect(url_for("todo", path_key=path_key))


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
        job = db.request_post(review_id)
        # 잡 id 를 넘겨 화면이 '그 잡'의 실패 사유까지 읽게 한다 — 사유 없이
        # '등록이 안 됐어요'만 뜨면 무엇을 고쳐야 할지 알 수 없다(2026-08-13).
        return jsonify({"ok": True, "job": (job or {}).get("id")})
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"답글 등록 요청 실패(review {review_id}): {e}",
                     kind=type(e).__name__, path=request.path,
                     detail=traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)[:150]}), 200


@app.route("/<path_key>/review/<int:review_id>/schedule", methods=["POST"])
def schedule_reply(path_key, review_id):
    """'아침에 등록' — 지금 올리지 않고 다음 아침 슬롯까지 재워 둔다.

    답글을 달면 손님 폰에 푸시가 간다. 새벽 3시에 울리는 푸시는 반갑지
    않고, 주문으로도 이어지지 않는다. 베어글스 주문은 오전 10~12시에
    몰리므로(실측), 9시에 일꾼이 한 건씩 올려 그 직전에 닿게 한다.
    """
    check(path_key)
    try:
        db.mark_scheduled(review_id)
        return jsonify({"ok": True, "at": SCHEDULED_POST_LABEL})
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"아침 등록 예약 실패(review {review_id}): {e}",
                     kind=type(e).__name__, path=request.path,
                     detail=traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)[:150]}), 200


@app.route("/<path_key>/review/<int:review_id>/unschedule", methods=["POST"])
def unschedule_reply(path_key, review_id):
    """'예약 취소' — 다시 평범한 할 일(초안)로 돌린다."""
    check(path_key)
    try:
        db.mark_drafted(review_id)
        return jsonify({"ok": True})
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"예약 취소 실패(review {review_id}): {e}",
                     kind=type(e).__name__, path=request.path,
                     detail=traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)[:150]}), 200


@app.route("/<path_key>/history")
def history(path_key):
    """등록한 답글 확인·수정 화면.

    등록 후에도 마음에 안 들면 여기서 고쳐 재게시할 수 있다(사장님 요청
    2026-08-12). 필터·정렬은 전체 리뷰 화면과 같은 방식(url_with)으로 쓴다
    — 예전엔 채널·별점 두 줄이 전부라 "지난달 불만 답글만" 같은 걸 볼 수
    없었다(사장님 지적 2026-08-23).
    """
    check(path_key)
    plat = (request.args.get("plat") or "").strip() or None
    sort = (request.args.get("sort") or "").strip() or "posted"
    q = (request.args.get("q") or "").strip() or None
    rating = request.args.get("rating", type=int)
    rating_max = request.args.get("rating_max", type=int)
    kind = request.args.get("kind") or None
    days = request.args.get("days", type=int)
    # 수정 기한이 지난 답글은 고칠 수 없으므로 기본으로 감춘다.
    show_expired = request.args.get("expired") == "1"
    page = max(1, request.args.get("page", default=1, type=int))
    window = None if (show_expired or days) else REPLY_EDIT_DAYS

    error, rows, total = None, [], 0
    try:
        # 예전엔 500건을 통째로 받아 파이썬에서 걸렀다(180KB, 1초+).
        # 이제 조건·쪽 나누기를 서버에서 한다.
        found, total = db.search_reviews(
            source="ours", platform=plat, rating=rating, rating_max=rating_max,
            kind=kind, days=(days or window), q=q, sort=sort,
            limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
        for r in found:
            v = _review_view(r)
            v["posted_at"] = (r.get("posted_at") or "")[:16].replace("T", " ")
            v["kind"] = r.get("kind") or ""
            v["expired"] = _is_expired(r.get("written_date"))
            rows.append(v)
    except Exception as e:  # noqa: BLE001
        error = f"데이터를 불러오지 못했어요: {str(e)[:150]}"
    pages = max(1, -(-total // PAGE_SIZE))
    return render_template("history.html", key=path_key, rows=rows,
                           error=error, plat=plat, sort=sort, total=total,
                           q=q or "", rating=rating, rating_max=rating_max,
                           kind=kind, days=days, expired=show_expired,
                           edit_days=REPLY_EDIT_DAYS,
                           page=min(page, pages), pages=pages,
                           active_tab="done", tab_counts=_tab_counts())


def _summarize_ratings(rows):
    """조건에 맞는 리뷰의 별점 요약 — 몇 점짜리가 얼마나 되는지 한눈에.

    목록만 보면 '4점 이하가 늘었나?' 같은 감이 안 온다. 별점만 따로 받아와
    (본문·원본 없이) 평균과 분포를 낸다 — payload 가 작아 느려지지 않는다.
    """
    ns = [r.get("rating") for r in (rows or []) if isinstance(r.get("rating"), int)]
    if not ns:
        return None
    dist = {n: ns.count(n) for n in (5, 4, 3, 2, 1)}
    return {"n": len(ns), "avg": round(sum(ns) / len(ns), 2), "dist": dist,
            "low": sum(v for k, v in dist.items() if k <= 4),
            "capped": len(ns) >= 1000}


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
    rating_max = request.args.get("rating_max", type=int)
    kind = request.args.get("kind") or None
    days = request.args.get("days", type=int)
    source = request.args.get("source") or None
    rep = request.args.get("replied")
    replied = True if rep == "y" else False if rep == "n" else None
    page = max(1, request.args.get("page", default=1, type=int))

    filters = dict(platform=plat, rating=rating, rating_max=rating_max,
                   kind=kind, days=days, source=source, replied=replied, q=q)

    error, rows, total, summary = None, [], 0, None
    try:
        # 목록과 요약(평균 별점·답글률)을 동시에 — 왕복 지연이 겹치지 않게.
        g = gather(
            page=lambda: db.search_reviews(
                sort=sort, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE,
                **filters),
            summary=lambda: db.search_reviews(count_only=True, limit=1000,
                                              **filters),
        )
        found, total = g["page"] or ([], 0)
        summary = _summarize_ratings((g["summary"] or ([], 0))[0])
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
        summary=summary, kind=kind, days=days, source=source,
        rating_max=rating_max,
        plat=plat, sort=sort, q=q or "", rating=rating, rep=rep or "",
        page=page, pages=max(1, -(-total // PAGE_SIZE)),
        worker=_worker_view(), job=_job_view(db.latest_job()),
        active_tab="all", tab_counts=_tab_counts(),
    )


@app.route("/<path_key>/care")
def care_reviews(path_key):
    """⚠️ 관리 필요 — 별점 5점 미만 + CS(불만·민감) 리뷰만 모아 본다.

    만점 리뷰에 묻혀 정작 손봐야 할 리뷰를 놓치지 않게 따로 뺀다
    (사장님 요청 2026-08-16).
    """
    check(path_key)
    mode = request.args.get("mode") or "all"      # all | low | cs
    plat = request.args.get("plat") or None
    sort = request.args.get("sort") or "new"
    days = request.args.get("days", type=int)
    rep = request.args.get("replied")
    # 기본(파라미터 없음)은 이제 '아직 답 안 한 것만' — 이 화면이 탭 바에서
    # 🚨 문제 탭이 됐고, 급한 것부터 보여야 한다(사장님 지시 2026-08-27).
    # '전체'는 명시적으로 replied=any 를 보낸다(_filters.html 참고).
    replied = True if rep == "y" else False if rep in (None, "", "n") else None
    page = max(1, request.args.get("page", default=1, type=int))

    error, rows, total = None, [], 0
    try:
        found, total = db.get_attention_reviews(
            platform=plat, mode=mode, sort=sort, days=days, replied=replied,
            limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
        for r in found:
            v = _review_view(r)
            v["kind"] = r.get("kind") or ""
            v["cs"] = v["kind"] in db.CS_KINDS
            v["replied"] = (r.get("reply_status") == "posted"
                            or bool(r.get("platform_replied")))
            v["reply_text"] = (r.get("reply_draft")
                               or r.get("platform_reply") or "")
            rows.append(v)
    except Exception as e:  # noqa: BLE001
        error = f"리뷰를 불러오지 못했어요: {str(e)[:150]}"

    return render_template(
        "care.html", key=path_key, rows=rows, error=error, total=total,
        mode=mode, plat=plat, sort=sort, page=page, days=days, rep=rep or "",
        pages=max(1, -(-total // PAGE_SIZE)), alerts=_owner_alerts(),
        active_tab="prob", tab_counts=_tab_counts(),
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
        if _ajax():
            return jsonify({"ok": False, "error": str(e)[:150]})
    return _done(path_key)


# ---------------------------------------------------------------------------
# 블로그 (📝) — 화면·버튼은 여기, 무거운 일은 집 PC 일꾼이 한다
# ---------------------------------------------------------------------------

from database import blog_store as blog  # noqa: E402


_PHOTO_MARK = re.compile(r"\[\s*([📷🎬])\s*([^\[\]\n]{1,200}?)\s*\]")


def _blog_photos(body: str) -> list[dict]:
    """본문에 박힌 사진 표시를 목록으로 뽑는다.

    초안을 만들 때 집 PC가 사진함에서 골라 `[📷 메뉴/잠봉뵈르.JPG]` 형태로
    본문에 박아 둔다. 웹(PythonAnywhere)은 드라이브 파일을 직접 못 읽으므로
    사진 자체가 아니라 **어떤 사진이 들어가는지**를 보여준다.
    """
    out, seen = [], set()
    for icon, rel in _PHOTO_MARK.findall(body or ""):
        if rel in seen:
            continue
        seen.add(rel)
        slot, _, name = rel.rpartition("/")
        out.append({"icon": icon, "rel": rel, "slot": slot,
                    "name": name, "video": icon == "🎬"})
    return out


def _blog_job_view(job) -> dict | None:
    """블로그 작업 진행 상태를 화면용으로."""
    if not job:
        return None
    label = {
        "blog_recommend": "글감 추천", "blog_draft": "초안 작성",
        "blog_publish": "네이버 초안 넣기", "blog_rank": "순위 확인",
        "blog_media": "사진함 살펴보기", "blog_learn": "수정에서 배우기",
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


@app.route("/<path_key>/blog/media", methods=["POST"])
def blog_media_scan(path_key):
    """사진함에 새로 올린 사진을 집 PC가 살펴보게 한다."""
    return _ask_worker(path_key, "blog_media")


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
    return render_template("blog_post.html", key=path_key, post=post or {},
                           photos=_blog_photos((post or {}).get("body", "")),
                           error=error)


@app.route("/<path_key>/blog/post/<int:post_id>/save", methods=["POST"])
def blog_post_save(path_key, post_id):
    """본문 저장 + 학습: 사장님이 뭘 고쳤는지가 다음 글의 교본이 된다.

    고치기 전/후를 blog_learn 잡으로 집 PC에 보내면, AI 가 차이를 읽어
    '잘못된 정보(사실 교정)'와 '말투 교정'을 knowledge/블로그-배운점.md 에
    쌓는다. 그 파일은 다음 초안 프롬프트에 자동 포함된다(금고 자동 로드).
    """
    check(path_key)
    try:
        new_body = request.form.get("body", "")
        old_body = ""
        try:
            old_body = (blog.get_post(post_id) or {}).get("body") or ""
        except Exception:  # noqa: BLE001 — 학습은 덤, 저장이 우선
            pass
        blog.update_post(post_id, body=new_body)
        if old_body and old_body.strip() != new_body.strip():
            blog.request_blog_job("blog_learn", {
                "post_id": post_id, "before": old_body, "after": new_body,
            }, by="web")
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


@app.route("/<path_key>/menu/ingredient/merge", methods=["POST"])
def menu_ingredient_merge(path_key):
    """중복 등록된 자재 둘을 하나로 — 레시피를 옮기고 남는 쪽을 지운다."""
    check(path_key)
    body = request.get_json(force=True) or {}
    try:
        out = db.ingredient_merge(body.get("keep_id"), body.get("drop_id"),
                                  price_from=body.get("price_from") or "keep")
        return jsonify({"ok": True, **out})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"자재 합치기 실패: {e}", kind=type(e).__name__,
                     path=request.path, detail=traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/<path_key>/menu/ingredient/<int:ing_id>/delete", methods=["POST"])
def menu_ingredient_delete(path_key, ing_id):
    """자재 삭제. 쓰는 중이면 어떤 메뉴가 쓰는지 알려주고, force 면 같이 지운다."""
    check(path_key)
    force = bool((request.get_json(silent=True) or {}).get("force"))
    try:
        return jsonify({"ok": True, **db.ingredient_delete(ing_id, force=force)})
    except db.IngredientInUse as e:
        return jsonify({"ok": False, "in_use": True, "skus": e.skus,
                        "error": str(e)}), 409
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"자재 삭제 실패: {e}", kind=type(e).__name__,
                     path=request.path, detail=traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/<path_key>/menu/item/new", methods=["POST"])
def menu_item_new(path_key):
    """새 메뉴 추가 — 이름·분류만 있으면 SKU 는 분류에서 자동으로 만든다."""
    check(path_key)
    body = request.get_json(force=True) or {}
    # 소개글은 자동으로 채우지 않는다 — 이름만 보고 지어낸 문장에 사실과 다른
    # 내용이 섞여 그대로 채널에 나갈 뻔했다(사장님 지시 2026-08-26). 빈 칸으로
    # 만들고 상세 창에서 직접 적는다.
    try:
        out = db.menu_create(body)
        # 새 메뉴는 '출시 진행' 목록에 올린다 — 채널 등록 체크리스트가 끝나거나
        # 직원이 '출시 완료'를 누를 때까지 첫 화면 배너에 뜬다.
        try:
            cur = db.menu_settings_all().get("launching") or []
            if out["sku"] not in cur:
                db.menu_set_setting("launching", cur + [out["sku"]])
        except Exception:  # noqa: BLE001 — 배너는 편의 기능, 생성 자체를 막지 않는다
            pass
        out["intro_ko"] = body.get("intro_ko")
        out["intro_en"] = body.get("intro_en")
        out["name_en"] = body.get("name_en")
        return jsonify({"ok": True, **out})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"메뉴 추가 실패: {e}", kind=type(e).__name__,
                     path=request.path, detail=traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/<path_key>/menu/category/rename", methods=["POST"])
def menu_category_rename(path_key):
    """분류 이름 바꾸기 — 그 분류의 메뉴 전부 + 목표 원가율 키까지 함께."""
    check(path_key)
    b = request.get_json(force=True) or {}
    try:
        return jsonify({"ok": True, **db.category_rename(b.get("from"), b.get("to"))})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"분류 이름 변경 실패: {e}", kind=type(e).__name__,
                     path=request.path, detail=traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/<path_key>/menu/category/order", methods=["POST"])
def menu_category_order(path_key):
    """분류 순서 = 메뉴판 순서."""
    check(path_key)
    order = (request.get_json(force=True) or {}).get("order") or []
    if not isinstance(order, list):
        return jsonify({"ok": False, "error": "순서 목록이 아닙니다"}), 400
    # 빈 목록을 넘기면 남은 분류가 가나다순으로 재배치되어 메뉴판이 통째로
    # 뒤집힌다(실제로 당해 봤다). 목록이 비면 아무것도 하지 않는다.
    if not order:
        return jsonify({"ok": False, "error": "분류 순서가 비어 있습니다"}), 400
    try:
        return jsonify({"ok": True, **db.category_reorder(order)})
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"분류 순서 변경 실패: {e}", kind=type(e).__name__,
                     path=request.path, detail=traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/<path_key>/menu/items/order", methods=["POST"])
def menu_items_order(path_key):
    """메뉴 순서 = 채널 메뉴판 줄 순서."""
    check(path_key)
    skus = (request.get_json(force=True) or {}).get("skus") or []
    if not isinstance(skus, list) or not skus:
        return jsonify({"ok": False, "error": "순서 목록이 비어 있습니다"}), 400
    try:
        return jsonify({"ok": True, **db.items_reorder(skus)})
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"메뉴 순서 변경 실패: {e}", kind=type(e).__name__,
                     path=request.path, detail=traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/<path_key>/menu/item/<sku>/delete", methods=["POST"])
def menu_item_delete(path_key, sku):
    """메뉴 삭제 — 레시피·세트 구성·채널 예외까지 함께."""
    check(path_key)
    try:
        return jsonify({"ok": True, **db.menu_delete(sku)})
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"메뉴 삭제 실패: {e}", kind=type(e).__name__,
                     path=request.path, detail=traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/<path_key>/menu/ingredient/merge/undo", methods=["POST"])
def menu_ingredient_merge_undo(path_key):
    """직전 합치기 되돌리기 — 지운 자재를 되살리고 레시피를 제자리로."""
    check(path_key)
    try:
        return jsonify({"ok": True, **db.ingredient_merge_undo()})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"합치기 되돌리기 실패: {e}", kind=type(e).__name__,
                     path=request.path, detail=traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/<path_key>/menu/component", methods=["POST"])
def menu_component_upsert(path_key):
    """세트 구성 추가/수정 — 세트에 '어떤 메뉴가 몇 개' 들어가는지."""
    check(path_key)
    body = request.get_json(force=True) or {}
    try:
        out = db.component_upsert(body.get("sku"), body.get("component_sku"),
                                  body.get("qty"), body.get("choice_group"))
        return jsonify({"ok": True, "recomputed": out})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"세트 구성 저장 실패: {e}", kind=type(e).__name__,
                     path=request.path, detail=traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/<path_key>/menu/component/<int:row_id>/delete", methods=["POST"])
def menu_component_delete(path_key, row_id):
    check(path_key)
    try:
        return jsonify({"ok": True, "recomputed": db.component_delete(row_id)})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


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
            "offers": _safe(db.offers_all),
            "components": _safe(db.components_all),
            "merge_undo": _safe(db.merge_undo_info),
            # 메뉴 사진 공개 URL 베이스 — 화면이 {base}/{sku}/{채널}.jpg 로 조합
            "img_base": (os.getenv("SUPABASE_URL", "").rstrip("/")
                         + "/storage/v1/object/public/menu-images"),
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


@app.route("/<path_key>/menu/item/<sku>/image", methods=["POST"])
def menu_item_image(path_key, sku):
    """메뉴 사진 원본 업로드 → 채널별 규격(배민/쿠팡/네이버/키오스크) 자동 생성.

    menu_images 는 service/ 안의 모듈이라 지연 import — 위(모듈 로드 시점)에서
    import 하면 `import service.app` 으로 부르는 테스트가 경로 문제로 죽는다
    (menu_intro 시절에 실제로 겪은 사고).
    """
    check(path_key)
    if not any(m["sku"] == sku for m in db.menu_all()):
        abort(404)
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "파일이 없습니다"}), 400
    raw = f.read()
    try:
        from menu_images import MAX_UPLOAD, upload_all
    except ImportError:
        from service.menu_images import MAX_UPLOAD, upload_all
    if len(raw) > MAX_UPLOAD:
        return jsonify({"ok": False,
                        "error": f"파일이 너무 큽니다({len(raw)//1024//1024}MB) — 15MB 이하로"}), 400
    try:
        out = upload_all(db.get_client(), sku, raw)
        return jsonify({"ok": True, **out})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"메뉴 사진 업로드 실패({sku}): {e}",
                     kind=type(e).__name__, path=request.path,
                     detail=traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/<path_key>/menu/item/<sku>/image/delete", methods=["POST"])
def menu_item_image_delete(path_key, sku):
    check(path_key)
    try:
        from menu_images import delete_all
    except ImportError:
        from service.menu_images import delete_all
    try:
        n = delete_all(db.get_client(), sku)
        return jsonify({"ok": True, "removed": n})
    except Exception as e:  # noqa: BLE001
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
    if key not in ("channel_fees", "target_cost_rates", "order_model", "task_done",
                   "launching"):
        abort(400)
    try:
        db.menu_set_setting(key, request.get_json(force=True))
        return jsonify({"ok": True})
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"메뉴 설정 저장 실패({key}): {e}",
                     kind=type(e).__name__, path=request.path,
                     detail=traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


# ---------------------------------------------------------------------------
# 마케팅 캘린더 (/mkt) — 설계: 2026-08-26 사장님 확정 (목업 v3)
# 매출 원천: 드라이브 장부관리 폴더의 TOS/IMU 포스 엑셀(집 PC 일꾼이 자동 반영)
# ---------------------------------------------------------------------------

from database import mkt_store  # noqa: E402
from service import mkt_page  # noqa: E402


@app.route("/<path_key>/mkt")
def mkt_home(path_key):
    check(path_key)
    today = datetime.now().date()
    try:
        y = int(request.args.get("y", today.year))
        m = int(request.args.get("m", today.month))
        assert 1 <= m <= 12 and 2024 <= y <= 2100
    except (ValueError, AssertionError):
        y, m = today.year, today.month
    view = mkt_page.build_month_view(y, m, today)
    return render_template("mkt.html", key=path_key, v=view)


@app.route("/<path_key>/mkt/guide")
def mkt_guide(path_key):
    check(path_key)
    return render_template("mkt_guide.html", key=path_key)


@app.route("/<path_key>/mkt/campaign", methods=["POST"])
def mkt_campaign_new(path_key):
    check(path_key)
    f = request.get_json(force=True) or {}
    title = (f.get("title") or "").strip()
    start = (f.get("start") or "").strip()
    if not title or not start:
        return jsonify({"ok": False, "error": "제목과 시작일은 필수예요."}), 400
    category = f.get("category") or "store"
    end = (f.get("end") or "").strip() or None
    if category == "var" and not end:
        end = start                       # 변수는 당일 단발
    targets = [t.strip() for t in (f.get("targets") or []) if t.strip()]
    if not targets:
        try:
            targets = mkt_store.extract_targets(
                title, mkt_store.distinct_products(days=120))
        except Exception:  # noqa: BLE001
            targets = []
    cost = f.get("cost")
    try:
        cost = int(str(cost).replace(",", "")) if cost not in (None, "") else None
    except ValueError:
        cost = None
    try:
        cid = mkt_store.create_campaign(
            title, category, start, end, targets or None, cost,
            (f.get("memo") or "").strip() or None)
        return jsonify({"ok": True, "id": cid, "targets": targets})
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"캠페인 저장 실패: {e}", kind=type(e).__name__,
                     path=request.path, detail=traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/<path_key>/mkt/campaign/<int:cid>/update", methods=["POST"])
def mkt_campaign_update(path_key, cid):
    check(path_key)
    f = request.get_json(force=True) or {}
    try:
        if f.get("action") == "end":
            mkt_store.update_campaign(
                cid, end_date=f.get("end") or str(datetime.now().date()),
                status="done")
        elif f.get("action") == "delete":
            mkt_store.delete_campaign(cid)
        else:
            patch = {k: f[k] for k in
                     ("title", "category", "start_date", "end_date",
                      "target_products", "cost", "memo") if k in f}
            mkt_store.update_campaign(cid, **patch)
        return jsonify({"ok": True})
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"캠페인 수정 실패(#{cid}): {e}",
                     kind=type(e).__name__, path=request.path,
                     detail=traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/<path_key>/mkt/campaign/<int:cid>/effect")
def mkt_campaign_effect(path_key, cid):
    check(path_key)
    try:
        return jsonify(mkt_page.campaign_effect(cid))
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)[:200]}), 200


@app.route("/<path_key>/mkt/day/<day>")
def mkt_day(path_key, day):
    check(path_key)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        abort(400)
    try:
        return jsonify(mkt_page.day_detail(day))
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)[:200]}), 200


@app.route("/<path_key>/mkt/import", methods=["POST"])
def mkt_import(path_key):
    """'장부 지금 반영' — 집 PC 일꾼에게 폴더 스캔 요청."""
    check(path_key)
    try:
        mkt_store.request_pos_import(by="mkt")
        return jsonify({"ok": True})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


# ---------------------------------------------------------------------------
# 회의 기록 (/meeting) — 사장님 요청 2026-08-27
#
# 회의에서 나온 이야기·결정·할 일을 적어 두는 곳. AI 자동 정리는 넣지 않는다
# (사장님 결정) — 직원이 직접 적고, 할 일만 홈 화면에서 같이 챙긴다.
# ---------------------------------------------------------------------------

from database import meeting_store as mt  # noqa: E402

# schema_v8 미적용 등으로 표가 없을 때 화면에 띄울 안내
_MEETING_SETUP = ("회의 기록 표가 아직 준비되지 않았어요. "
                  "사장님께 알려주시면 바로 열어드릴게요.")

_WEEKDAY = "월화수목금토일"


def _looks_missing_table(e) -> bool:
    """표가 아직 없어서 난 오류인가(PGRST205 / 42P01)."""
    m = str(e)
    return ("42P01" in m or "PGRST205" in m
            or ("meetings" in m and "does not exist" in m))


def _meeting_form_tasks() -> list[dict]:
    """작성/수정 폼에서 온 할 일 줄들을 모은다(빈 줄은 버린다)."""
    f = request.form
    ids = f.getlist("task_id")
    contents = f.getlist("task_content")
    owners = f.getlist("task_owner")
    dues = f.getlist("task_due")
    # 완료 여부는 상세 화면에서 체크한다. 폼에는 숨은 값으로 실려 오가므로
    # (줄마다 하나씩) 수정하다가 완료 표시가 풀리지 않는다.
    dones = f.getlist("task_done")
    out = []
    for i, content in enumerate(contents):
        if not (content or "").strip():
            continue
        out.append({
            "id": ids[i] if i < len(ids) else None,
            "content": content,
            "owner": owners[i] if i < len(owners) else "",
            "due_date": dues[i] if i < len(dues) else "",
            "done": (dones[i] if i < len(dones) else "") in ("1", "true", "on"),
        })
    return out


def _meeting_category() -> str:
    """분류 — 목록에서 고르거나, '직접 입력'으로 새 이름을 넣는다."""
    cat = (request.form.get("category") or "").strip()
    if cat == "__new__":
        cat = (request.form.get("category_new") or "").strip()
    return cat[:20]


def _meeting_card(row, tasks) -> dict:
    """목록 카드에 보여줄 것 — 날짜 표기, 한 줄 요약, 남은 할 일 수."""
    d = str(row.get("meeting_date") or "")[:10]
    when = d
    try:
        dt = datetime.fromisoformat(d)
        when = f"{dt.month}월 {dt.day}일 ({_WEEKDAY[dt.weekday()]})"
    except ValueError:
        pass
    summary = " · ".join(
        line.strip(" -·•")
        for line in (row.get("decisions") or row.get("body") or "").splitlines()
        if line.strip())
    return {
        "when": when,
        "summary": summary[:120],
        "tasks": len(tasks),
        "open": sum(1 for t in tasks if not t.get("done")),
    }


def _meeting_when(m) -> str:
    d = str(m.get("meeting_date") or "")[:10]
    try:
        dt = datetime.fromisoformat(d)
        return f"{dt.year}년 {dt.month}월 {dt.day}일 ({_WEEKDAY[dt.weekday()]})"
    except ValueError:
        return d


def _lines(text):
    return [ln.strip(" -·•\t") for ln in (text or "").splitlines() if ln.strip()]


def _meeting_cats():
    try:
        return mt.categories()
    except Exception:  # noqa: BLE001
        return list(mt.DEFAULT_CATEGORIES)


@app.route("/<path_key>/meeting")
def meeting_home(path_key):
    """회의 목록 — 월별로 묶어 최신순. 검색·분류 탭."""
    check(path_key)
    q = (request.args.get("q") or "").strip()[:60]
    category = (request.args.get("cat") or "").strip()[:20]
    error, rows, total, tasks = None, [], 0, {}
    cats = list(mt.DEFAULT_CATEGORIES)
    try:
        listing = mt.list_meetings(q=q or None, category=category or None)
        rows, total = listing
        cats = _meeting_cats()
        tasks = mt.tasks_for([r["id"] for r in rows]) if rows else {}
    except Exception as e:  # noqa: BLE001
        error = (_MEETING_SETUP if _looks_missing_table(e)
                 else f"회의 목록을 불러오지 못했어요: {str(e)[:150]}")

    months = []
    for r in rows:
        d = str(r.get("meeting_date") or "")[:10]
        head = f"{d[:4]}년 {int(d[5:7])}월" if len(d) == 10 else "날짜 미상"
        r["view"] = _meeting_card(r, tasks.get(r["id"]) or [])
        if not months or months[-1]["head"] != head:
            months.append({"head": head, "items": []})
        months[-1]["items"].append(r)

    return render_template(
        "meeting.html", key=path_key, months=months, total=total,
        q=q, cat=category, cats=cats, error=error)


@app.route("/<path_key>/meeting/new")
def meeting_new(path_key):
    check(path_key)
    return render_template(
        "meeting_form.html", key=path_key, m=None, tasks=[],
        cats=_meeting_cats(), today=str(mt.today_kst()))


@app.route("/<path_key>/meeting/<int:mid>")
def meeting_detail(path_key, mid):
    check(path_key)
    m = mt.get_meeting(mid)
    if not m:
        abort(404)
    return render_template(
        "meeting_detail.html", key=path_key, m=m, tasks=mt.get_tasks(mid),
        when=_meeting_when(m), decisions=_lines(m.get("decisions")))


@app.route("/<path_key>/meeting/<int:mid>/edit")
def meeting_edit(path_key, mid):
    check(path_key)
    m = mt.get_meeting(mid)
    if not m:
        abort(404)
    return render_template(
        "meeting_form.html", key=path_key, m=m, tasks=mt.get_tasks(mid),
        cats=_meeting_cats(), today=str(mt.today_kst()))


@app.route("/<path_key>/meeting/save", methods=["POST"])
@app.route("/<path_key>/meeting/<int:mid>/save", methods=["POST"])
def meeting_save(path_key, mid=None):
    """작성·수정 저장 — 평범한 폼 전송(JS 가 막혀도 저장된다)."""
    check(path_key)
    fields = {
        "title": (request.form.get("title") or "").strip() or "제목 없는 회의",
        "meeting_date": (request.form.get("meeting_date") or "").strip(),
        "category": _meeting_category(),
        "attendees": (request.form.get("attendees") or "").strip(),
        "body": (request.form.get("body") or "").strip(),
        "decisions": (request.form.get("decisions") or "").strip(),
    }
    try:
        if mid:
            mt.update_meeting(mid, **fields)
        else:
            mid = mt.create_meeting(**fields)
        mt.save_tasks(mid, _meeting_form_tasks())
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"회의 저장 실패(#{mid}): {e}",
                     kind=type(e).__name__, path=request.path,
                     detail=traceback.format_exc())
        abort(500)
    return redirect(url_for("meeting_detail", path_key=path_key, mid=mid))


@app.route("/<path_key>/meeting/<int:mid>/delete", methods=["POST"])
def meeting_delete(path_key, mid):
    check(path_key)
    try:
        mt.delete_meeting(mid)
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"회의 삭제 실패(#{mid}): {e}",
                     kind=type(e).__name__, path=request.path,
                     detail=traceback.format_exc())
        abort(500)
    return redirect(url_for("meeting_home", path_key=path_key))


@app.route("/<path_key>/meeting/task/<int:tid>/done", methods=["POST"])
def meeting_task_done(path_key, tid):
    """할 일 체크 — 회의 상세와 홈 화면이 같이 쓴다."""
    check(path_key)
    data = request.get_json(force=True, silent=True) or {}
    done = bool(data.get("done", True))
    try:
        mt.set_task_done(tid, done)
        return jsonify({"ok": True, "done": done})
    except Exception as e:  # noqa: BLE001
        db.log_error("service", f"회의 할일 체크 실패(#{tid}): {e}",
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
