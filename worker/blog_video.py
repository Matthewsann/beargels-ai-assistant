"""찍어둔 긴 영상에서 블로그에 넣을 짧은 구간만 잘라낸다.

왜 이렇게 하나:
    매장에서 찍은 영상은 4K 세로에 수십 초~수백 MB 다. 그대로 블로그에 올리면
    ①업로드가 오래 걸리고 ②본문에서 화면을 세로로 길게 잡아먹어 글 읽는 흐름을
    끊고 ③정작 보여주고 싶은 순간(자르는 순간·크림 바르는 순간)은 영상 중간에
    묻혀 있다. 그래서 **AI가 영상을 실제로 훑어보고 핵심 구간을 골라** 짧게 자른다.

블로그용 규격(글 읽는 흐름을 안 끊는 선택):
    · 길이 8~12초 — 재생 버튼을 누르게 하고, 다 보게 하는 길이
    · 1:1 정사각 1080 — 본문에서 세로로 덜 길어 스크롤이 안 끊긴다
    · 소리 유지 — 자르는 소리·바르는 소리가 이 영상의 핵심이다
    · faststart — 재생 버튼 누르자마자 시작

    py worker/blog_video.py "원본.MOV"           한 개 잘라보기
    py worker/blog_video.py "원본.MOV" --plan    어디를 자를지만 보기(인코딩 X)
"""
from __future__ import annotations

import io
import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

logger = logging.getLogger(__name__)

MIN_SEC = float(os.getenv("BLOG_CLIP_MIN_SEC", "8"))
MAX_SEC = float(os.getenv("BLOG_CLIP_MAX_SEC", "12"))
# 이보다 짧으면 블로그에 넣어도 재생 버튼만 깜빡이다 끝난다 → 아예 안 만든다.
# (원본 자체가 1초짜리인 경우가 있다 — 찍다 만 것)
DROP_UNDER_SEC = float(os.getenv("BLOG_CLIP_DROP_SEC", "3"))
# 본문 영상 규격. "1:1" 정사각 · "9:16" 세로 · "16:9" 가로 · "원본"
ASPECT = os.getenv("BLOG_CLIP_ASPECT", "1:1")
SIZE = int(os.getenv("BLOG_CLIP_SIZE", "1080"))

# 훑어볼 때 몇 초 간격으로 장면을 볼지 / 최대 몇 장까지 볼지
SAMPLE_EVERY = 1.5
SAMPLE_MAX = 14


def ffmpeg_exe() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def duration(path: str | pathlib.Path) -> float:
    """영상 길이(초). ffprobe 없이 ffmpeg 출력에서 읽는다."""
    r = _run([ffmpeg_exe(), "-hide_banner", "-i", str(path)])
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", r.stderr or "")
    if not m:
        raise RuntimeError(f"영상 길이를 읽지 못했습니다: {pathlib.Path(path).name}")
    h, mnt, s = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(s)


def _crop_filter() -> str:
    """규격에 맞춰 가운데를 기준으로 꽉 채워 자르는 필터."""
    if ASPECT == "원본":
        return f"scale={SIZE}:-2"
    w, h = (int(x) for x in ASPECT.split(":"))
    tw = SIZE
    th = int(round(SIZE * h / w / 2) * 2)          # 짝수여야 인코딩된다
    return (f"scale={tw}:{th}:force_original_aspect_ratio=increase,"
            f"crop={tw}:{th},setsar=1")


# ---------------------------------------------------------------------------
# 어디를 자를지 — AI 가 장면을 훑어본다
# ---------------------------------------------------------------------------

def sample_frames(path: str | pathlib.Path, out_dir: pathlib.Path) -> list[tuple[float, pathlib.Path]]:
    """일정 간격으로 장면 사진을 뽑는다. [(초, 사진경로), ...]"""
    total = duration(path)
    every = max(SAMPLE_EVERY, total / SAMPLE_MAX)
    out_dir.mkdir(parents=True, exist_ok=True)
    shots = []
    t = 0.0
    i = 0
    while t < total - 0.2 and i < SAMPLE_MAX:
        dest = out_dir / f"f{i:02d}.jpg"
        r = _run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
                  "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                  "-vf", "scale=512:-2", str(dest)])
        if dest.exists() and dest.stat().st_size > 0:
            shots.append((t, dest))
        elif r.returncode != 0:
            logger.debug("장면 추출 실패 %.1fs: %s", t, (r.stderr or "")[:120])
        t += every
        i += 1
    if not shots:
        raise RuntimeError("영상에서 장면을 하나도 뽑지 못했습니다.")
    return shots


PLAN_PROMPT = """너는 베어글스(인천 송도 베이글 카페)의 네이버 블로그 담당자다.
아래는 매장에서 찍은 영상 한 개를 시간 순서대로 뽑아본 장면들이다.
장면 번호와 그 장면의 시각(초)은 이렇다:
{timeline}

이 영상에서 **블로그 본문에 넣을 {minsec}~{maxsec}초 구간 하나**를 골라라.

고르는 기준(중요한 순서대로):
1. 먹고 싶어지는 결정적 순간이 들어갈 것 — 자르는 순간, 단면이 드러나는 순간,
   크림·소스를 바르는 순간, 김이 나는 순간.
2. 그 결정적 순간이 **구간의 앞쪽 1~2초 안에** 오게 시작점을 잡을 것.
   블로그에서도 앞부분에서 흥미가 없으면 바로 넘긴다.
3. 흔들리거나 초점이 안 맞는 구간, 손만 왔다 갔다 하는 구간은 피할 것.
4. 장면이 갑자기 끊기지 않게 끝낼 것.

지어내지 마라. 안 보이면 본 대로만 적어라.
{avoid}
JSON 하나만 출력(설명·코드블록 금지):
{{"start": 0.0, "end": 10.0,
  "subject": "이 영상에 찍힌 것 한 줄",
  "caption": "블로그에서 영상 밑에 달 담백한 한 줄(과장·이모지 금지)",
  "keywords": ["낱말", "3~6개"],
  "why": "왜 이 구간인지 한 줄",
  "usable": true}}
쓸 만한 구간이 없으면 usable 을 false 로."""


def _extract_obj(text: str) -> dict:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    else:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            text = m.group(0)
    return json.loads(text)


def plan_clip(path: str | pathlib.Path,
              avoid: list[tuple[float, float]] | None = None) -> dict:
    """영상을 훑어보고 자를 구간을 정한다.

    avoid: 이미 다른 글에 쓴 구간들. 같은 원본이라도 **다른 구간**으로 편집하면
    재사용해도 된다(사장님 확정 2026-08-28) — 그래서 겹치지만 않게 고른다.
    """
    import llm
    avoid_txt = ""
    if avoid:
        spans = ", ".join(f"{a:.0f}~{b:.0f}초" for a, b in avoid)
        avoid_txt = (f"\n★ 이 원본의 {spans} 구간은 이미 다른 글에 썼다."
                     f" **그 구간과 겹치지 않는 다른 순간**을 골라라."
                     f" 피할 수 없으면 usable 을 false 로.\n")
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="blogvid_"))
    try:
        shots = sample_frames(path, tmp)
        timeline = "\n".join(f"- 장면 {i + 1}: {t:.1f}초" for i, (t, _) in enumerate(shots))
        raw = llm.see([p for _, p in shots],
                      user=PLAN_PROMPT.format(timeline=timeline, avoid=avoid_txt,
                                              minsec=int(MIN_SEC), maxsec=int(MAX_SEC)),
                      max_tokens=700, prefer="gemini")
        plan = _extract_obj(raw)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    total = duration(path)
    start = max(0.0, float(plan.get("start") or 0))
    end = float(plan.get("end") or (start + MAX_SEC))
    # AI 가 준 구간을 상식선으로 다듬는다(너무 짧거나 영상 밖으로 나가지 않게)
    end = min(end, total)
    if end - start < MIN_SEC:
        end = min(total, start + MIN_SEC)
    if end - start > MAX_SEC:
        end = start + MAX_SEC
    if end > total:                       # 원본이 최소 길이보다 짧은 경우
        start, end = 0.0, total
    plan["start"], plan["end"] = round(start, 2), round(end, 2)
    plan.setdefault("usable", True)
    return plan


# ---------------------------------------------------------------------------
# 실제로 자르기
# ---------------------------------------------------------------------------

def make_clip(src: str | pathlib.Path, out: str | pathlib.Path,
              start: float, end: float) -> pathlib.Path:
    """구간을 잘라 블로그용 mp4 로 인코딩한다(소리 유지)."""
    out = pathlib.Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # ⚠️ -ss / -t 는 반드시 -i **앞**에 둔다. 뒤에 두면 길이가 어긋난다
    #    (릴스 편집기에서 실제로 당한 함정 — 2026-08-18).
    args = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}", "-i", str(src),
            "-vf", _crop_filter(),
            "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", str(out)]
    r = _run(args)
    if r.returncode != 0 or not out.exists():
        raise RuntimeError(f"영상 자르기 실패: {(r.stderr or '')[:300]}")
    return out


def build(src: str | pathlib.Path, out_dir: str | pathlib.Path,
          name: str | None = None,
          avoid: list[tuple[float, float]] | None = None) -> dict | None:
    """영상 1개 → 블로그용 클립 1개. 쓸 구간이 없으면 None.

    돌려주는 값: {path, start, end, subject, caption, keywords, why, size}
    """
    src = pathlib.Path(src)
    plan = plan_clip(src, avoid=avoid)
    if not plan.get("usable", True):
        logger.info("쓸 만한 구간 없음: %s", src.name)
        return None
    if plan["end"] - plan["start"] < DROP_UNDER_SEC:
        logger.info("너무 짧아 건너뜀(%.1f초): %s", plan["end"] - plan["start"], src.name)
        return None
    out = pathlib.Path(out_dir) / f"{name or src.stem}.mp4"
    make_clip(src, out, plan["start"], plan["end"])
    return {**plan, "path": str(out), "size": out.stat().st_size,
            "source": src.name}


def main() -> None:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit("사용법: py worker/blog_video.py \"원본영상.MOV\" [--plan]")
    src = pathlib.Path(args[0])
    print(f"원본: {src.name} ({duration(src):.1f}초)")
    plan = plan_clip(src)
    print(f"  고른 구간: {plan['start']}~{plan['end']}초 "
          f"({plan['end'] - plan['start']:.1f}초)")
    print(f"  내용: {plan.get('subject', '')}")
    print(f"  캡션: {plan.get('caption', '')}")
    print(f"  이유: {plan.get('why', '')}")
    if "--plan" in sys.argv:
        return
    out = ROOT / "data" / "blog_clips" / f"{src.stem}.mp4"
    make_clip(src, out, plan["start"], plan["end"])
    print(f"\n저장: {out}  ({out.stat().st_size / 1_000_000:.1f}MB)")


if __name__ == "__main__":
    main()
