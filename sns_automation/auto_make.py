"""릴스 전 과정 자동 실행 — 직원 웹의 [릴스 만들기] 버튼이 부르는 일.

직원 웹앱(외부 서버)은 영상을 못 만지므로, 버튼은 jobs 큐(kind='reel')에
요청만 넣고 **집 PC 일꾼이 이 모듈로 전 과정을 돌린다**:

    소재 폴더 찾기 → 프로젝트 연결 → 메모 저장 → 샷 구성표
    → AI 자막(유료 Claude) → 렌더 → AI 캡션 → 완성본 저장
    → 클라우드 업로드(직원 웹 ② 목록에 뜸)

파이프라인 웹앱(run_web.py)과 같은 헬퍼를 쓰므로, 웹에서 손으로 하던 것과
결과가 동일하다. AI 비용: 릴스 1편당 자막+캡션 ≈ $0.15 안팎.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile

logger = logging.getLogger(__name__)


class MakeError(RuntimeError):
    """사람이 읽고 조치할 수 있는 실패 메시지."""


def _find_folder(topic: str) -> str:
    """소재 창고에서 주제 폴더를 찾는다(정확히 → 부분일치 순)."""
    from . import source_watch
    root = source_watch.source_root()
    if not root:
        raise MakeError("집 PC에서 소재 폴더(원본소재)를 찾지 못했어요.")
    exact = os.path.join(root, topic)
    if os.path.isdir(exact):
        return exact
    t = topic.casefold().replace(" ", "")
    for item in source_watch.list_topics(root):
        if t in item["topic"].casefold().replace(" ", ""):
            return item["path"]
    raise MakeError(f"'{topic}' 폴더를 원본소재에서 찾지 못했어요. "
                    "드라이브 업로드가 아직 동기화 중일 수 있어요(1~2분 뒤 다시).")


def _project_for(folder: str, memo: str):
    """폴더에 연결된 프로젝트를 찾거나 만든다. 메모가 오면 갱신한다."""
    from . import source_watch
    from . import webapp as wa
    name = os.path.basename(folder)
    p = None
    if os.path.isdir(wa.PROJECTS_DIR):
        for pid in os.listdir(wa.PROJECTS_DIR):
            q = wa._load_project(pid)
            if q and os.path.normcase(q.get("source_dir", "")) == os.path.normcase(folder):
                p = q
                break
    if p is None:
        p = wa._new_project(name, menu=name,
                            guide=memo or source_watch.guide_text(folder),
                            source_dir=folder)
        p["status"] = wa.ST_UPLOADED
    if memo:
        p["guide"] = memo.strip()[:2000]
    wa._save_project(p)
    return p


def _frames_for(p: dict, plan: dict, max_px: int = 480) -> list[tuple[str, bytes]]:
    """샷 중간 지점의 프레임들 — AI가 실제 화면을 보고 자막을 쓰게."""
    from . import video_editor
    from . import webapp as wa
    ff = video_editor.ffmpeg_exe()
    folder = wa._media_dir(p)
    frames: list[tuple[str, bytes]] = []
    for s in plan.get("shots") or []:
        fd, tmp = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        try:
            subprocess.run(
                [ff, "-y", "-ss", f"{s['in'] + s['dur'] / 2:.2f}",
                 "-i", os.path.join(folder, s["clip"]), "-frames:v", "1",
                 "-vf", f"scale={max_px}:-2", "-q:v", "5", tmp],
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


#: 장면 선택용 프레임 예산 — 클립이 많아도 이 안에서 나눠 뽑는다(비용·속도 제어).
FRAME_BUDGET = 48
MAX_CLIPS_TO_SCAN = 12


def footage_plan(p: dict, files: list[dict]) -> dict:
    """AI 가 촬영본을 직접 보고 만든 구성표 (장면 선택 + 자막을 한 번에).

    클립마다 일정 간격 프레임을 뽑아 전부 보여주고, "단면이 갈라지는 그 순간"
    같은 결정적 구간의 시각을 AI 가 직접 찍는다. 실패하면 예외.
    """
    from . import planner, video_editor
    from . import webapp as wa

    folder = wa._media_dir(p)
    durs = wa._durations(p, files)
    videos = [(f["name"], durs.get(f["name"], 0.0))
              for f in files if f["kind"] == "video"]
    videos = [(n, d) for n, d in videos if d >= 1.0]
    if not videos:
        raise MakeError("쓸 수 있는 영상이 없어요(1초 이상 필요).")
    # 클립이 수십 개면 다 못 본다 — 긴 것부터(내용이 많을 확률) 상위만 훑는다
    videos.sort(key=lambda x: x[1], reverse=True)
    videos = videos[:MAX_CLIPS_TO_SCAN]
    per_clip = max(2, FRAME_BUDGET // len(videos))

    clips = []
    for name, d in videos:
        frames = video_editor.sample_frames(
            os.path.join(folder, name), d, max_frames=per_clip)
        if frames:
            clips.append({"name": name, "duration": round(d, 2), "frames": frames})
    if not clips:
        raise MakeError("프레임을 뽑지 못했어요.")
    return asyncio.run(planner.plan_from_footage(
        clips, p.get("title", ""), p.get("menu", ""), p.get("guide", "")))


def make_reel(topic: str, memo: str = "") -> dict:
    """주제 폴더 하나로 릴스 완성본까지. 성공 시 요약 dict 반환."""
    from . import cloud_sync, planner, shot_plan, video_editor
    from . import webapp as wa

    folder = _find_folder(topic)
    p = _project_for(folder, memo)
    pid, title = p["id"], p.get("title", topic)
    guide = p.get("guide", "")

    files = wa._raw_files(pid, p)
    videos = [f for f in files if f["kind"] == "video"]
    if not videos:
        raise MakeError(f"'{title}' 폴더에 영상이 없어요. 릴스는 영상이 필요해요.")

    # ① 구성표 — 사장님이 저장한 게 있으면 그것(구조 존중),
    #    없으면 **AI 장면 선택**(1순위 설계), 그것도 실패하면 어림짐작 뼈대.
    plan, ai_captions, ai_selected = None, False, False
    if not p.get("shot_plan"):
        try:
            plan = footage_plan(p, files)
            ai_captions = ai_selected = True     # 장면과 말을 한 번에 골랐다
        except Exception as e:
            logger.warning("AI 장면 선택 실패(어림짐작 뼈대로 진행): %s", e)
    if plan is None:
        plan = wa._ensure_plan(p, files)
    if not plan:
        raise MakeError("영상 길이를 읽지 못해 구성표를 만들 수 없었어요.")

    # ② AI 자막 — 장면 선택이 이미 말까지 썼으면 건너뛴다
    frames = _frames_for(p, plan)
    if not ai_captions:
        try:
            plan = asyncio.run(planner.write_shot_captions(
                plan, title, p.get("menu", title), frames, guide=guide))
            ai_captions = True
        except Exception as e:
            logger.warning("AI 자막 실패(뼈대 자막으로 진행): %s", e)
    p["shot_plan"] = plan
    wa._save_project(p)

    # ③ 렌더
    out = os.path.join(wa._proj_dir(pid), "reel.mp4")
    res = video_editor.build_reel_from_plan(plan, wa._media_dir(p), out)
    p["status"] = wa.ST_EDITED
    wa._save_project(p)

    # ④ AI 캡션 — 실패 시 템플릿 폴백
    caption = ""
    try:
        gen = wa._get_caption_gen()
        if gen and frames:
            r = asyncio.run(gen.generate(
                images=[frames[0][1]], topic=title, is_reel=True,
                media_count=len(videos), note=guide))
            caption = r.full_text
            p["caption"], p["hashtags"] = r.caption, r.hashtags
    except Exception as e:
        logger.warning("AI 캡션 실패(템플릿 폴백): %s", e)
    if not caption:
        fb = wa._fallback_caption(p)
        caption = fb["caption"] + "\n\n" + " ".join(fb["hashtags"])

    # ⑤ 완성본 저장 (웹 finalize 와 같은 규칙: 버전 번호 붙여 안 덮어씀)
    final_dir = os.path.join(wa.FINAL_DIR, wa._slug(title))
    os.makedirs(final_dir, exist_ok=True)
    n = 1
    while os.path.exists(os.path.join(
            final_dir, "reel.mp4" if n == 1 else f"reel_{n}.mp4")):
        n += 1
    reel_name = "reel.mp4" if n == 1 else f"reel_{n}.mp4"
    cap_name = "caption.txt" if n == 1 else f"caption_{n}.txt"
    shutil.copy2(out, os.path.join(final_dir, reel_name))
    with open(os.path.join(final_dir, cap_name), "w", encoding="utf-8") as f:
        f.write(caption)
    p["status"] = wa.ST_DONE
    p["final_path"] = final_dir
    wa._save_project(p)
    planner.record_hook(pid, title, (plan.get("hook") or {}).get("text", ""), reel_name)

    # ⑥ 클라우드 업로드 → 직원 웹 ② 목록
    cloud = None
    try:
        entry = cloud_sync.push_reel(pid, title,
                                     os.path.join(final_dir, reel_name), caption)
        cloud = entry.get("video")
    except Exception as e:
        logger.warning("클라우드 업로드 실패(로컬 저장은 완료): %s", e)

    return {"title": title, "seconds": res.get("seconds"),
            "ai_captions": ai_captions, "ai_selected": ai_selected,
            "cloud": bool(cloud), "file": reel_name}


def _pipeline_url() -> str:
    """집 PC 파이프라인 화면 주소 — 같은 와이파이의 폰에서 열 수 있는 링크.

    IP 가 바뀔 수 있어(DHCP) 목록을 올릴 때마다 현재 값을 다시 잰다.
    접속 코드가 설정돼 있으면 ?code= 를 붙여 바로 열리게 한다.
    """
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except OSError:
        return ""
    port = os.getenv("PIPELINE_PORT", "8000")
    url = f"http://{ip}:{port}"
    code = os.getenv("PIPELINE_ACCESS_CODE", "").strip()
    return f"{url}/?code={code}" if code else url


def push_topics() -> None:
    """소재 폴더 목록을 클라우드에 올린다 — 직원 웹의 주제 선택칸이 읽는다."""
    from . import cloud_sync, source_watch
    root = source_watch.source_root()
    if not root:
        return
    import json as _json
    import time as _time
    topics = [{"topic": t["topic"], "videos": t["videos"],
               "images": t["images"], "ready": t["ready"]}
              for t in source_watch.list_topics(root)]
    data = _json.dumps({"updated": int(_time.time()), "topics": topics,
                        "pipeline_url": _pipeline_url()},
                       ensure_ascii=False).encode("utf-8")
    cloud_sync._bucket().upload(
        "state/topics.json", data,
        {"content-type": "application/json; charset=utf-8", "upsert": "true"})
