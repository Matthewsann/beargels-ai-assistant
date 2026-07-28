"""인스타 릴스 파이프라인 (웹앱에 붙는 블루프린트).

주제 고르기 → 프로젝트 생성 → 영상 업로드 → 릴스 자동편집
→ 확인·자막 피드백 → 완성본 저장(구글 드라이브).

영상 편집(ffmpeg)은 PC 성능이 필요하므로 집 PC 웹앱에서 동작한다.
sns_automation.video_editor / caption_generator 를 그대로 사용한다.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

from flask import Blueprint, abort, jsonify, render_template, request, send_file

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sns_automation import video_editor  # noqa: E402
from sns_automation.templates import TEMPLATES, get_template  # noqa: E402

bp = Blueprint("instagram", __name__, url_prefix="/instagram")

PROJECTS_DIR = pathlib.Path(
    os.getenv("PIPELINE_PROJECTS_DIR") or (ROOT / "automation" / "reels")
)
FINAL_DIR = pathlib.Path(
    os.getenv("PIPELINE_FINAL_DIR") or (ROOT / "automation" / "reels_완성본")
)

_VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}
_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}

ST_SHOOT, ST_UPLOADED, ST_EDITED, ST_DONE = "촬영대기", "업로드됨", "편집완료", "완성"

# 촬영 가이드가 붙은 주제 추천 (knowledge/growth_strategy.md 기반)
SUGGESTED_TOPICS = [
    {"title": "크림치즈 듬뿍 바르는 순간", "pillar": "메뉴 클로즈업", "template": "T1",
     "hook": "크림치즈, 이만큼 들어갑니다", "menu": "송도 크림치즈 베이글",
     "guide": "크림치즈를 두툼하게 바르는 손동작을 세로로 가까이. 5~10초. 갓 바른 결이 보이게."},
    {"title": "제철 과일산도 단면", "pillar": "메뉴 클로즈업", "template": "T1",
     "hook": "이번 주 신상, 과일 통째로", "menu": "송도 과일 산도",
     "guide": "산도를 반으로 가른 단면을 정면 클로즈업. 크림·과일이 꽉 찬 게 보이게."},
    {"title": "사장 vs 알바 대결", "pillar": "비하인드", "template": "T5",
     "hook": "누가 제일 잘 썰까?", "menu": "베어글스 송도",
     "guide": "같은 재료를 여러 명이 써는 걸 각각 짧게. 대결 구도. 표정·손 클로즈업 섞기."},
    {"title": "베이글 데우는 법 (꿀팁)", "pillar": "정보·팁", "template": "T4",
     "hook": "베이글 겉바속촉 데우는 법", "menu": "송도 베이글",
     "guide": "1·2·3 스텝으로 데우는 과정. 각 스텝 짧게. 마지막에 겉바속촉 단면."},
    {"title": "말차 소금크림 라떼", "pillar": "신메뉴", "template": "T2",
     "hook": "단짠, 제주 말차 소금크림", "menu": "말차 소금크림 라떼",
     "guide": "소금크림 얹는 순간 + 잔에 붓는 컷. 위에서 아래로 층 보이게."},
    {"title": "잠봉뵈르 베이글", "pillar": "메뉴 클로즈업", "template": "T1",
     "hook": "송도에서 이 조합은 여기뿐", "menu": "잠봉뵈르 베이글",
     "guide": "잠봉·버터 층이 보이게 단면 클로즈업. 한 입 베어무는 컷 있으면 더 좋음."},
]


def _slug(text: str) -> str:
    s = re.sub(r"[^\w가-힣]+", "-", (text or "").strip()).strip("-")
    return s[:40] or "topic"


def _pdir(pid: str) -> pathlib.Path:
    return PROJECTS_DIR / pid


def _load(pid: str) -> dict | None:
    f = _pdir(pid) / "project.json"
    if not f.exists():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


def _save(data: dict) -> None:
    d = _pdir(data["id"])
    d.mkdir(parents=True, exist_ok=True)
    data["updated"] = int(time.time())
    (d / "project.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _files(pid: str) -> list[dict]:
    raw = _pdir(pid) / "raw"
    if not raw.is_dir():
        return []
    out = []
    for p in sorted(raw.iterdir()):
        ext = p.suffix.lower()
        kind = "video" if ext in _VIDEO_EXT else ("image" if ext in _IMAGE_EXT else "other")
        out.append({"name": p.name, "kind": kind})
    return out


def _detail(pid: str) -> dict | None:
    p = _load(pid)
    if not p:
        return None
    p["files"] = _files(pid)
    p["has_reel"] = (_pdir(pid) / "reel.mp4").exists()
    p["raw_dir"] = str((_pdir(pid) / "raw").resolve())
    p["final_dir"] = str((FINAL_DIR / _slug(p.get("title", ""))).resolve())
    return p


# ── 화면 ──────────────────────────────────────────────
@bp.route("/")
def index():
    projects = []
    if PROJECTS_DIR.is_dir():
        for d in PROJECTS_DIR.iterdir():
            p = _load(d.name)
            if p:
                p["file_count"] = len(_files(d.name))
                projects.append(p)
    projects.sort(key=lambda x: x.get("created", 0), reverse=True)
    return render_template("instagram.html", topics=SUGGESTED_TOPICS, projects=projects,
                           reel_templates=list(TEMPLATES.values()))


# ── API ───────────────────────────────────────────────
@bp.post("/api/projects")
def create_project():
    f = request.form
    title = (f.get("title") or "").strip()
    if not title:
        return jsonify({"error": "주제를 입력해주세요."}), 400
    pid = f"{int(time.time())}-{_slug(title)}"
    data = {"id": pid, "title": title, "hook": f.get("hook", ""), "menu": f.get("menu", ""),
            "guide": f.get("guide", ""), "template": f.get("template", "T1"),
            "status": ST_SHOOT, "created": int(time.time())}
    (_pdir(pid) / "raw").mkdir(parents=True, exist_ok=True)
    _save(data)
    return jsonify(data)


@bp.get("/api/projects/<pid>")
def get_project(pid):
    p = _detail(pid)
    if not p:
        abort(404)
    return jsonify(p)


@bp.post("/api/projects/<pid>/upload")
def upload(pid):
    if not _load(pid):
        abort(404)
    raw = _pdir(pid) / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in request.files.getlist("files"):
        name = os.path.basename(f.filename or "file")
        f.save(str(raw / name))
        saved.append(name)
    p = _load(pid)
    if p["status"] == ST_SHOOT and saved:
        p["status"] = ST_UPLOADED
        _save(p)
    return jsonify({"saved": saved, "files": _files(pid)})


@bp.post("/api/projects/<pid>/caption")
def caption(pid):
    """AI 캡션·자막 생성 (크레딧 없으면 브랜드 키워드 기반 초안)."""
    p = _load(pid)
    if not p:
        abort(404)
    used_ai, data = False, None
    key = os.getenv("ANTHROPIC_API_KEY")
    if key:
        try:
            import asyncio

            from sns_automation.caption_generator import CaptionGenerator
            frame = _first_frame(pid)
            gen = CaptionGenerator(key)
            res = asyncio.run(gen.generate(
                images=[frame] if frame else [], topic=p.get("title", ""),
                is_reel=True, media_count=max(1, len(_files(pid))),
            ))
            data = {"menu": res.menu, "caption": res.caption,
                    "hashtags": res.hashtags, "overlay_text": res.overlay_text}
            used_ai = True
        except Exception as e:  # 크레딧 없음 등 → 초안으로
            print(f"[인스타] AI 캡션 실패 → 초안 사용: {e}")
    if data is None:
        menu = (p.get("menu") or p.get("title") or "베이글").strip()
        hook = (p.get("hook") or "").strip()
        lines = ([hook] if hook else []) + [f"송도에서 즐기는 {menu} 🥯", "오늘도 잠시 쉬어가세요."]
        tags = ["#베어글스송도", "#송도베이글", "#송도카페", "#송도맛집"]
        m = re.sub(r"\s+", "", menu)
        if m and f"#{m}" not in tags:
            tags.append(f"#{m}")
        data = {"menu": menu, "caption": "\n".join(lines), "hashtags": tags[:5],
                "overlay_text": hook}
    p["caption"] = data["caption"]
    p["hashtags"] = data["hashtags"]
    if data.get("menu"):
        p["menu"] = data["menu"]
    if data.get("overlay_text"):
        p["hook"] = data["overlay_text"]
    _save(p)
    return jsonify({"ok": True, "ai": used_ai, **data})


def _first_frame(pid: str) -> bytes | None:
    vids = [str(_pdir(pid) / "raw" / f["name"]) for f in _files(pid) if f["kind"] == "video"]
    if not vids:
        return None
    ff = video_editor.ffmpeg_exe()
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "f.jpg")
        subprocess.run([ff, "-y", "-ss", "1", "-i", vids[0], "-frames:v", "1",
                        "-vf", "scale=1000:-1", out], capture_output=True)
        if os.path.exists(out):
            return pathlib.Path(out).read_bytes()
    return None


@bp.post("/api/projects/<pid>/render")
def render(pid):
    p = _load(pid)
    if not p:
        abort(404)
    videos = [str(_pdir(pid) / "raw" / f["name"]) for f in _files(pid) if f["kind"] == "video"]
    if not videos:
        return jsonify({"error": "영상 파일이 없어요. 릴스는 영상이 필요해요."}), 400
    p["hook"] = request.form.get("hook") or p.get("hook", "")
    p["menu"] = request.form.get("menu") or p.get("menu", "")
    if request.form.get("template"):
        p["template"] = request.form["template"]
    tmpl = get_template(p.get("template"))
    music = os.getenv("REEL_MUSIC_PATH")
    try:
        video_editor.build_reel(
            videos, str(_pdir(pid) / "reel.mp4"),
            target_seconds=tmpl.target_seconds,
            hook=p["hook"] or None, menu=p["menu"] or None,
            watermark="", keep_audio=True,
            music_path=music if (music and os.path.exists(music)) else None,
        )
    except Exception as e:
        return jsonify({"error": f"편집 실패: {e}"}), 500
    p["status"] = ST_EDITED
    _save(p)
    return jsonify({"ok": True, "status": p["status"]})


@bp.get("/api/projects/<pid>/reel")
def reel(pid):
    f = _pdir(pid) / "reel.mp4"
    if not f.exists():
        abort(404)
    return send_file(str(f), mimetype="video/mp4")


@bp.post("/api/projects/<pid>/finalize")
def finalize(pid):
    p = _load(pid)
    if not p:
        abort(404)
    src = _pdir(pid) / "reel.mp4"
    if not src.exists():
        return jsonify({"error": "먼저 릴스를 만들어주세요."}), 400
    folder = FINAL_DIR / _slug(p["title"])
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(src), str(folder / "reel.mp4"))
    (folder / "caption.txt").write_text(
        request.form.get("caption") or p.get("caption", ""), encoding="utf-8"
    )
    p["status"] = ST_DONE
    p["final_path"] = str(folder)
    _save(p)
    return jsonify({"ok": True, "folder": str(folder)})


@bp.post("/api/projects/<pid>/open")
def open_folder(pid):
    p = _load(pid)
    if not p:
        abort(404)
    which = request.form.get("which", "raw")
    target = (FINAL_DIR / _slug(p["title"])) if which == "final" else (_pdir(pid) / "raw")
    target.mkdir(parents=True, exist_ok=True)
    opened = False
    try:
        os.startfile(str(target))  # Windows 탐색기
        opened = True
    except Exception:
        pass
    return jsonify({"ok": True, "opened": opened, "path": str(target.resolve())})


@bp.post("/api/projects/<pid>/delete")
def delete_project(pid):
    shutil.rmtree(str(_pdir(pid)), ignore_errors=True)
    return jsonify({"ok": True})
