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
import re
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


def _finish_bookkeeping(p: dict, plan: dict | None) -> None:
    """완성본이 나온 뒤의 장부 정리 — 소재 사용 원장 + 콘텐츠 브리프.

    원장(worker/media_ledger)에 "인스타가 이 소재의 이 구간을 썼다"를 남겨야
    블로그가 같은 컷을 또 쓰지 않는다. 로컬 웹 finalize 는 예전부터 이걸
    했는데 원버튼 경로(PA)에는 빠져 있었다(설계 검토 2026-09-04).
    실패해도 완성 저장을 막지 않는다 — 장부는 부가 기록이다.
    """
    from . import webapp as wa
    shots = (plan or {}).get("shots") or []
    used = [{"name": s["clip"], "in": s.get("in"), "dur": s.get("dur")}
            for s in shots if isinstance(s, dict) and s.get("clip")]
    if used:
        p["used_media"] = used
    try:
        wa._record_usage_to_ledger(p)
    except Exception as e:  # noqa: BLE001
        logger.warning("사용 원장 기록 실패: %s", str(e)[:120])
    try:
        from . import briefs
        b = briefs.by_project(p["id"]) or briefs.by_folder(p.get("source_dir") or "")
        if b:
            briefs.patch(b["id"], insta={"project_id": p["id"],
                                         "hook": (plan or {}).get("hook", {}).get("text")})
            briefs.set_status(b["id"], briefs.MAKING)
            p["brief_id"] = b["id"]
    except Exception as e:  # noqa: BLE001
        logger.warning("브리프 연결 실패: %s", str(e)[:120])


def _brief_guide(folder: str) -> str:
    """이 폴더에 걸린 브리프의 촬영 의도(훅·샷)를 편집기가 읽을 글로.

    아이디어 카드의 샷 목록이 편집으로 전달되지 않던 구멍을 메운다
    (설계 검토 2026-09-04). 촬영가이드.txt 가 이미 같은 내용을 담고 있으면
    source_watch.guide_text 로 들어오므로 여기서는 빈 문자열을 돌려준다.
    """
    try:
        from . import briefs, source_watch
        b = briefs.by_folder(folder)
        if not b:
            return ""
        if source_watch.guide_text(folder).strip():
            return ""                      # 폴더에 가이드 파일이 이미 있다
        insta = b.get("insta") or {}
        lines = [f"[기획 의도] {b.get('topic', '')}"]
        if b.get("why"):
            lines.append(b["why"])
        if insta.get("hook_angle"):
            lines.append(f"훅 각도: {insta['hook_angle']}")
        for s in insta.get("shots") or []:
            lines.append(f"- {s.get('what', '')} ({s.get('secs', 0)}초)")
        return "\n".join(lines)[:2000]
    except Exception as e:  # noqa: BLE001
        logger.debug("브리프 가이드 없음: %s", e)
        return ""


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
        p = wa._new_project(
            name, menu=name,
            # 근거의 우선순위: 사람이 적은 메모 → 폴더의 촬영가이드 → 브리프의 기획 의도
            guide=memo or source_watch.guide_text(folder) or _brief_guide(folder),
            source_dir=folder)
        p["status"] = wa.ST_UPLOADED
    if memo:
        p["guide"] = memo.strip()[:2000]
    elif not (p.get("guide") or "").strip():
        p["guide"] = source_watch.guide_text(folder) or _brief_guide(folder)
    wa._save_project(p)
    return p


def _frames_for(p: dict, plan: dict, max_px: int = 480) -> list[tuple[str, bytes]]:
    """샷 중간 지점의 프레임들 — AI 자막·검수 썸네일의 눈.

    ⚠️ 샷 순서와 **자리를 맞춘다**: 뽑기에 실패한 샷도 (mime, b"") 로 자리를
    남긴다 — 건너뛰면 몇 번째 샷의 화면인지 어긋난다(썸네일이 밀림).
    AI 에 보낼 때는 빈 항목을 걸러 쓸 것.
    """
    from . import video_editor
    from . import webapp as wa
    ff = video_editor.ffmpeg_exe()
    folder = wa._media_dir(p)
    frames: list[tuple[str, bytes]] = []
    for s in plan.get("shots") or []:
        data = b""
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
        except (subprocess.SubprocessError, OSError):
            pass
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        frames.append(("image/jpeg", data))
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


def make_script(topic: str, memo: str = "") -> dict:
    """1단계 — 대본까지만 만든다(영상 없음). 사람 검수 게이트 앞까지.

    사장님 지적(2026-09-02): 메모가 틀리면(산딸기 vs 자몽) AI 는 그대로 쓴다.
    그래서 영상을 만들기 **전에** 훅·샷별 자막·CTA·캡션을 웹 검수함에 올리고,
    사장님이 고쳐서 승인하면 2단계(make_video)가 그 대본대로 렌더한다.
    """
    from . import cloud_sync, planner
    from . import webapp as wa

    folder = _find_folder(topic)
    p = _project_for(folder, memo)
    pid, title = p["id"], p.get("title", topic)
    guide = p.get("guide", "")

    files = wa._raw_files(pid, p)
    if not any(f["kind"] == "video" for f in files):
        raise MakeError(f"'{title}' 폴더에 영상이 없어요. 릴스는 영상이 필요해요.")

    # 구성표 — 저장본 존중, 없으면 AI 장면 선택, 그것도 안 되면 뼈대
    plan, ai_selected = None, False
    if not p.get("shot_plan"):
        try:
            plan = footage_plan(p, files)
            ai_selected = True
        except Exception as e:
            logger.warning("AI 장면 선택 실패(뼈대로 진행): %s", e)
    if plan is None:
        plan = wa._ensure_plan(p, files)
    if not plan:
        raise MakeError("영상 길이를 읽지 못해 구성표를 만들 수 없었어요.")

    frames = _frames_for(p, plan)       # 샷과 자리 맞춤(실패 샷은 빈 항목)
    valid = [f for f in frames if f[1]]
    if not ai_selected:                 # 뼈대 구성이면 말이라도 AI 가 쓴다
        try:
            plan = asyncio.run(planner.write_shot_captions(
                plan, title, p.get("menu", title), valid, guide=guide))
        except Exception as e:
            logger.warning("AI 자막 실패(뼈대 자막 유지): %s", e)

    caption = ""
    try:
        gen = wa._get_caption_gen()
        if gen and valid:
            r = asyncio.run(gen.generate(
                images=[valid[0][1]], topic=title, is_reel=True,
                media_count=sum(1 for f in files if f["kind"] == "video"),
                note=guide))
            caption = r.full_text
            p["caption"], p["hashtags"] = r.caption, r.hashtags
    except Exception as e:
        logger.warning("AI 캡션 실패(템플릿 폴백): %s", e)
    if not caption:
        fb = wa._fallback_caption(p)
        caption = fb["caption"] + "\n\n" + " ".join(fb["hashtags"])

    # 검수 화면에 보여줄 샷 썸네일 — 자막만 보고는 재료를 판단할 수 없다
    thumbs: list[str] = []
    try:
        thumbs = cloud_sync.push_script_thumbs(pid, [b for _m, b in frames])
    except Exception as e:
        logger.warning("썸네일 업로드 실패(텍스트만 검수): %s", e)

    p["shot_plan"] = plan
    p["script_caption"] = caption
    wa._save_project(p)

    import time as _time
    entry = {
        "pid": pid, "title": title, "memo": guide,
        "hook": (plan.get("hook") or {}).get("text", ""),
        "cta": (plan.get("cta") or {}).get("text", ""),
        "shots": [{"role": s.get("role", ""), "dur": s.get("dur", 0),
                   "caption": s.get("caption") or "",
                   "thumb": thumbs[i] if i < len(thumbs) else ""}
                  for i, s in enumerate(plan.get("shots") or [])],
        "caption": caption,
        "missing": [m.get("need", "") for m in (plan.get("missing") or [])][:3],
        "created": int(_time.time()),
    }
    cloud_sync.push_script(entry)
    return {"title": title, "pid": pid, "shots": len(entry["shots"]),
            "ai_selected": ai_selected,
            "missing_shots": entry["missing"]}


def make_video(pid: str, script: dict | None = None) -> dict:
    """2단계 — 사람이 검수한 대본대로 영상을 만든다.

    script: 웹에서 고친 {hook, cta, captions[], caption}. 사람이 승인한
    문구이므로 검수는 **경고만** 하고 자동 재작성으로 덮어쓰지 않는다.
    """
    from . import cloud_sync, planner, qc, shot_plan, video_editor
    from . import webapp as wa

    p = wa._load_project(pid)
    if not p:
        raise MakeError("대본의 프로젝트를 찾지 못했어요. 대본을 다시 만들어주세요.")
    plan = p.get("shot_plan")
    if not plan:
        raise MakeError("저장된 구성표가 없어요. 대본을 다시 만들어주세요.")
    title = p.get("title", "")
    caption = p.get("script_caption", "")

    if script:                          # 사람이 고친 문구를 구성표에 반영
        if script.get("hook") is not None:
            plan.setdefault("hook", {})["text"] = str(script["hook"]).strip()
        if script.get("cta") is not None:
            plan.setdefault("cta", {})["text"] = str(script["cta"]).strip()
        caps = script.get("captions")
        if isinstance(caps, list):
            for i, c in enumerate(caps[:len(plan.get("shots") or [])]):
                plan["shots"][i]["caption"] = (str(c).strip() or None) if c is not None else None
        if script.get("caption") is not None:
            caption = str(script["caption"]).strip()
    plan = shot_plan.normalize(plan)
    p["shot_plan"], p["script_caption"] = plan, caption
    wa._save_project(p)

    out = os.path.join(wa._proj_dir(pid), "reel.mp4")
    res = video_editor.build_reel_from_plan(plan, wa._media_dir(p), out)
    p["status"] = wa.ST_EDITED
    wa._save_project(p)

    # 검수 — 승인된 대본은 고치지 않는다. 경고만 남긴다(사람이 권위자).
    report = qc.run_qc(plan, out, caption, p.get("guide", ""))
    p["qc"] = {**report, "fixed": False, "approved": True,
               "missing": plan.get("missing") or []}
    wa._save_project(p)

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
    # 다시 만든 판은 아직 안 올린 것 — 이전 발행 기록은 history 로 내리고
    # 카드엔 [올렸어요]가 다시 뜬다(publish_sync 의 '새 판' 규칙).
    from . import publish_sync as _ps
    _ps.start_new_version(p)
    p["status"] = wa.ST_DONE
    p["final_path"] = final_dir
    _finish_bookkeeping(p, plan)          # 사용 원장 + 콘텐츠 브리프
    wa._save_project(p)
    planner.record_hook(pid, title, (plan.get("hook") or {}).get("text", ""), reel_name)

    cloud = None
    try:
        entry = cloud_sync.push_reel(
            pid, title, os.path.join(final_dir, reel_name), caption,
            script={"hook": (plan.get("hook") or {}).get("text", ""),
                    "cta": (plan.get("cta") or {}).get("text", ""),
                    "captions": [s.get("caption") or ""
                                 for s in plan.get("shots") or []]})
        cloud = entry.get("video")
    except Exception as e:
        logger.warning("클라우드 업로드 실패(로컬 저장은 완료): %s", e)
    try:
        cloud_sync.remove_script(pid)   # 검수함 정리
    except Exception:
        pass
    return {"title": title, "seconds": res.get("seconds"), "file": reel_name,
            "cloud": bool(cloud),
            "qc_passed": report["passed"],
            "qc_warnings": (report["critical"] + report["warnings"])[:3]}


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
    frames = [f for f in _frames_for(p, plan) if f[1]]
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

    # ④-b 출하 전 검수 — 치명 불량이면 말을 한 번 고쳐 재렌더(두 번은 안 함)
    from . import qc
    report = qc.run_qc(plan, out, caption, guide)
    qc_fixed = False
    if not report["passed"] and report["fix_words"]:
        try:
            feedback = (guide + "\n[검수 지적 — 반드시 고칠 것]\n"
                        + "\n".join(f"- {i}" for i in report["critical"]))
            plan = asyncio.run(planner.write_shot_captions(
                plan, title, p.get("menu", title), frames, guide=feedback))
            res = video_editor.build_reel_from_plan(plan, wa._media_dir(p), out)
            p["shot_plan"] = plan
            wa._save_project(p)
            report2 = {"passed": not qc.deterministic_issues(plan, caption),
                       "critical": qc.deterministic_issues(plan, caption),
                       "warnings": report["warnings"], "fix_words": False}
            report, qc_fixed = report2, True
        except Exception as e:
            logger.warning("검수 재작업 실패(경고 출하): %s", e)
    p["qc"] = {**report, "fixed": qc_fixed,
               "missing": plan.get("missing") or [],
               "rejected": plan.get("rejected") or []}
    wa._save_project(p)

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
    # 다시 만든 판은 아직 안 올린 것 — 이전 발행 기록은 history 로 내리고
    # 카드엔 [올렸어요]가 다시 뜬다(publish_sync 의 '새 판' 규칙).
    from . import publish_sync as _ps
    _ps.start_new_version(p)
    p["status"] = wa.ST_DONE
    p["final_path"] = final_dir
    _finish_bookkeeping(p, plan)          # 사용 원장 + 콘텐츠 브리프
    wa._save_project(p)
    planner.record_hook(pid, title, (plan.get("hook") or {}).get("text", ""), reel_name)

    # ⑥ 클라우드 업로드 → 직원 웹 ② 목록
    cloud = None
    try:
        entry = cloud_sync.push_reel(
            pid, title, os.path.join(final_dir, reel_name), caption,
            script={"hook": (plan.get("hook") or {}).get("text", ""),
                    "cta": (plan.get("cta") or {}).get("text", ""),
                    "captions": [s.get("caption") or ""
                                 for s in plan.get("shots") or []]})
        cloud = entry.get("video")
    except Exception as e:
        logger.warning("클라우드 업로드 실패(로컬 저장은 완료): %s", e)

    return {"title": title, "seconds": res.get("seconds"),
            "ai_captions": ai_captions, "ai_selected": ai_selected,
            "cloud": bool(cloud), "file": reel_name,
            "qc_passed": report["passed"], "qc_fixed": qc_fixed,
            "qc_warnings": report["warnings"][:3],
            "missing_shots": [m.get("need", "") for m in (plan.get("missing") or [])][:3]}


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


def _briefs_from_ideas(ideas: list[dict], source: str) -> list[dict]:
    """아이디어 카드 → 콘텐츠 브리프(주제 1개 + 채널별 지시). 실패해도 카드는 뜬다."""
    made = []
    try:
        from . import briefs
        for i in ideas:
            title = (i.get("title") or "").strip()
            if not title or briefs.by_folder(title):
                continue                      # 같은 주제가 이미 진행 중이면 새로 만들지 않는다
            b = briefs.create(
                title, why=i.get("why", ""), source=source,
                insta={"hook_angle": i.get("hook_angle", ""), "shots": i.get("shots") or []},
                blog={"keyword": (i.get("blog_keyword") or "").strip(),
                      "angle": (i.get("blog_angle") or "").strip()})
            i["brief_id"] = b["id"]
            made.append(b)
        if made:
            briefs.push()
    except Exception as e:  # noqa: BLE001 — 브리프가 없어도 촬영 제안은 볼 수 있어야 한다
        logger.warning("브리프 생성 실패: %s", str(e)[:150])
    return made


def run_ideas() -> int:
    """MKT 파트너의 '먼저 제안' — 촬영 아이디어를 만들어 올린다."""
    from . import cloud_sync, planner
    recent = [e.get("title", "") for e in cloud_sync.load_index()[:8]]
    ideas = asyncio.run(planner.suggest_shoots(recent))
    if not ideas:
        raise MakeError("아이디어를 만들지 못했어요.")
    _briefs_from_ideas(ideas, "weekly")       # 브리프 id 를 카드에 심고 나서 올린다
    cloud_sync.push_ideas(ideas, source="weekly")
    return len(ideas)


#: 촬영가이드 파일 이름 — source_watch.guide_text 가 이 이름을 읽는다.
GUIDE_NAME = "촬영가이드.txt"


def start_shoot(brief_id: str = "", title: str = "") -> dict:
    """[📸 이거 찍을게요] — 폴더와 촬영가이드를 미리 만들어 둔다.

    지금까지는 사장님이 카드를 보고 **드라이브에서 직접 폴더를 만들고 제목을
    똑같이 타이핑**해야 연결됐다. 사람 결정 세 번이 촬영 시작을 막던 지점이라
    (설계 검토 2026-09-04) 시스템이 대신 만든다 — 사장님은 찍어 넣기만.

    반환: {folder, path, guide, brief_id, created}
    """
    from . import briefs, source_watch
    b = briefs.get(brief_id) if brief_id else None
    if b is None and title:
        b = briefs.by_folder(title)
    if b is None:
        # 브리프가 없으면(옛 카드) 제목만으로 만든다 — 흐름이 끊기지 않게.
        if not title:
            raise MakeError("어느 아이디어인지 알 수 없어요.")
        b = briefs.create(title, source="manual")
    name = (b.get("folder") or b.get("topic") or title).strip()
    root = source_watch.source_root()
    if not root:
        raise MakeError("집 PC에서 소재 폴더(원본소재)를 찾지 못했어요.")
    safe = re.sub(r'[\\/:*?"<>|]', " ", name).strip()[:60] or "새 촬영"
    path = os.path.join(root, safe)
    created = not os.path.isdir(path)
    os.makedirs(path, exist_ok=True)

    insta, blog = b.get("insta") or {}, b.get("blog") or {}
    lines = [f"■ {b.get('topic', safe)}", ""]
    if b.get("why"):
        lines += [f"왜 지금: {b['why']}", ""]
    if insta.get("hook_angle"):
        lines += [f"훅(첫 3초): {insta['hook_angle']}", ""]
    lines.append("[찍을 샷]")
    for n, s in enumerate(insta.get("shots") or [], 1):
        lines.append(f"{n}. {s.get('what', '')} ({s.get('secs', 0)}초)")
    if blog.get("keyword"):
        lines += ["", "[같은 촬영으로 블로그도 씁니다]",
                  f"검색 키워드: {blog['keyword']}",
                  f"글 각도: {blog.get('angle', '')}",
                  "→ 위 샷 외에 '완성 접시 정면 사진' 한 장을 더 찍어주세요(블로그 대표사진)."]
    lines += ["", "[찍고 나서]", "이 폴더에 그대로 올려주세요. 10분 안에 제가 확인하고",
              "못 쓰는 컷이 있으면 알려드릴게요.", "",
              f"(브리프 {b['id']} · 이 파일은 자동으로 만들어졌어요)"]
    guide = "\n".join(lines)
    try:
        with open(os.path.join(path, GUIDE_NAME), "w", encoding="utf-8") as f:
            f.write(guide)
    except OSError as e:
        raise MakeError(f"촬영가이드를 저장하지 못했어요: {e}") from e

    briefs.patch(b["id"], folder=safe)
    briefs.set_status(b["id"], briefs.SHOOTING)
    try:
        briefs.push()
        push_topics()
    except Exception as e:  # noqa: BLE001 — 화면 갱신 실패가 폴더 생성을 무르지 않는다
        logger.warning("촬영 시작 알림 실패: %s", str(e)[:120])
    return {"folder": safe, "path": path, "guide": guide,
            "brief_id": b["id"], "created": created}


def run_intake(folder_name: str = "") -> list[str]:
    """입고 검수 — 소재가 들어온 폴더를 훑어 '못 쓰는 컷'을 폰으로 보낸다.

    folder_name 이 없으면 촬영중·소재도착 상태의 브리프 폴더를 전부 본다.
    반환: 사람이 읽을 요약 문장들.
    """
    from . import briefs, intake_qc, source_watch
    root = source_watch.source_root()
    if not root:
        return ["소재 폴더를 찾지 못했어요."]
    if folder_name:
        targets = [folder_name]
    else:
        targets = [b.get("folder") or b.get("topic") for b in briefs.load()
                   if b.get("status") in (briefs.SHOOTING, briefs.ARRIVED)]
    notes: list[str] = []
    for name in [t for t in targets if t]:
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        media = source_watch.scan_media(path)
        if not (media["videos"] or media["images"]):
            continue                       # 아직 안 찍었다 — 잔소리하지 않는다
        res = intake_qc.check_folder(path)
        line = intake_qc.summary_line(res)
        b = briefs.by_folder(name)
        if b:
            briefs.patch(b["id"], intake={
                "checked_at": res["checked_at"], "files": res["files"],
                "ok": res["ok"], "bad": res["bad"], "missing": res["missing"]})
            if res["ok"]:
                briefs.set_status(b["id"], briefs.ARRIVED)
        notes.append(f"「{name}」 {line}")
    if notes:
        try:
            briefs.push()
        except Exception as e:  # noqa: BLE001
            logger.warning("브리프 업로드 실패: %s", str(e)[:120])
    return notes or ["새로 들어온 소재가 없어요."]


def run_reference(desc: str) -> str:
    """사장님이 가져온 레퍼런스 → 우리 버전 촬영 기획 1개."""
    from . import cloud_sync, planner
    ideas = asyncio.run(planner.plan_from_reference(desc))
    if not ideas:
        raise MakeError("레퍼런스를 기획으로 옮기지 못했어요.")
    _briefs_from_ideas(ideas, "ref")
    cloud_sync.push_ideas(ideas, source="ref")
    return ideas[0].get("title", "")


#: PA 파이프라인 화면에 올리는 최근 프로젝트 수
PIPE_RECENT = 6


def push_pipe_state(refresh_pid: str | None = None) -> None:
    """파이프라인 상태를 PA 웹사이트용으로 클라우드에 올린다.

    refresh_pid 를 주면 그 프로젝트의 썸네일·미리보기까지 새로 올린다
    (렌더·저장 직후). 평소(30분 주기)에는 구성표 JSON 만 가볍게 갱신.
    """
    import time as _time
    from . import cloud_sync
    from . import webapp as wa

    if refresh_pid:
        p = wa._load_project(refresh_pid)
        if p and p.get("shot_plan"):
            try:
                frames = _frames_for(p, p["shot_plan"])
                p["thumb_urls"] = cloud_sync.push_script_thumbs(
                    refresh_pid, [b for _m, b in frames])
            except Exception as e:
                logger.warning("파이프 썸네일 실패: %s", e)
            reel = os.path.join(wa._proj_dir(refresh_pid), "reel.mp4")
            if os.path.exists(reel):
                try:
                    p["preview_url"] = cloud_sync.push_preview(refresh_pid, reel)
                    p["preview_ver"] = int(_time.time())
                except Exception as e:
                    logger.warning("미리보기 업로드 실패: %s", e)
            wa._save_project(p)

    items = []
    if os.path.isdir(wa.PROJECTS_DIR):
        pids = sorted(
            (x for x in os.listdir(wa.PROJECTS_DIR)
             if os.path.exists(os.path.join(wa.PROJECTS_DIR, x, "project.json"))),
            key=lambda x: os.path.getmtime(
                os.path.join(wa.PROJECTS_DIR, x, "project.json")),
            reverse=True)[:PIPE_RECENT]
        for pid in pids:
            p = wa._load_project(pid)
            if not p:
                continue
            plan = p.get("shot_plan") or {}
            thumbs = p.get("thumb_urls") or []
            items.append({
                "pid": pid, "title": p.get("title", pid),
                "status": p.get("status", ""), "memo": p.get("guide", ""),
                "hook": (plan.get("hook") or {}).get("text", ""),
                "cta": (plan.get("cta") or {}).get("text", ""),
                "shots": [{"clip": s.get("clip", ""), "in": s.get("in", 0),
                           "dur": s.get("dur", 0), "role": s.get("role", ""),
                           "caption": s.get("caption") or "",
                           "audio": bool(s.get("audio")),
                           "thumb": thumbs[i] if i < len(thumbs) else ""}
                          for i, s in enumerate(plan.get("shots") or [])],
                "caption": p.get("script_caption", ""),
                "qc": (p.get("qc") or {}).get("warnings") or
                      (p.get("qc") or {}).get("critical") or [],
                "missing": [m.get("need", "") for m in (plan.get("missing") or [])][:3],
                "preview": p.get("preview_url", ""),
                "preview_ver": p.get("preview_ver", 0),
            })
    cloud_sync.push_pipe({"updated": int(_time.time()), "projects": items})


def run_pipe_action(pid: str, action: str, payload: dict | None = None) -> str:
    """PA 파이프라인 화면의 버튼 하나를 집 PC 에서 실행한다. 반환=결과 문장."""
    from . import planner, shot_plan, video_editor
    from . import webapp as wa

    p = wa._load_project(pid)
    if not p:
        raise MakeError("프로젝트를 찾지 못했어요.")
    title = p.get("title", pid)
    payload = payload or {}

    def _apply_edits():
        plan = p.get("shot_plan") or {}
        if payload.get("hook") is not None:
            plan.setdefault("hook", {})["text"] = str(payload["hook"]).strip()
        if payload.get("cta") is not None:
            plan.setdefault("cta", {})["text"] = str(payload["cta"]).strip()
        if isinstance(payload.get("shots"), list):
            plan["shots"] = [
                {"clip": s.get("clip", ""), "in": float(s.get("in") or 0),
                 "dur": float(s.get("dur") or 2.5),
                 "caption": (str(s.get("caption") or "").strip() or None),
                 "role": s.get("role") or "과정",
                 "audio": bool(s.get("audio")),
                 "slow": shot_plan.PAYOFF_SLOW
                         if s.get("role") == shot_plan.ROLE_PAYOFF else 1.0}
                for s in payload["shots"] if s.get("clip")]
        p["shot_plan"] = shot_plan.normalize(plan)
        if payload.get("caption") is not None:
            p["script_caption"] = str(payload["caption"]).strip()
        wa._save_project(p)

    if action == "save":
        _apply_edits()
        push_pipe_state(pid)
        return f"'{title}' 구성표 저장"
    if action == "render":
        _apply_edits()
        out = os.path.join(wa._proj_dir(pid), "reel.mp4")
        res = video_editor.build_reel_from_plan(
            p["shot_plan"], wa._media_dir(p), out)
        p["status"] = wa.ST_EDITED
        wa._save_project(p)
        push_pipe_state(pid)
        return f"'{title}' 다시 편집 완료 ({res.get('seconds')}초) — 미리보기 갱신됨"
    if action == "ai_full":
        files = wa._raw_files(pid, p)
        p["shot_plan"] = footage_plan(p, files)
        wa._save_project(p)
        push_pipe_state(pid)
        return f"'{title}' 장면을 AI 가 새로 골랐어요 — 확인 후 [다시 편집]을 누르세요"
    if action == "ai_words":
        plan = p.get("shot_plan")
        if not plan:
            raise MakeError("구성표가 없어요.")
        frames = [f for f in _frames_for(p, plan) if f[1]]
        p["shot_plan"] = asyncio.run(planner.write_shot_captions(
            plan, title, p.get("menu", title), frames, guide=p.get("guide", "")))
        wa._save_project(p)
        push_pipe_state(pid)
        return f"'{title}' 자막을 새로 썼어요 — 확인 후 [다시 편집]을 누르세요"
    if action == "finalize":
        res = make_video(pid, None)     # 렌더+검수+완성본+목록까지 한 번에
        push_pipe_state(pid)
        note = "검수 통과" if res.get("qc_passed") else "⚠️ 검수 경고 있음"
        return f"'{title}' 완성본 확정 ({res.get('seconds')}초 · {note}) — 완성본 목록에 올라감"
    raise MakeError(f"모르는 동작: {action}")


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
