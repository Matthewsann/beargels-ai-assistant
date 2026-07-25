"""베어글스 블로그 웹앱 (로컬).

지금은 1단계 — '글 창고' 화면. automation/library/ 를 읽어 웹으로 보여준다.
집 PC에서 run.bat 을 더블클릭하면 브라우저에서 http://localhost:5000 으로 열린다.

다음 단계에서 기획실·캘린더·분석을 같은 앱에 붙여나간다.
"""
from __future__ import annotations

import pathlib

import yaml
from flask import Flask, abort, render_template

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


if __name__ == "__main__":
    print("=" * 48)
    print(" 베어글스 블로그 웹앱 실행 중")
    print(" 브라우저에서 열기 →  http://localhost:5050")
    print(" (끄려면 이 창에서 Ctrl+C)")
    print("=" * 48)
    app.run(host="0.0.0.0", port=5050, debug=False)
