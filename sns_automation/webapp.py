"""베어글스 인스타 파이프라인 웹페이지 (FastAPI, 로컬 기반).

6단계 흐름을 브라우저에서:
  ① 주제 고르기(+촬영 가이드) → ② 프로젝트 생성 → ③ 영상 업로드
  → ④ 릴스 자동편집 → ⑤ 미리보기·피드백(자막 수정·재편집) → ⑥ 완성본 저장

구글 드라이브/크레딧 없이 **PC 로컬 폴더 + 브라우저 업로드**로 동작한다.
집 PC에서 `python run_web.py` → http://localhost:8000

폴더 구조:
  projects/<프로젝트id>/raw/     ← 업로드한 원본
  projects/<프로젝트id>/reel.mp4 ← 자동편집 결과
  projects/<프로젝트id>/project.json
  완성본/<주제>/                 ← 최종 저장(발행용)
"""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import video_editor
from .templates import TEMPLATES, get_template

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(__file__))
_WEB_DIR = os.path.join(_ROOT, "web")
PROJECTS_DIR = os.getenv("PIPELINE_PROJECTS_DIR") or os.path.join(_ROOT, "projects")
FINAL_DIR = os.getenv("PIPELINE_FINAL_DIR") or os.path.join(_ROOT, "완성본")

_VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}
_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}

# ── 촬영 가이드가 붙은 주제 추천 (knowledge/growth_strategy.md 기반) ──
SUGGESTED_TOPICS = [
    {
        "title": "크림치즈 듬뿍 바르는 순간",
        "pillar": "메뉴 클로즈업",
        "template": "T1",
        "hook": "크림치즈, 이만큼 들어갑니다",
        "menu": "송도 크림치즈 베이글",
        "guide": "크림치즈를 두툼하게 바르는 손동작을 세로로 가까이. 5~10초. 갓 바른 결이 보이게 클로즈업.",
    },
    {
        "title": "제철 과일산도 단면",
        "pillar": "메뉴 클로즈업",
        "template": "T1",
        "hook": "이번 주 신상, 과일 통째로",
        "menu": "송도 과일 산도",
        "guide": "산도를 반으로 가른 단면을 정면 클로즈업. 크림·과일이 꽉 찬 게 보이게. 천천히 들어올리기.",
    },
    {
        "title": "사장 vs 알바 대결",
        "pillar": "비하인드",
        "template": "T5",
        "hook": "누가 제일 잘 썰까?",
        "menu": "베어글스 송도",
        "guide": "같은 재료(토마토 등)를 여러 명이 써는 걸 각각 짧게. 대결 구도. 표정·손 클로즈업 섞기.",
    },
    {
        "title": "베이글 데우는 법 (꿀팁)",
        "pillar": "정보·팁",
        "template": "T4",
        "hook": "베이글 겉바속촉 데우는 법",
        "menu": "송도 베이글",
        "guide": "1·2·3 스텝으로 데우는 과정. 각 스텝 짧게. 마지막에 겉바속촉 단면.",
    },
    {
        "title": "말차 소금크림 라떼",
        "pillar": "신메뉴",
        "template": "T2",
        "hook": "단짠, 제주 말차 소금크림",
        "menu": "말차 소금크림 라떼",
        "guide": "소금크림 얹는 순간 + 잔에 붓는 컷. 위에서 아래로 층 보이게. 시원한 얼음.",
    },
    {
        "title": "잠봉뵈르 베이글",
        "pillar": "메뉴 클로즈업",
        "template": "T1",
        "hook": "송도에서 이 조합은 여기뿐",
        "menu": "잠봉뵈르 베이글",
        "guide": "잠봉·버터 층이 보이게 단면 클로즈업. 한 입 베어무는 컷 있으면 더 좋음.",
    },
]

# 프로젝트 상태 (6단계 흐름)
ST_SHOOT = "촬영대기"      # 프로젝트 생성됨, 업로드 전
ST_UPLOADED = "업로드됨"   # 원본 올라옴, 편집 전
ST_EDITED = "편집완료"     # 릴스 생성됨, 확인/피드백
ST_DONE = "완성"           # 완성본 저장됨


def _slug(text: str) -> str:
    s = re.sub(r"[^\w가-힣]+", "-", text.strip()).strip("-")
    return s[:40] or "topic"


def _proj_dir(pid: str) -> str:
    return os.path.join(PROJECTS_DIR, pid)


def _load_project(pid: str) -> dict | None:
    meta = os.path.join(_proj_dir(pid), "project.json")
    if not os.path.exists(meta):
        return None
    with open(meta, encoding="utf-8") as f:
        return json.load(f)


def _save_project(data: dict) -> None:
    d = _proj_dir(data["id"])
    os.makedirs(d, exist_ok=True)
    data["updated"] = int(time.time())
    with open(os.path.join(d, "project.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _raw_files(pid: str) -> list[dict]:
    raw = os.path.join(_proj_dir(pid), "raw")
    if not os.path.isdir(raw):
        return []
    out = []
    for name in sorted(os.listdir(raw)):
        ext = os.path.splitext(name)[1].lower()
        kind = "video" if ext in _VIDEO_EXT else ("image" if ext in _IMAGE_EXT else "other")
        out.append({"name": name, "kind": kind})
    return out


# ── AI 캡션 생성 (Claude) — 텔레그램 없이 웹에서 ──
_caption_gen = None


def _get_caption_gen():
    """ANTHROPIC_API_KEY가 있으면 캡션 생성기를 준비(지연 초기화). 없으면 None."""
    global _caption_gen
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    if _caption_gen is None:
        from .caption_generator import CaptionGenerator
        _caption_gen = CaptionGenerator(key)
    return _caption_gen


def _first_video_frame(pid: str) -> bytes | None:
    """업로드한 첫 영상에서 대표 프레임 1장을 뽑아 이미지 바이트로 반환."""
    raw = os.path.join(_proj_dir(pid), "raw")
    videos = [os.path.join(raw, f["name"]) for f in _raw_files(pid) if f["kind"] == "video"]
    if not videos:
        return None
    ff = video_editor.ffmpeg_exe()
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "frame.jpg")
        subprocess.run(
            [ff, "-y", "-ss", "1", "-i", videos[0], "-frames:v", "1",
             "-vf", "scale=1000:-1", out],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if os.path.exists(out):
            with open(out, "rb") as f:
                return f.read()
    return None


def _fallback_caption(p: dict) -> dict:
    """크레딧/키가 없을 때 쓰는 간단 캡션(브랜드·지역·메뉴 키워드 기반)."""
    menu = (p.get("menu") or p.get("title") or "베이글").strip()
    hook = (p.get("hook") or "").strip()
    lines = [hook] if hook else []
    lines.append(f"송도에서 즐기는 {menu} 🥯")
    lines.append("오늘도 잠시 쉬어가세요.")
    tags = ["#베어글스송도", "#송도베이글", "#송도카페", "#송도맛집"]
    m = re.sub(r"\s+", "", menu)
    if m and f"#{m}" not in tags:
        tags.append(f"#{m}")
    return {"menu": menu, "caption": "\n".join(lines),
            "hashtags": tags[:5], "overlay_text": hook}


def create_app() -> FastAPI:
    os.makedirs(PROJECTS_DIR, exist_ok=True)
    os.makedirs(FINAL_DIR, exist_ok=True)
    app = FastAPI(title="베어글스 인스타 파이프라인")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        with open(os.path.join(_WEB_DIR, "pipeline.html"), encoding="utf-8") as f:
            return f.read()

    # ① 주제 추천 + 촬영 가이드
    @app.get("/api/topics")
    async def topics():
        return {"topics": SUGGESTED_TOPICS,
                "templates": [{"id": t.id, "name": t.name} for t in TEMPLATES.values()]}

    # 프로젝트 목록
    @app.get("/api/projects")
    async def list_projects():
        items = []
        if os.path.isdir(PROJECTS_DIR):
            for pid in os.listdir(PROJECTS_DIR):
                p = _load_project(pid)
                if p:
                    p["file_count"] = len(_raw_files(pid))
                    items.append(p)
        items.sort(key=lambda x: x.get("created", 0), reverse=True)
        return {"projects": items}

    # ② 프로젝트 생성
    @app.post("/api/projects")
    async def create_project(
        title: str = Form(...),
        hook: str = Form(""),
        menu: str = Form(""),
        guide: str = Form(""),
        template: str = Form("T1"),
    ):
        pid = f"{int(time.time())}-{_slug(title)}"
        data = {
            "id": pid, "title": title, "hook": hook, "menu": menu,
            "guide": guide, "template": template, "status": ST_SHOOT,
            "created": int(time.time()),
        }
        os.makedirs(os.path.join(_proj_dir(pid), "raw"), exist_ok=True)
        _save_project(data)
        return data

    # 프로젝트 상세
    @app.get("/api/projects/{pid}")
    async def get_project(pid: str):
        p = _load_project(pid)
        if not p:
            raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
        p["files"] = _raw_files(pid)
        p["has_reel"] = os.path.exists(os.path.join(_proj_dir(pid), "reel.mp4"))
        # 폴더 위치 표시용 (사진/영상 넣는 곳 · 완성본 저장 곳)
        p["raw_dir"] = os.path.abspath(os.path.join(_proj_dir(pid), "raw"))
        p["final_dir"] = os.path.abspath(os.path.join(FINAL_DIR, _slug(p.get("title", ""))))
        return p

    # ③ 업로드
    @app.post("/api/projects/{pid}/upload")
    async def upload(pid: str, files: list[UploadFile] = File(...)):
        p = _load_project(pid)
        if not p:
            raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
        raw = os.path.join(_proj_dir(pid), "raw")
        os.makedirs(raw, exist_ok=True)
        saved = []
        for f in files:
            safe = os.path.basename(f.filename or "file")
            dest = os.path.join(raw, safe)
            with open(dest, "wb") as out:
                while chunk := await f.read(1 << 20):
                    out.write(chunk)
            saved.append(safe)
        if p["status"] == ST_SHOOT:
            p["status"] = ST_UPLOADED
            _save_project(p)
        return {"saved": saved, "files": _raw_files(pid)}

    # ④ 릴스 자동편집 (+ ⑤ 재편집: hook/menu 바꿔 다시 호출)
    @app.post("/api/projects/{pid}/render")
    async def render(pid: str, hook: str = Form(""), menu: str = Form("")):
        p = _load_project(pid)
        if not p:
            raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
        raw = os.path.join(_proj_dir(pid), "raw")
        videos = [os.path.join(raw, f["name"]) for f in _raw_files(pid) if f["kind"] == "video"]
        if not videos:
            raise HTTPException(400, "영상 파일이 없어요. 릴스는 영상이 필요해요 (사진만으론 불가).")

        p["hook"] = hook or p.get("hook", "")
        p["menu"] = menu or p.get("menu", "")
        tmpl = get_template(p.get("template"))
        out = os.path.join(_proj_dir(pid), "reel.mp4")
        music = os.getenv("REEL_MUSIC_PATH") or None

        def _build():
            video_editor.build_reel(
                videos, out,
                target_seconds=tmpl.target_seconds,
                hook=p["hook"] or None,
                menu=p["menu"] or None,
                watermark="",
                keep_audio=True,
                music_path=music if (music and os.path.exists(music)) else None,
            )

        try:
            await asyncio.to_thread(_build)
        except Exception as e:
            logger.exception("릴스 편집 실패: %s", pid)
            raise HTTPException(500, f"편집 실패: {e}")

        p["status"] = ST_EDITED
        _save_project(p)
        return {"ok": True, "status": p["status"]}

    # ✨ AI 캡션·자막 생성 (텔레그램 없이 웹에서)
    @app.post("/api/projects/{pid}/caption")
    async def caption(pid: str):
        p = _load_project(pid)
        if not p:
            raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
        videos = [f for f in _raw_files(pid) if f["kind"] == "video"]
        gen = _get_caption_gen()
        used_ai = False
        data = None
        if gen:
            try:
                frame = await asyncio.to_thread(_first_video_frame, pid)
                res = await gen.generate(
                    images=[frame] if frame else [],
                    topic=p.get("title", ""),
                    is_reel=True,
                    media_count=max(1, len(videos)),
                )
                data = {"menu": res.menu, "caption": res.caption,
                        "hashtags": res.hashtags, "overlay_text": res.overlay_text}
                used_ai = True
            except Exception as e:
                logger.warning("AI 캡션 실패 → 템플릿 사용: %s", e)
        if data is None:
            data = _fallback_caption(p)

        # 프로젝트에 저장 (자막/캡션 채우기)
        p["caption"] = data["caption"]
        p["hashtags"] = data["hashtags"]
        if data.get("menu"):
            p["menu"] = data["menu"]
        if data.get("overlay_text"):
            p["hook"] = data["overlay_text"]
        _save_project(p)
        return {"ok": True, "ai": used_ai, **data}

    # 릴스 미리보기 스트리밍
    @app.get("/api/projects/{pid}/reel")
    async def reel(pid: str):
        path = os.path.join(_proj_dir(pid), "reel.mp4")
        if not os.path.exists(path):
            raise HTTPException(404, "아직 편집된 릴스가 없어요.")
        return FileResponse(path, media_type="video/mp4")

    # ⑥ 완성본 저장
    @app.post("/api/projects/{pid}/finalize")
    async def finalize(pid: str, caption: str = Form("")):
        p = _load_project(pid)
        if not p:
            raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
        reel_path = os.path.join(_proj_dir(pid), "reel.mp4")
        if not os.path.exists(reel_path):
            raise HTTPException(400, "먼저 릴스를 생성해주세요.")
        folder = os.path.join(FINAL_DIR, f"{_slug(p['title'])}")
        os.makedirs(folder, exist_ok=True)
        shutil.copy2(reel_path, os.path.join(folder, "reel.mp4"))
        with open(os.path.join(folder, "caption.txt"), "w", encoding="utf-8") as f:
            f.write(caption or p.get("hook", ""))
        p["status"] = ST_DONE
        p["final_path"] = folder
        _save_project(p)
        return {"ok": True, "folder": folder}

    # 📁 폴더 열기 (윈도우 탐색기) — 촬영본(raw) / 완성본(final)
    @app.post("/api/projects/{pid}/open")
    async def open_folder(pid: str, which: str = Form("raw")):
        p = _load_project(pid)
        if not p:
            raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
        if which == "final":
            target = os.path.join(FINAL_DIR, _slug(p.get("title", "")))
        else:
            target = os.path.join(_proj_dir(pid), "raw")
        target = os.path.abspath(target)
        os.makedirs(target, exist_ok=True)
        opened = False
        try:
            os.startfile(target)  # Windows 탐색기로 열기
            opened = True
        except Exception as e:  # 서버가 GUI 없는 환경이면 못 염 (경로는 반환)
            logger.warning("폴더 열기 실패(경로만 반환): %s", e)
        return {"ok": True, "opened": opened, "path": target}

    # 프로젝트 삭제
    @app.delete("/api/projects/{pid}")
    async def delete_project(pid: str):
        d = _proj_dir(pid)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
        return {"ok": True}

    return app
