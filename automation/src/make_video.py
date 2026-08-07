"""
[미디어 2] 사진 여러 장 → 짧은 홍보 영상 자동 제작 (ffmpeg).

media/enhanced/ (없으면 inbox/)의 사진들로 페이드 전환 슬라이드 영상을 만듭니다.
배경음악: media/music.mp3 를 넣어두면 자동으로 입힙니다(없으면 무음).
※ 음악은 저작권 무료 음원만 사용하세요 (유튜브 오디오 라이브러리 등).

    python src/make_video.py               # 최근 사진 최대 6장으로 15초 내외 영상
    python src/make_video.py a.jpg b.jpg   # 지정한 사진들로 제작

ffmpeg 설치(윈도우, 한 번만):  winget install Gyan.FFmpeg   (설치 후 터미널 재시작)
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENHANCED = ROOT / "media" / "enhanced"
INBOX = ROOT / "media" / "inbox"
OUT_DIR = ROOT / "media" / "video"
MUSIC = ROOT / "media" / "music.mp3"

EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def load_video_cfg() -> dict:
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        return (cfg.get("media", {}) or {}).get("video", {}) or {}
    except Exception:
        return {}


def pick_photos(args: list[str], limit: int) -> list[pathlib.Path]:
    if args:
        return [pathlib.Path(a) if pathlib.Path(a).is_absolute() else ROOT / a for a in args]
    src = ENHANCED if ENHANCED.exists() and any(ENHANCED.iterdir()) else INBOX
    files = sorted(
        (p for p in src.iterdir() if p.suffix.lower() in EXTS),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    return list(reversed(files[:limit]))  # 최근 사진들을 시간순으로


def build_video(photos: list[pathlib.Path], out: pathlib.Path,
                size: str, per_sec: float) -> bool:
    w, h = size.split("x")
    n = len(photos)
    fade = 0.4

    cmd = ["ffmpeg", "-y"]
    for p in photos:
        cmd += ["-loop", "1", "-t", str(per_sec), "-i", str(p)]

    has_music = MUSIC.exists()
    if has_music:
        cmd += ["-stream_loop", "-1", "-i", str(MUSIC)]

    # 각 사진: 화면 꽉 차게 크롭 → 페이드 인/아웃 → 이어붙이기
    parts, refs = [], []
    for i in range(n):
        parts.append(
            f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},setsar=1,fps=30,"
            f"fade=t=in:st=0:d={fade},fade=t=out:st={per_sec - fade}:d={fade}[v{i}]"
        )
        refs.append(f"[v{i}]")
    parts.append(f"{''.join(refs)}concat=n={n}:v=1:a=0[outv]")
    cmd += ["-filter_complex", ";".join(parts), "-map", "[outv]"]

    if has_music:
        cmd += ["-map", f"{n}:a", "-shortest", "-c:a", "aac", "-b:a", "128k"]

    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]

    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print("  ✗ ffmpeg 실패:")
        print(res.stderr[-1500:])
        return False
    return True


def main() -> None:
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg 가 없습니다. 윈도우:  winget install Gyan.FFmpeg  후 터미널을 다시 여세요.")

    vcfg = load_video_cfg()
    size = vcfg.get("size", "1080x1080")
    per_sec = float(vcfg.get("per_image_sec", 2.5))
    limit = int(vcfg.get("max_photos", 6))

    photos = pick_photos([a for a in sys.argv[1:] if not a.startswith("-")], limit)
    if len(photos) < 2:
        sys.exit("사진이 2장 이상 필요합니다. 먼저 enhance_photos.py 로 사진을 준비하세요.")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    existing = len(list(OUT_DIR.glob("영상_*.mp4")))
    out = OUT_DIR / f"영상_{existing + 1:03d}.mp4"

    total = per_sec * len(photos)
    print(f"사진 {len(photos)}장 → {total:.0f}초 영상 제작 ({size}"
          + (", 배경음악 포함" if MUSIC.exists() else ", 무음 — media/music.mp3 추가 가능") + ")")
    if build_video(photos, out, size, per_sec):
        print(f"\n완료: {out.relative_to(ROOT)}  → 블로그 글 중간에 넣으면 체류시간에 유리해요")


if __name__ == "__main__":
    main()
