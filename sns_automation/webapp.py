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

from . import planner, shot_plan, source_watch, video_editor
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


def _new_project(title: str, *, hook: str = "", menu: str = "", guide: str = "",
                 template: str = "T1", source_dir: str = "") -> dict:
    """프로젝트 하나를 만들고 저장한 뒤 그 dict를 돌려준다.

    create_project(웹 폼) / start_plan_item(주간계획) / 폴더 감시 세 곳이 같은
    규칙을 써야 목록·상세 라우트가 전부 인식한다. 예전엔 복붙이었다.
    """
    pid = f"{int(time.time())}-{_slug(title)}"
    data = {
        "id": pid, "title": title, "hook": hook, "menu": menu,
        "guide": guide, "template": template, "status": ST_SHOOT,
        "created": int(time.time()),
    }
    if source_dir:
        # 소재를 복사하지 않고 창고 폴더를 그대로 읽는다 (4K라 복사가 무겁다)
        data["source_dir"] = source_dir
    os.makedirs(os.path.join(_proj_dir(pid), "raw"), exist_ok=True)
    _save_project(data)
    return data


def _media_dir(p: dict) -> str:
    """이 프로젝트의 소재가 실제로 있는 폴더. 창고 연결분이면 창고를 본다."""
    src = p.get("source_dir")
    if src and os.path.isdir(src):
        return src
    return os.path.join(_proj_dir(p["id"]), "raw")


def _raw_files(pid: str, p: dict | None = None) -> list[dict]:
    p = p or _load_project(pid) or {"id": pid}
    folder = _media_dir(p)
    if not os.path.isdir(folder):
        return []
    out = []
    for name in sorted(os.listdir(folder)):
        if not os.path.isfile(os.path.join(folder, name)):
            continue
        ext = os.path.splitext(name)[1].lower()
        kind = "video" if ext in _VIDEO_EXT else ("image" if ext in _IMAGE_EXT else "other")
        out.append({"name": name, "kind": kind})
    return out


#: 방문 유도 장치 — 목표가 "노출 → 매장 방문 → 매출"이라 규칙으로 박는다.
#: 지역명이 없으면 송도 사람이 자기 동네 가게로 인식하지 못한다(실제 인기글 공통 문법).
BRAND_LABEL = os.getenv("REEL_BRAND_LABEL", "송도 베어글스")
DEFAULT_CTA = os.getenv("REEL_CTA", "저장해두셨다가 놀러 오세요")


def _ensure_plan(p: dict, files: list[dict]) -> dict | None:
    """구성표를 돌려준다. 사장님이 고친 게 있으면 그것, 없으면 자동 생성.

    자동 생성도 '자르는 순간 → 단면' 뼈대를 따르므로, 손대지 않아도
    통짜 이어붙이기보다는 낫다.
    """
    saved = p.get("shot_plan")
    if saved:
        try:
            return shot_plan.normalize(saved)
        except shot_plan.ShotPlanError:
            logger.warning("저장된 구성표가 깨져 자동 생성으로 대체: %s", p.get("id"))
    dur = _durations(p, files)
    clips = [{"name": f["name"], "duration": dur.get(f["name"], 0.0)}
             for f in files if f["kind"] == "video"]
    try:
        return shot_plan.from_clips(
            clips,
            hook=p.get("hook", ""),
            menu=p.get("menu", ""),
            label=BRAND_LABEL,
            cta=DEFAULT_CTA,
            template=p.get("template", "T1"),
        )
    except shot_plan.ShotPlanError as e:
        logger.warning("구성표 자동 생성 실패(%s) → 예전 방식으로 편집", e)
        return None


def _durations(p: dict, files: list[dict]) -> dict[str, float]:
    """영상 길이(초). 4K 프로빙이 느려 project.json에 캐시한다."""
    cache = dict(p.get("durations") or {})
    folder = _media_dir(p)
    changed = False
    for f in files:
        if f["kind"] != "video" or f["name"] in cache:
            continue
        cache[f["name"]] = round(
            video_editor.probe_seconds(os.path.join(folder, f["name"])), 2)
        changed = True
    if changed:
        p["durations"] = cache
        _save_project(p)
    return cache


# ── AI 캡션 생성 (Claude) — 텔레그램 없이 웹에서 ──
_caption_gen = None


def _get_caption_gen():
    """무료 AI(Gemini)가 있으면 캡션 생성기를 준비(지연 초기화). 없으면 None.

    유료 Claude API 는 쓰지 않는다(사장님 지시 2026-08-30) — 캡션 생성은
    GEMINI_API_KEY 하나로 돌고, 없으면 템플릿 폴백이 대신한다.
    """
    global _caption_gen
    try:
        import llm
        if "gemini" not in llm.available_providers():
            return None
    except Exception:
        return None
    if _caption_gen is None:
        from .caption_generator import CaptionGenerator
        _caption_gen = CaptionGenerator()
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

    # (선택) 접속 코드 — .env의 PIPELINE_ACCESS_CODE 설정 시에만 켜짐.
    # 폰/외부에서 접속을 열 때 남이 못 들어오게 하는 간단한 잠금.
    access_code = os.getenv("PIPELINE_ACCESS_CODE", "").strip()
    if access_code:
        @app.middleware("http")
        async def _access_gate(request, call_next):
            given = request.query_params.get("code", "")
            if request.cookies.get("pipe_code") == access_code or given == access_code:
                resp = await call_next(request)
                if given == access_code:
                    resp.set_cookie("pipe_code", access_code,
                                    max_age=90 * 24 * 3600, httponly=True)
                return resp
            return HTMLResponse(
                "<meta charset='utf-8'><body style='font-family:sans-serif;"
                "text-align:center;padding-top:80px'><h2>🔒 접속 코드가 필요해요</h2>"
                "<p>주소 뒤에 <b>?code=접속코드</b> 를 붙여 다시 접속하세요.<br>"
                "예: http://주소:8000/?code=1234</p></body>", status_code=401)

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
        return _new_project(title, hook=hook, menu=menu, guide=guide, template=template)

    # 프로젝트 상세
    @app.get("/api/projects/{pid}")
    async def get_project(pid: str):
        p = _load_project(pid)
        if not p:
            raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
        p["files"] = _raw_files(pid, p)
        p["durations"] = _durations(p, p["files"])
        p["has_reel"] = os.path.exists(os.path.join(_proj_dir(pid), "reel.mp4"))
        # 폴더 위치 표시용 (사진/영상 넣는 곳 · 완성본 저장 곳)
        p["raw_dir"] = os.path.abspath(_media_dir(p))
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
        files = _raw_files(pid, p)
        folder = _media_dir(p)
        videos = [os.path.join(folder, f["name"]) for f in files if f["kind"] == "video"]
        if not videos:
            raise HTTPException(400, "영상 파일이 없어요. 릴스는 영상이 필요해요 (사진만으론 불가).")

        p["hook"] = hook or p.get("hook", "")
        p["menu"] = menu or p.get("menu", "")
        tmpl = get_template(p.get("template"))
        out = os.path.join(_proj_dir(pid), "reel.mp4")
        music = os.getenv("REEL_MUSIC_PATH") or None
        plan = _ensure_plan(p, files)   # 구성표가 있으면(또는 만들 수 있으면) 그걸로

        def _build():
            if plan:
                # 기획안대로 편집 — 구간 컷·샷별 자막·페이오프 슬로우·부분 소리
                video_editor.build_reel_from_plan(plan, folder, out)
                return
            # 구성표를 못 만든 경우에만 예전 방식(통짜 이어붙이기)로 폴백
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
        # 같은 주제로 훅만 바꾼 버전을 여러 개 저장할 수 있게 (덮어쓰지 않음)
        n = 1
        while os.path.exists(os.path.join(
                folder, "reel.mp4" if n == 1 else f"reel_{n}.mp4")):
            n += 1
        reel_name = "reel.mp4" if n == 1 else f"reel_{n}.mp4"
        cap_name = "caption.txt" if n == 1 else f"caption_{n}.txt"
        shutil.copy2(reel_path, os.path.join(folder, reel_name))
        with open(os.path.join(folder, cap_name), "w", encoding="utf-8") as f:
            f.write(caption or p.get("hook", ""))
        p["status"] = ST_DONE
        p["final_path"] = folder
        if caption:
            p["caption"] = caption
        _save_project(p)
        # 훅 라이브러리에 자동 기록 (발행·성과는 나중에 채움)
        planner.record_hook(pid, p.get("title", ""), p.get("hook", ""), reel_name)
        return {"ok": True, "folder": folder, "file": reel_name}

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

    # ═══ ① 주간 촬영 체크리스트 (기획 에이전트) ═══
    @app.get("/api/plan")
    async def get_plan():
        return planner.get_plan()

    @app.post("/api/plan/generate")
    async def generate_plan():
        plan = await planner.generate_weekly_plan(count=3)
        return plan

    @app.post("/api/plan/start")
    async def start_plan_item(index: int = Form(...)):
        plan = planner.get_plan()
        items = plan.get("items", [])
        if not (0 <= index < len(items)):
            raise HTTPException(400, "잘못된 항목입니다.")
        t = items[index]
        data = _new_project(
            t["title"], hook=t.get("hook", ""), menu=t.get("menu", ""),
            guide=t.get("guide", ""), template=t.get("template", "T1"))
        pid = data["id"]
        planner.mark_plan_item(index, pid)
        return data

    # ═══ ② 훅 3버전 추천 (1→N 변형) ═══
    @app.post("/api/projects/{pid}/hooks")
    async def hook_variants(pid: str):
        p = _load_project(pid)
        if not p:
            raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
        r = await planner.suggest_hooks(
            p.get("title", ""), p.get("menu", ""), p.get("hook", ""))
        p["hook_variants"] = r["hooks"]
        _save_project(p)
        return r

    # ═══ 샷 구성표 — 기획안이자 편집 명세 ═══
    @app.get("/api/projects/{pid}/shots")
    async def get_shots(pid: str):
        """현재 구성표. 저장된 게 없으면 클립을 보고 만들어서 보여준다."""
        p = _load_project(pid)
        if not p:
            raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
        files = _raw_files(pid, p)
        plan = _ensure_plan(p, files)
        if not plan:
            raise HTTPException(400, "영상이 없어 구성표를 만들 수 없어요.")
        return {
            "plan": plan,
            "seconds": shot_plan.total_seconds(plan),
            "checklist": shot_plan.checklist(plan),
            "durations": _durations(p, files),
            "saved": bool(p.get("shot_plan")),
        }

    @app.post("/api/projects/{pid}/shots")
    async def save_shots(pid: str, plan: str = Form(...)):
        """사장님이 웹에서 고친 구성표를 저장. 다음 편집부터 이걸 쓴다."""
        p = _load_project(pid)
        if not p:
            raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
        try:
            norm = shot_plan.normalize(json.loads(plan))
        except (ValueError, shot_plan.ShotPlanError) as e:
            raise HTTPException(400, f"구성표가 올바르지 않아요: {e}")
        p["shot_plan"] = norm
        _save_project(p)
        return {"ok": True, "plan": norm, "seconds": shot_plan.total_seconds(norm)}

    def _shot_frames(p: dict, plan: dict, max_px: int = 480) -> list[tuple[str, bytes]]:
        """샷 순서대로 그 시점의 프레임을 뽑는다 (AI가 실제 화면을 보고 자막을 쓰게)."""
        ff = video_editor.ffmpeg_exe()
        folder = _media_dir(p)
        frames: list[tuple[str, bytes]] = []
        for s in plan.get("shots") or []:
            src = os.path.join(folder, s["clip"])
            fd, tmp = tempfile.mkstemp(suffix=".jpg")
            os.close(fd)
            try:
                subprocess.run(
                    [ff, "-y", "-ss", f"{s['in'] + s['dur'] / 2:.2f}", "-i", src,
                     "-frames:v", "1", "-vf", f"scale={max_px}:-2", "-q:v", "5", tmp],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
                with open(tmp, "rb") as f:
                    data = f.read()
                if data:
                    frames.append(("image/jpeg", data))
            except (subprocess.SubprocessError, OSError):
                pass
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return frames

    @app.post("/api/projects/{pid}/shots/ai")
    async def ai_shot_captions(pid: str):
        """AI가 구성표의 말(훅·자막·CTA)을 확정 말투로 쓴다. 구조는 그대로.

        유료 Claude 전용(사장님 확정) — 한 번에 약 $0.1 안팎.
        """
        p = _load_project(pid)
        if not p:
            raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
        files = _raw_files(pid, p)
        plan = _ensure_plan(p, files)
        if not plan:
            raise HTTPException(400, "영상이 없어 구성표를 만들 수 없어요.")
        frames = await asyncio.to_thread(_shot_frames, p, plan)
        try:
            new_plan = await planner.write_shot_captions(
                plan, p.get("title", ""), p.get("menu", ""), frames)
        except Exception as e:
            logger.warning("AI 자막 실패: %s", e)
            raise HTTPException(502, f"AI 자막 실패: {e}")
        p["shot_plan"] = new_plan
        _save_project(p)
        return {"ok": True, "plan": new_plan,
                "seconds": shot_plan.total_seconds(new_plan)}

    @app.post("/api/projects/{pid}/shots/reset")
    async def reset_shots(pid: str):
        """자동 생성으로 되돌리기."""
        p = _load_project(pid)
        if not p:
            raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
        p.pop("shot_plan", None)
        _save_project(p)
        return {"ok": True}

    @app.get("/api/projects/{pid}/clip/{name}")
    async def clip_frame(pid: str, name: str, t: float = 0.0):
        """샷 구성표 표에 쓸 미리보기 프레임 (해당 시점 1장)."""
        p = _load_project(pid)
        if not p:
            raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
        src = os.path.join(_media_dir(p), os.path.basename(name))
        if not os.path.exists(src):
            raise HTTPException(404, "클립을 찾을 수 없습니다.")
        ff = video_editor.ffmpeg_exe()
        fd, tmp = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        try:
            subprocess.run(
                [ff, "-y", "-ss", f"{max(0.0, t):.2f}", "-i", src, "-frames:v", "1",
                 "-vf", "scale=240:-2", "-q:v", "6", tmp],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        except (subprocess.SubprocessError, OSError):
            raise HTTPException(500, "미리보기를 만들지 못했어요.")
        if not os.path.getsize(tmp):
            raise HTTPException(404, "그 시점에는 화면이 없어요.")
        return FileResponse(tmp, media_type="image/jpeg")

    # ═══ 소재 창고 — "폴더에 올리면 시작된다" ═══
    @app.get("/api/source")
    async def source_topics():
        """드라이브 원본소재 폴더의 주제 목록. 이미 프로젝트가 있으면 표시."""
        root = source_watch.source_root()
        if not root:
            return {"root": None, "topics": [],
                    "hint": ".env 의 REEL_SOURCE_DIR 에 소재 폴더 경로를 넣어주세요."}
        linked = {}
        if os.path.isdir(PROJECTS_DIR):
            for pid in os.listdir(PROJECTS_DIR):
                q = _load_project(pid)
                if q and q.get("source_dir"):
                    linked[os.path.normcase(q["source_dir"])] = {
                        "id": q["id"], "status": q.get("status")}
        topics = source_watch.list_topics(root)
        for t in topics:
            t["project"] = linked.get(os.path.normcase(t["path"]))
        return {"root": root, "topics": topics}

    @app.post("/api/source/import")
    async def source_import(topic: str = Form(...)):
        """주제 폴더 하나를 릴스 프로젝트로 연결한다(원본은 복사하지 않음)."""
        root = source_watch.source_root()
        if not root:
            raise HTTPException(400, "소재 폴더를 찾지 못했어요.")
        folder = os.path.join(root, os.path.basename(topic))
        if not os.path.isdir(folder):
            raise HTTPException(404, "그런 주제 폴더가 없어요.")
        media = source_watch.scan_media(folder)
        if not media["videos"]:
            raise HTTPException(400, "그 폴더엔 영상이 없어요. 릴스는 영상이 필요해요.")
        for pid in os.listdir(PROJECTS_DIR) if os.path.isdir(PROJECTS_DIR) else []:
            q = _load_project(pid)
            if q and os.path.normcase(q.get("source_dir", "")) == os.path.normcase(folder):
                return q          # 이미 연결됨 — 중복 생성하지 않는다
        name = os.path.basename(folder)
        data = _new_project(name, menu=name, guide=source_watch.guide_text(folder),
                            source_dir=folder)
        data["status"] = ST_UPLOADED     # 소재가 이미 있으니 촬영대기가 아니다
        _save_project(data)
        return data

    # ═══ ③ 발행 대기 큐 ═══
    @app.get("/api/queue")
    async def publish_queue():
        items = []
        if os.path.isdir(PROJECTS_DIR):
            for pid in os.listdir(PROJECTS_DIR):
                p = _load_project(pid)
                if p and p.get("status") == ST_DONE and not p.get("published"):
                    items.append({
                        "id": p["id"], "title": p.get("title", ""),
                        "hook": p.get("hook", ""),
                        "caption": p.get("caption", ""),
                        "hashtags": p.get("hashtags", []),
                        "final_path": p.get("final_path", ""),
                        "created": p.get("created", 0),
                    })
        items.sort(key=lambda x: x["created"])
        return {"items": items, "rhythm": planner.PUBLISH_RHYTHM}

    @app.post("/api/projects/{pid}/published")
    async def mark_published(pid: str):
        p = _load_project(pid)
        if not p:
            raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
        p["published"] = True
        p["published_at"] = int(time.time())
        _save_project(p)
        planner.mark_published(pid)
        # 릴스 발행 = 마케팅 실행 — MKT 캘린더에 자동 기록 (사장님 지시
        # 2026-08-30). 실패해도 발행 완료 처리를 막지 않는다.
        try:
            from database import mkt_store
            title = (p.get("title") or p.get("menu") or p.get("name") or "").strip()
            mkt_store.auto_record(
                title=f"릴스: {title}" if title else "릴스 발행",
                source_ref=f"reel#{pid}",
                memo="릴스 발행 완료 시 자동 기록")
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True}

    # ═══ ④ 주간 리뷰 (성과 피드백 루프) ═══
    @app.get("/api/review")
    async def review():
        hooks = planner.get_hook_library()
        hooks = [h for h in hooks if h.get("published")]
        hooks.reverse()
        hooks = hooks[:20]

        # 발행 전후 매출 비교 (목표의 '매출' 고리 — 상관관계 참고용).
        # DB 를 못 읽어도 리뷰 화면은 떠야 하므로 실패는 조용히 넘어간다.
        def _attach_sales():
            from . import sales_link
            for h in hooks:
                try:
                    h["sales"] = sales_link.effect(h.get("published_at") or 0)
                except Exception as e:
                    logger.debug("매출 연결 실패(%s): %s", h.get("id"), e)
                    h["sales"] = None
        await asyncio.to_thread(_attach_sales)

        insights = planner.get_insights_log()
        insights.reverse()
        return {"hooks": hooks, "insights": insights[:5]}

    @app.post("/api/hooks/{hid}/result")
    async def hook_result(
        hid: str,
        reach: str = Form(""), saves: str = Form(""),
        shares: str = Form(""), likes: str = Form(""),
    ):
        def _num(v):
            v = v.strip().replace(",", "")
            return int(v) if v.isdigit() else None
        ok = planner.record_result(
            hid, reach=_num(reach), saves=_num(saves),
            shares=_num(shares), likes=_num(likes))
        if not ok:
            raise HTTPException(404, "기록을 찾을 수 없습니다.")
        return {"ok": True}

    @app.post("/api/insights")
    async def insights(
        files: list[UploadFile] = File(default=[]),
        note: str = Form(""),
    ):
        images = []
        for f in files:
            images.append(await f.read())
        if not images and not note:
            raise HTTPException(400, "스크린샷이나 메모를 넣어주세요.")
        r = await planner.analyze_insights(images, note)
        return r

    # 프로젝트 삭제
    @app.delete("/api/projects/{pid}")
    async def delete_project(pid: str):
        d = _proj_dir(pid)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
        return {"ok": True}

    return app
