"""근무표 화면 (service/app.py 의 /schedule 라우트들이 쓴다).

데이터 로직은 집 PC 웹앱과 완전히 같아서 webapp/schedule.py 의 함수를 그대로
가져다 쓴다 — 저장 형식이 한 곳에만 정의돼 있어야 두 화면이 어긋나지 않는다.
저장 위치는 서버 디스크의 <리포>/schedule/ (.gitignore 라 git pull 에 안 쓸린다.
PythonAnywhere 디스크는 계속 유지된다).

두 개의 얼굴:
  · /<비밀주소>/schedule   관리자 화면 (사이드바 포함)
  · /s/<토큰>              직원용 열람 — 로그인 없음, 스케줄만 보인다.
                          비밀주소와 다른 토큰이라, 직원이 이 링크로
                          회의기록·MKT캘린더에 들어갈 수 없다.
"""
from __future__ import annotations

import secrets
from datetime import date

from flask import abort, jsonify, render_template, request, url_for

from webapp.schedule import (  # noqa: F401 — 데이터 계층 재사용
    build_boot, build_export, load_config, parse_iso, save_config, save_week,
    _clean_shift,
)


def _boot_for(key: str, **kw) -> dict:
    boot = build_boot(date.today(), **kw)
    boot["api"] = f"/{key}/schedule"   # JS 가 저장할 때 쓸 주소 앞부분
    return boot


# ---------------------------------------------------------------------------
# 화면
# ---------------------------------------------------------------------------
def admin_page(key: str):
    cfg = load_config()
    return render_template(
        "schedule.html",
        key=key,
        boot=_boot_for(key),
        public_url=url_for("schedule_staff", token=cfg["publicToken"], _external=True),
    )


def staff_page(token: str):
    cfg = load_config()
    if not secrets.compare_digest(token, cfg.get("publicToken") or ""):
        abort(404)
    return render_template("schedule_public.html",
                           boot=build_boot(date.today(), back=1, fwd=2))


# ---------------------------------------------------------------------------
# 저장 (관리자 비밀주소 안에서만 불린다)
# ---------------------------------------------------------------------------
def save_week_api():
    body = request.get_json(silent=True) or {}
    start = parse_iso(body.get("week_start"))
    days_raw = body.get("days")
    if not isinstance(days_raw, list) or len(days_raw) != 7:
        return jsonify({"ok": False, "error": "days 형식이 올바르지 않아요."}), 400
    days = []
    for day in days_raw:
        items = [_clean_shift(x) for x in day] if isinstance(day, list) else []
        days.append([x for x in items if x])
    save_week({
        "week_start": start.isoformat(),
        "locked": bool(body.get("locked")),
        "days": days,
    })
    return jsonify({"ok": True})


def save_config_api():
    body = request.get_json(silent=True) or {}
    cfg = load_config()
    for k in ("bizHours", "closedDows", "closedDates", "presets", "staff",
              "salesPerHead", "showHoliday", "showWeather"):
        if k in body:
            cfg[k] = body[k]
    save_config(cfg)
    return jsonify({"ok": True})


def new_token_api():
    """직원용 링크 새로 만들기 (퇴사자가 생겼을 때 등)."""
    cfg = load_config()
    cfg["publicToken"] = secrets.token_urlsafe(12)
    save_config(cfg)
    return jsonify({"ok": True, "url": url_for("schedule_staff",
                                               token=cfg["publicToken"], _external=True)})


def export():
    start = parse_iso(request.args.get("from"), date.today().replace(day=1))
    end = parse_iso(request.args.get("to"), date.today())
    fmt = (request.args.get("fmt") or "md").lower()
    return build_export(start, end, fmt)
