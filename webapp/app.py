"""베어글스 블로그 웹앱 (로컬).

지금은 1단계 — '글 창고' 화면. automation/library/ 를 읽어 웹으로 보여준다.
집 PC에서 run.bat 을 더블클릭하면 브라우저에서 http://localhost:5000 으로 열린다.

다음 단계에서 기획실·캘린더·분석을 같은 앱에 붙여나간다.
"""
from __future__ import annotations

import calendar as calmod
import pathlib
from datetime import date, datetime, timedelta

import yaml
from flask import Flask, abort, redirect, render_template, request, url_for

ROOT = pathlib.Path(__file__).resolve().parent.parent
LIB = ROOT / "automation" / "library"

STATUS_META = {
    "ready":     {"label": "발행 대기", "cls": "warning"},
    "scheduled": {"label": "예약됨",   "cls": "accent"},
    "published": {"label": "발행 완료", "cls": "success"},
}

app = Flask(__name__)


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


@app.route("/")
def dashboard():
    items = load_items()
    counts = {
        "ready": sum(1 for i in items if i.get("status") == "ready"),
        "scheduled": sum(1 for i in items if i.get("status") == "scheduled"),
        "published": sum(1 for i in items if i.get("status") == "published"),
    }
    return render_template("dashboard.html", items=items, counts=counts)


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
    return render_template("post.html", meta=meta, body=body, item_id=item_id)


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


if __name__ == "__main__":
    print("=" * 48)
    print(" 베어글스 블로그 웹앱 실행 중")
    print(" 브라우저에서 열기 →  http://localhost:5050")
    print(" (끄려면 이 창에서 Ctrl+C)")
    print("=" * 48)
    app.run(host="0.0.0.0", port=5050, debug=False)
