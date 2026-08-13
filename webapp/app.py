"""베어글스 블로그 웹앱 (로컬).

지금은 1단계 — '글 창고' 화면. automation/library/ 를 읽어 웹으로 보여준다.
집 PC에서 run.bat 을 더블클릭하면 브라우저에서 http://localhost:5000 으로 열린다.

다음 단계에서 기획실·캘린더·분석을 같은 앱에 붙여나간다.
"""
from __future__ import annotations

import calendar as calmod
import hmac
import json
import pathlib
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta

import os
import secrets

import yaml
from dotenv import load_dotenv
from flask import (
    Flask, abort, redirect, render_template, request, session, url_for,
)

# Windows 콘솔(cp949)에서도 한글·특수문자(— → 등) print 가 깨지지 않도록 UTF-8 고정
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = pathlib.Path(__file__).resolve().parent.parent
LIB = ROOT / "automation" / "library"

load_dotenv(ROOT / ".env")

STATUS_META = {
    "ready":     {"label": "발행 대기", "cls": "warning"},
    "scheduled": {"label": "예약됨",   "cls": "accent"},
    "published": {"label": "발행 완료", "cls": "success"},
}

app = Flask(__name__)

# ---------------------------------------------------------------------------
# 로그인 (외부 인터넷 노출 대비)
# ---------------------------------------------------------------------------
# .env 의 BEARGELS_WEB_PASSWORD 가 있으면 모든 화면이 로그인 뒤로 잠긴다.
# 없으면 잠금 없음 = 집 안에서만 쓰는 모드. 터널(start_public.bat)은 비밀번호가
# 없으면 아예 실행을 거부한다 — 인터넷에 무방비로 열리는 걸 막기 위해.
WEB_PASSWORD = (os.getenv("BEARGELS_WEB_PASSWORD") or "").strip()

# 세션 서명키 — 파일에 보관해 앱을 재시작해도 로그인이 유지된다.
_SECRET_FILE = pathlib.Path(__file__).resolve().parent / ".flask_secret"
if not _SECRET_FILE.exists():
    _SECRET_FILE.write_text(secrets.token_hex(32), encoding="utf-8")
app.secret_key = _SECRET_FILE.read_text(encoding="utf-8").strip()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

OPEN_PATHS = ("/login", "/static")

# 인스타 릴스 파이프라인 (주제→촬영→자동편집→완성본). 로그인 잠금은
# 아래 before_request 가 블루프린트 경로에도 그대로 적용된다.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from instagram import bp as instagram_bp  # noqa: E402

app.register_blueprint(instagram_bp)


@app.before_request
def require_login():
    if not WEB_PASSWORD or session.get("ok"):
        return None
    if request.path.startswith(OPEN_PATHS):
        return None
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not WEB_PASSWORD:
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        given = request.form.get("password", "")
        if hmac.compare_digest(given, WEB_PASSWORD):
            session["ok"] = True
            session.permanent = True
            return redirect(url_for("dashboard"))
        time.sleep(1.0)  # 무작위 대입 속도 늦추기
        error = "비밀번호가 달라요."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def load_items() -> list[dict]:
    items = []
    if not LIB.exists():
        return items
    for meta_path in sorted(LIB.glob("*/meta.yaml")):
        try:
            m = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        m["_status_meta"] = STATUS_META.get(m.get("status", "ready"), STATUS_META["ready"])
        items.append(m)
    items.sort(key=lambda m: m.get("id", 0), reverse=True)
    return items


def _upcoming_schedule(items: list[dict], limit: int = 3) -> list[dict]:
    today = date.today()
    up = []
    for it in items:
        if it.get("status") == "scheduled" and it.get("scheduled_time"):
            try:
                dt = datetime.strptime(str(it["scheduled_time"]).split(" ")[0], "%Y-%m-%d").date()
            except Exception:
                continue
            if dt >= today:
                up.append({"id": it.get("id"), "title": it.get("title"),
                           "when": it.get("scheduled_time"), "dt": dt})
    up.sort(key=lambda x: x["dt"])
    return up[:limit]


def _rank_summary() -> dict | None:
    if not RANK_HISTORY.exists():
        return None
    try:
        checks = json.loads(RANK_HISTORY.read_text(encoding="utf-8")).get("checks", {})
    except Exception:
        return None
    dates = sorted(checks)
    if not dates:
        return None
    latest = checks[dates[-1]]
    found = [r for r in latest if r.get("found")]
    best = min((r["rank"] for r in found), default=None)
    return {
        "date": dates[-1], "total": len(latest), "found": len(found),
        "page1": sum(1 for r in found if r.get("page") == 1), "best": best,
    }


@app.route("/")
def dashboard():
    items = load_items()
    counts = {
        "ready": sum(1 for i in items if i.get("status") == "ready"),
        "scheduled": sum(1 for i in items if i.get("status") == "scheduled"),
        "published": sum(1 for i in items if i.get("status") == "published"),
    }
    return render_template(
        "dashboard.html", items=items, counts=counts,
        upcoming=_upcoming_schedule(items), rank=_rank_summary(),
    )


@app.route("/post/<int:item_id>")
def view_post(item_id: int):
    d = LIB / f"{item_id:04d}"
    if not (d / "post.md").exists():
        abort(404)
    meta = {}
    if (d / "meta.yaml").exists():
        meta = yaml.safe_load((d / "meta.yaml").read_text(encoding="utf-8")) or {}
    meta["_status_meta"] = STATUS_META.get(meta.get("status", "ready"), STATUS_META["ready"])
    body = (d / "post.md").read_text(encoding="utf-8")
    return render_template("post.html", meta=meta, body=body, item_id=item_id,
                           prepared=request.args.get("prepared"))


def _item_date(it: dict):
    """캘린더에 찍을 날짜. 예약글=예약일, 발행글=예약일 또는 작성일. 대기글은 None(안 찍음)."""
    status = it.get("status")
    if status == "ready":
        return None
    s = it.get("scheduled_time") or (it.get("created", "") if status == "published" else "")
    try:
        return datetime.strptime(str(s).split(" ")[0], "%Y-%m-%d").date()
    except Exception:
        return None


RANK_CHECKER = pathlib.Path(__file__).resolve().parent / "rank_checker.py"
RANK_HISTORY = pathlib.Path(__file__).resolve().parent / "rank_history.json"
NAVER_AUTODRAFT = ROOT / "automation" / "src" / "naver_autodraft.py"


def tracked_keywords() -> list[str]:
    kws = []
    for it in load_items():
        k = (it.get("main_keyword") or "").strip()
        if k and k not in kws:
            kws.append(k)
    for d in ("송도 베이글", "송도 카페"):
        if d not in kws:
            kws.append(d)
    return kws


def _friendly_error(e: Exception) -> str:
    msg = str(e).lower()
    if "쓸 수 있는 ai" in str(e) or "no provider" in msg:
        return ("쓸 수 있는 AI 가 없어요. .env 에 GEMINI_API_KEY(무료) 또는 "
                "ANTHROPIC_API_KEY 를 넣어주세요. 무료 키: aistudio.google.com/apikey")
    if "credit" in msg or "billing" in msg or "insufficient" in msg:
        return ("Claude 크레딧이 부족해요. .env 에 GEMINI_API_KEY(무료)를 넣으면 "
                "바로 무료로 계속 쓸 수 있어요. 무료 키: aistudio.google.com/apikey")
    if "api_key" in msg or "authentication" in msg or "x-api-key" in msg:
        return "API 키가 잘못됐어요. .env 의 ANTHROPIC_API_KEY / GEMINI_API_KEY 를 확인해주세요."
    if "quota" in msg or "429" in msg:
        return "무료 사용 한도를 잠깐 넘었어요. 몇 분 뒤 다시 눌러주세요."
    return f"AI 작업 중 문제가 생겼어요: {e}"


RECOMMENDATIONS = pathlib.Path(__file__).resolve().parent / "recommendations.json"


def load_recommendations() -> dict:
    if not RECOMMENDATIONS.exists():
        return {}
    try:
        data = json.loads(RECOMMENDATIONS.read_text(encoding="utf-8"))
    except Exception:
        return {}
    items = data.get("items", [])
    items.sort(key=lambda x: x.get("priority", 999))
    data["items"] = items
    return data


@app.route("/plan", methods=["GET", "POST"])
def plan_room():
    plan = None
    error = None
    hint = ""
    if request.method == "POST":
        hint = request.form.get("hint", "").strip()
        try:
            import planner
            plan = planner.make_plan(hint)
        except Exception as e:  # noqa: BLE001 — 사용자에게 친절 메시지로 변환
            error = _friendly_error(e)
    return render_template("plan.html", plan=plan, error=error, hint=hint,
                           recs=load_recommendations())


@app.route("/plan/recommend", methods=["POST"])
def plan_recommend():
    error = None
    try:
        import planner
        items = planner.make_recommendations()
        RECOMMENDATIONS.write_text(
            json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
        return redirect(url_for("plan_room"))
    except Exception as e:  # noqa: BLE001
        error = _friendly_error(e)
    return render_template("plan.html", plan=None, error=error, hint="",
                           recs=load_recommendations())


@app.route("/plan/draft", methods=["POST"])
def plan_draft():
    topic = request.form.get("topic", "").strip()
    post_type = request.form.get("post_type", "정보성").strip() or "정보성"
    title = request.form.get("title", "").strip()
    main_keyword = request.form.get("main_keyword", "").strip()
    subs = [s.strip() for s in request.form.get("sub_keywords", "").split(",") if s.strip()]
    try:
        import planner
        item_id = planner.make_draft(topic, post_type, title, main_keyword, subs)
        return redirect(url_for("view_post", item_id=item_id))
    except Exception as e:  # noqa: BLE001
        return render_template("plan.html", plan=None, error=_friendly_error(e), hint=topic)


@app.route("/replies", methods=["GET", "POST"])
def replies_room():
    """배민·쿠팡 리뷰 답글 초안 만들기. 초안만 — 게시는 사장님이 직접."""
    import replies as replies_mod

    result = None
    error = None
    form = {"platform": "baemin", "rating": "5"}
    if request.method == "POST":
        form = request.form.to_dict()
        review = replies_mod.build_review(request.form)
        try:
            result = replies_mod.generate(review)
        except Exception as e:  # noqa: BLE001 — 친절 메시지로 변환
            error = _friendly_error(e)
    return render_template(
        "replies.html", result=result, error=error, form=form,
        platforms=replies_mod.PLATFORMS, kind_label=replies_mod.KIND_LABEL,
    )


@app.route("/analytics")
def analytics():
    hist = {}
    if RANK_HISTORY.exists():
        try:
            hist = json.loads(RANK_HISTORY.read_text(encoding="utf-8"))
        except Exception:
            hist = {}
    checks = hist.get("checks", {})
    dates = sorted(checks.keys())
    latest = checks.get(dates[-1], []) if dates else []
    prev = checks.get(dates[-2], []) if len(dates) >= 2 else []
    prev_rank = {r["keyword"]: r.get("rank") for r in prev}

    rows = []
    for r in latest:
        kw = r["keyword"]
        cur = r.get("rank")
        old = prev_rank.get(kw)
        trend = None
        if cur and old:
            trend = old - cur  # +면 상승(순위 숫자 작아짐)
        elif cur and old is None and len(dates) >= 2:
            trend = "new"
        rows.append({
            "keyword": kw, "found": r.get("found"),
            "page": r.get("page"), "pos": r.get("pos_in_page"),
            "rank": cur, "scanned": r.get("scanned"), "trend": trend,
        })
    return render_template(
        "analytics.html", rows=rows, last_date=(dates[-1] if dates else None),
        checking=request.args.get("checking"), tracked=tracked_keywords(),
        items=load_items(),
    )


@app.route("/analytics/check", methods=["POST"])
def analytics_check():
    kws = tracked_keywords()
    try:
        subprocess.Popen([sys.executable, str(RANK_CHECKER), *kws], cwd=str(RANK_CHECKER.parent))
    except Exception:
        pass
    return redirect(url_for("analytics", checking=1))


@app.route("/calendar")
def calendar_view():
    today = date.today()
    ym = request.args.get("ym", "")
    try:
        year, month = (int(x) for x in ym.split("-"))
    except Exception:
        year, month = today.year, today.month

    weeks = calmod.Calendar(firstweekday=6).monthdayscalendar(year, month)
    byday: dict[int, list[dict]] = {}
    for it in load_items():
        dt = _item_date(it)
        if dt and dt.year == year and dt.month == month:
            byday.setdefault(dt.day, []).append(it)

    first = date(year, month, 1)
    prev = first - timedelta(days=1)
    nxt = date(year, month, 28) + timedelta(days=10)
    return render_template(
        "calendar.html", weeks=weeks, byday=byday, year=year, month=month, today=today,
        prev_ym=f"{prev.year}-{prev.month:02d}", next_ym=f"{nxt.year}-{nxt.month:02d}",
    )


@app.route("/post/<int:item_id>/prepare", methods=["POST"])
def prepare_naver(item_id: int):
    """집 PC 일꾼이 이 글을 네이버에 임시저장(초안)으로 넣는다. 최종 발행은 사장님이 직접."""
    post_json = LIB / f"{item_id:04d}" / "post.json"
    if not post_json.exists():
        abort(404)
    try:
        subprocess.Popen(
            [sys.executable, str(NAVER_AUTODRAFT), "--post", str(post_json)],
            cwd=str(NAVER_AUTODRAFT.parent),
        )
    except Exception:
        pass
    return redirect(url_for("view_post", item_id=item_id, prepared=1))


@app.route("/post/<int:item_id>/schedule", methods=["POST"])
def schedule_post(item_id: int):
    d = LIB / f"{item_id:04d}"
    mp = d / "meta.yaml"
    if not mp.exists():
        abort(404)
    meta = yaml.safe_load(mp.read_text(encoding="utf-8")) or {}
    if request.form.get("action") == "unschedule":
        meta["status"] = "ready"
        meta["scheduled_time"] = None
    else:
        dt = request.form.get("date")
        tm = request.form.get("time") or "08:00"
        if dt:
            meta["status"] = "scheduled"
            meta["scheduled_time"] = f"{dt} {tm}"
    mp.write_text(yaml.safe_dump(meta, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return redirect(url_for("view_post", item_id=item_id))


@app.route("/post/<int:item_id>/delete", methods=["POST"])
def delete_post(item_id: int):
    """글을 휴지통(library/_trash)으로 이동. 완전 삭제 아님 — 되살릴 수 있음."""
    d = LIB / f"{item_id:04d}"
    if not d.exists():
        abort(404)
    trash = LIB / "_trash"
    trash.mkdir(exist_ok=True)
    dest = trash / f"{item_id:04d}"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.move(str(d), str(dest))
    return redirect(url_for("dashboard"))


@app.route("/post/<int:item_id>/status", methods=["POST"])
def set_post_status(item_id: int):
    d = LIB / f"{item_id:04d}"
    mp = d / "meta.yaml"
    if not mp.exists():
        abort(404)
    meta = yaml.safe_load(mp.read_text(encoding="utf-8")) or {}
    new = request.form.get("status", "ready")
    if new in ("ready", "scheduled", "published"):
        meta["status"] = new
        if new != "scheduled":
            meta["scheduled_time"] = None
    mp.write_text(yaml.safe_dump(meta, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return redirect(url_for("view_post", item_id=item_id))


@app.route("/post/<int:item_id>/evaluate")
def evaluate_post(item_id: int):
    d = LIB / f"{item_id:04d}"
    if not (d / "post.md").exists():
        abort(404)
    meta = {}
    if (d / "meta.yaml").exists():
        meta = yaml.safe_load((d / "meta.yaml").read_text(encoding="utf-8")) or {}
    body = (d / "post.md").read_text(encoding="utf-8")
    title = meta.get("title", "")
    main_keyword = meta.get("main_keyword", "")

    import evaluator
    checks = evaluator.mechanical_check(body, title, main_keyword)
    ok = sum(1 for c in checks if c["status"] == "ok")
    review = None
    review_error = None
    if request.args.get("ai"):
        try:
            review = evaluator.expert_review(body, title, main_keyword)
        except Exception as e:  # noqa: BLE001
            review_error = _friendly_error(e)
    return render_template(
        "evaluate.html", item_id=item_id, meta=meta, checks=checks,
        ok=ok, total=len(checks), review=review, review_error=review_error,
    )


@app.route("/post/<int:item_id>/edit", methods=["GET", "POST"])
def edit_post(item_id: int):
    d = LIB / f"{item_id:04d}"
    md_path = d / "post.md"
    if not md_path.exists():
        abort(404)
    if request.method == "POST":
        new_body = request.form.get("body", "")
        # 편집 전 원본을 .bak 으로 한 번 백업(실수 되돌리기용)
        try:
            (d / "post.md.bak").write_text(md_path.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            pass
        md_path.write_text(new_body.replace("\r\n", "\n"), encoding="utf-8")
        return redirect(url_for("view_post", item_id=item_id))
    meta = {}
    if (d / "meta.yaml").exists():
        meta = yaml.safe_load((d / "meta.yaml").read_text(encoding="utf-8")) or {}
    body = md_path.read_text(encoding="utf-8")
    return render_template("edit.html", meta=meta, body=body, item_id=item_id)


@app.route("/place")
def place_guide():
    """네이버 스마트플레이스 실행 가이드 — 저장소 루트의 정적 HTML 을 그대로 서빙.

    직원용 서비스 앱(service/app.py)의 `/<key>/place` 와 같은 파일을 본다.
    내용을 고칠 때는 루트의 `스마트플레이스-직원가이드.html` 한 곳만 고치면
    양쪽에 같이 반영된다.
    """
    page = ROOT / "스마트플레이스-직원가이드.html"
    try:
        body = page.read_text(encoding="utf-8")
    except OSError:
        abort(404)
    # 파일이 <title> 부터 시작하는 조각이라 문서 껍데기를 씌워 준다.
    return (
        '<!doctype html>\n<html lang="ko">\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"{body}\n</html>"
    )


if __name__ == "__main__":
    PORT = int(os.getenv("PORT", "5050"))
    print("=" * 52)
    print(" 베어글스 AI 비서 웹앱 실행 중")
    print(f" 브라우저에서 열기 →  http://localhost:{PORT}")
    if WEB_PASSWORD:
        print(" [잠김] 비밀번호 잠금 켜짐 (외부 접속 가능)")
    else:
        print(" [주의] 비밀번호 없음 — 집 안(같은 와이파이)에서만 쓰세요.")
        print("    외부에서 쓰려면 .env 에 BEARGELS_WEB_PASSWORD 를 넣으세요.")
    print(" (끄려면 이 창에서 Ctrl+C)")
    print("=" * 52)
    app.run(host="0.0.0.0", port=PORT, debug=False)
