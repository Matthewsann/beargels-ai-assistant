"""근무표 (웹앱에 붙는 블루프린트).

주간 스케줄 작성 → 확정(잠금) → 실제 근무 기록 → 데이터 내보내기.

두 개의 얼굴이 있다:
  · /schedule      관리자 전용. 기존 로그인 뒤에 잠긴다(app.py 의 before_request).
  · /s/<토큰>       직원용 열람 링크. 로그인 없이 스케줄만 보인다.
                   사이드바를 아예 렌더하지 않아 다른 프로그램으로 넘어갈 길이 없다.

데이터는 리포 루트 schedule/ 밑에 JSON 으로 둔다. 직원 실명이 들어가므로
.gitignore 로 빼서 GitHub 에는 올라가지 않는다 (기기마다 로컬 보관).
"""
from __future__ import annotations

import csv
import io
import json
import pathlib
import re
import secrets
from datetime import date, datetime, timedelta
from urllib.parse import quote

from flask import (
    Blueprint, Response, abort, jsonify, redirect, render_template, request,
    session, url_for,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "schedule"
WEEKS_DIR = DATA / "weeks"

bp = Blueprint("schedule", __name__)

DOW = ["월", "화", "수", "목", "금", "토", "일"]

# 앞뒤로 몇 주씩 열어둘지 (지난 기록 보기 + 미리 짜두기)
WEEKS_BACK, WEEKS_FWD = 4, 4


# ---------------------------------------------------------------------------
# 날짜 유틸
# ---------------------------------------------------------------------------
def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def parse_iso(s: str | None, fallback: date | None = None) -> date:
    try:
        return datetime.strptime((s or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return fallback or date.today()


def md(d: date) -> str:
    return f"{d.month}/{d.day}"


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
def default_config() -> dict:
    return {
        # 영업시간은 "언제부터 이랬다"를 쌓는다. 바꿔도 과거 근무표는 그대로다.
        "bizHours": [
            {"from": "2020-01-01", "dows": [[7, 21]] * 7},
        ],
        "closedDows": [],
        "closedDates": [],
        "presets": [
            {"name": "오픈", "s": 6.5, "e": 12},
            {"name": "미들", "s": 10, "e": 15},
            {"name": "오후", "s": 14, "e": 19},
            {"name": "마감", "s": 16, "e": 21.5},
        ],
        "staff": [],
        "salesPerHead": 35,
        "showHoliday": True,
        "showWeather": True,
        "publicToken": secrets.token_urlsafe(12),
    }


def load_config() -> dict:
    cfg = default_config()
    path = DATA / "config.json"
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                cfg.update(saved)
        except (json.JSONDecodeError, OSError):
            pass  # 손상된 설정은 무시하고 기본값으로 뜬다
    # 토큰이 없던 예전 파일이면 새로 만들어 붙인다
    if not cfg.get("publicToken"):
        cfg["publicToken"] = secrets.token_urlsafe(12)
        save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def biz_of(cfg: dict, d: date) -> list[float]:
    """그 날짜에 적용되던 영업시간 [시작, 종료]."""
    entry = None
    iso = d.isoformat()
    for e in sorted(cfg.get("bizHours") or [], key=lambda x: x.get("from", "")):
        if (e.get("from") or "") <= iso:
            entry = e
    if not entry:
        entry = (cfg.get("bizHours") or [{}])[0]
    dows = entry.get("dows") or [[7, 21]] * 7
    pair = dows[d.weekday()] if d.weekday() < len(dows) else [7, 21]
    return [float(pair[0]), float(pair[1])]


# ---------------------------------------------------------------------------
# 주 단위 근무 데이터
# ---------------------------------------------------------------------------
_WEEK_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")


def empty_week(start: date) -> dict:
    return {"week_start": start.isoformat(), "locked": False, "days": [[] for _ in range(7)]}


def load_week(start: date) -> dict:
    path = WEEKS_DIR / f"{start.isoformat()}.json"
    if not path.exists():
        return empty_week(start)
    try:
        w = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return empty_week(start)
    days = w.get("days")
    if not isinstance(days, list) or len(days) != 7:
        days = [[] for _ in range(7)]
    return {
        "week_start": start.isoformat(),
        "locked": bool(w.get("locked")),
        "days": [d if isinstance(d, list) else [] for d in days],
    }


def save_week(week: dict) -> None:
    start = parse_iso(week.get("week_start"))
    WEEKS_DIR.mkdir(parents=True, exist_ok=True)
    (WEEKS_DIR / f"{start.isoformat()}.json").write_text(
        json.dumps(week, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def all_week_starts() -> list[date]:
    if not WEEKS_DIR.exists():
        return []
    out = []
    for p in WEEKS_DIR.iterdir():
        if _WEEK_FILE.match(p.name):
            out.append(parse_iso(p.stem))
    return sorted(out)


# ---------------------------------------------------------------------------
# 공휴일 · 날씨
# ---------------------------------------------------------------------------
# 공휴일은 한국천문연구원 특일 정보 API(공공데이터포털, 무료)에서 1년에 한 번 받아
# schedule/holidays.json 에 저장해 두고 쓴다. 아직 안 받아왔으면 아래 표로 버틴다.
BUILTIN_HOLIDAYS = {
    "2026-01-01": "신정",
    "2026-02-16": "설날 연휴", "2026-02-17": "설날", "2026-02-18": "설날 연휴",
    "2026-03-01": "삼일절", "2026-03-02": "대체공휴일",
    "2026-05-05": "어린이날",
    "2026-06-06": "현충일",
    "2026-08-15": "광복절", "2026-08-17": "대체공휴일",
    "2026-09-24": "추석 연휴", "2026-09-25": "추석", "2026-09-26": "추석 연휴",
    "2026-10-03": "개천절", "2026-10-05": "대체공휴일",
    "2026-10-09": "한글날",
    "2026-12-25": "성탄절",
}


def _load_json(path: pathlib.Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def load_holidays() -> dict:
    holidays = dict(BUILTIN_HOLIDAYS)
    holidays.update(_load_json(DATA / "holidays.json"))
    return holidays


def load_sales() -> dict:
    """시간대별 예상 매출 (최근 4주 같은 요일 중앙값).

    POS 매출 내역에서 하루 한 번 계산해 schedule/sales.json 에 저장해 둔 것.
    없으면 빈 값 — 그래프 자리에 "아직 매출 데이터가 없어요"만 뜬다.
    형식: {"base": 6, "step": 0.5, "unit": "천원",
           "dows": {"0": [30분 단위 값들], ... "6": [...]}, "updated": "YYYY-MM-DD"}
    """
    return _load_json(DATA / "sales.json")


def load_weather() -> dict:
    """기상청 단기예보를 하루 한 번 받아 저장해 둔 것.

    없으면 빈 값 — 날씨 칸을 아예 안 그린다. 없는 날씨를 지어내지 않는다.
    """
    return _load_json(DATA / "weather.json")


# ---------------------------------------------------------------------------
# 화면에 넘길 데이터 한 덩이
# ---------------------------------------------------------------------------
def build_boot(anchor: date, *, back: int = WEEKS_BACK, fwd: int = WEEKS_FWD) -> dict:
    cfg = load_config()
    today = date.today()
    first = monday_of(anchor) - timedelta(weeks=back)

    weeks = []
    for i in range(back + fwd + 1):
        start = first + timedelta(weeks=i)
        w = load_week(start)
        iso = [(start + timedelta(days=n)).isoformat() for n in range(7)]
        weeks.append({
            "start": start.isoformat(),
            "iso": iso,
            "dates": [md(start + timedelta(days=n)) for n in range(7)],
            "label": f"{md(start)} (월) – {md(start + timedelta(days=6))} (일)",
            "locked": w["locked"],
            "days": w["days"],
            "biz": [biz_of(cfg, start + timedelta(days=n)) for n in range(7)],
        })

    return {
        "cfg": cfg,
        "weeks": weeks,
        "todayIso": today.isoformat(),
        "anchorWeek": monday_of(anchor).isoformat(),
        "holidays": load_holidays(),
        "weather": load_weather(),
        "sales": load_sales(),
        "dow": DOW,
    }


# ---------------------------------------------------------------------------
# 화면
# ---------------------------------------------------------------------------
@bp.route("/schedule")
def schedule_home():
    anchor = parse_iso(request.args.get("week"), date.today())
    cfg = load_config()
    return render_template(
        "schedule.html",
        boot=build_boot(anchor),
        public_url=url_for("schedule.staff_view", token=cfg["publicToken"], _external=True),
    )


@bp.route("/s/<token>")
def staff_view(token: str):
    """직원용 열람 링크. 로그인 없이 열리고, 스케줄만 보인다."""
    cfg = load_config()
    if not secrets.compare_digest(token, cfg.get("publicToken") or ""):
        abort(404)
    return render_template("schedule_public.html", boot=build_boot(date.today(), back=1, fwd=2))


# ---------------------------------------------------------------------------
# 저장 (관리자만 — before_request 로그인 잠금이 걸린다)
# ---------------------------------------------------------------------------
def _clean_shift(raw: dict) -> dict | None:
    try:
        who = str(raw.get("w") or "").strip()
        s, e = float(raw.get("s")), float(raw.get("e"))
    except (TypeError, ValueError):
        return None
    if not who or not (0 <= s < e <= 30):
        return None
    out = {"w": who, "s": s, "e": e}
    for key in ("st", "note", "actual", "pos"):
        val = raw.get(key)
        if isinstance(val, str) and val.strip():
            out[key] = val.strip()[:40]
    if isinstance(raw.get("br"), (int, float)):
        out["br"] = max(0, min(240, int(raw["br"])))
    return out


@bp.route("/schedule/api/week", methods=["POST"])
def api_save_week():
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


@bp.route("/schedule/api/config", methods=["POST"])
def api_save_config():
    body = request.get_json(silent=True) or {}
    cfg = load_config()
    for key in ("bizHours", "closedDows", "closedDates", "presets", "staff",
                "salesPerHead", "showHoliday", "showWeather"):
        if key in body:
            cfg[key] = body[key]
    save_config(cfg)
    return jsonify({"ok": True})


@bp.route("/schedule/api/token", methods=["POST"])
def api_new_token():
    """직원용 링크 새로 만들기 (퇴사자가 생겼을 때 등)."""
    cfg = load_config()
    cfg["publicToken"] = secrets.token_urlsafe(12)
    save_config(cfg)
    return jsonify({"ok": True, "url": url_for("schedule.staff_view",
                                               token=cfg["publicToken"], _external=True)})


# ---------------------------------------------------------------------------
# 데이터 내보내기 — 기간을 골라서 뽑는다
# ---------------------------------------------------------------------------
def _hm(h: float) -> str:
    return f"{int(h):02d}:{int(round((h % 1) * 60)):02d}"


def _break_minutes(sh: dict) -> int:
    if isinstance(sh.get("br"), (int, float)):
        return int(sh["br"])
    length = sh["e"] - sh["s"]
    return 60 if length > 8 else (30 if length >= 5 else 0)


def collect_rows(start: date, end: date) -> list[dict]:
    """기간 안의 실제 근무 기록. 기록이 없으면 예정대로 간주한다."""
    rows = []
    cur = monday_of(start)
    while cur <= end:
        week = load_week(cur)
        for i, day in enumerate(week["days"]):
            d = cur + timedelta(days=i)
            if not (start <= d <= end):
                continue
            for sh in sorted(day, key=lambda x: x.get("s", 0)):
                absent = sh.get("note") == "결근"
                worked = 0.0 if absent else (sh["e"] - sh["s"] - _break_minutes(sh) / 60)
                rows.append({
                    "date": d.isoformat(),
                    "label": f"{md(d)} ({DOW[d.weekday()]})",
                    "who": sh.get("w", ""),
                    "plan": f"{_hm(sh['s'])}–{_hm(sh['e'])}",
                    "actual": "결근" if absent else (sh.get("actual") or f"{_hm(sh['s'])}–{_hm(sh['e'])}"),
                    "break": _break_minutes(sh),
                    "hours": round(worked, 2),
                    "note": sh.get("note", "") if sh.get("st") == "diff" else "",
                })
        cur += timedelta(weeks=1)
    return rows


@bp.route("/schedule/export")
def export_data():
    start = parse_iso(request.args.get("from"), date.today().replace(day=1))
    end = parse_iso(request.args.get("to"), date.today())
    if end < start:
        start, end = end, start
    fmt = (request.args.get("fmt") or "md").lower()
    rows = collect_rows(start, end)

    totals: dict[str, float] = {}
    for r in rows:
        totals[r["who"]] = totals.get(r["who"], 0) + r["hours"]

    stamp = f"{start.isoformat()}_{end.isoformat()}"

    def disposition(ext: str) -> str:
        # HTTP 헤더는 latin-1 만 담을 수 있다. 한글 파일명은 RFC 5987 로 따로 싣고,
        # 못 읽는 브라우저를 위해 영문 이름을 함께 준다.
        return (
            f'attachment; filename="schedule_{stamp}.{ext}"; '
            f"filename*=UTF-8''{quote(f'근무기록_{stamp}.{ext}')}"
        )
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["날짜", "이름", "예정", "실제", "휴게(분)", "시간", "비고"])
        for r in rows:
            w.writerow([r["label"], r["who"], r["plan"], r["actual"], r["break"], r["hours"], r["note"]])
        w.writerow([])
        for who, h in sorted(totals.items()):
            w.writerow(["합계", who, "", "", "", round(h, 2), ""])
        # 엑셀이 한글을 깨뜨리지 않게 BOM 을 붙인다
        return Response(
            "﻿" + buf.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": disposition("csv")},
        )

    lines = [
        f"# 근무 기록 {md(start)} ~ {md(end)}",
        "",
        "| 날짜 | 이름 | 예정 | 실제 | 휴게 | 시간 | 비고 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['label']} | {r['who']} | {r['plan']} | {r['actual']} | "
            f"{r['break']}분 | {r['hours']} | {r['note']} |"
        )
    lines += ["", "## 합계", "", "| 이름 | 시간 |", "|---|---|"]
    for who, h in sorted(totals.items()):
        lines.append(f"| {who} | {round(h, 2)} |")

    return Response(
        "\n".join(lines) + "\n",
        mimetype="text/markdown; charset=utf-8",
        headers={"Content-Disposition": disposition("md")},
    )
