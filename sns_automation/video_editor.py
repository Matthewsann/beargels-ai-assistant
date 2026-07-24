"""템플릿 기반 릴스 자동 편집기 (ffmpeg + Pillow 오버레이).

원본 클립(들)을 인스타 릴스 규격(9:16, 1080×1920)으로 변환하고,
템플릿에 맞춰 이어붙이기 + 자막/문구 오버레이 + 브랜드 워터마크 + 배경음악을
넣어 mp4로 출력한다.

- ffmpeg는 imageio-ffmpeg 가 번들 제공 → 사용자가 따로 설치할 필요 없음.
- 번들 ffmpeg에는 drawtext 필터가 없어서, 자막은 Pillow로 PNG를 그려
  ffmpeg overlay 필터로 합성한다 (한글 폰트·외곽선 자유롭고 크로스플랫폼).
- 한글 폰트(.ttf)가 필요. Windows는 맑은고딕(malgun.ttf)을 자동 탐색.
"""

import glob
import logging
import os
import subprocess
import tempfile

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

TARGET_W, TARGET_H = 1080, 1920
FPS = 30

_NORMALIZE = (
    f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
    f"crop={TARGET_W}:{TARGET_H},setsar=1,fps={FPS},format=yuv420p"
)


def ffmpeg_exe() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def find_korean_font() -> str | None:
    """한글 자막용 폰트 경로 (환경변수 > Windows 맑은고딕 > 시스템 탐색)."""
    override = os.getenv("REEL_FONT_PATH")
    if override and os.path.exists(override):
        return override
    candidates = [
        r"C:\Windows\Fonts\malgunbd.ttf",  # 맑은고딕 Bold
        r"C:\Windows\Fonts\malgun.ttf",
        "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    for pattern in (
        "/usr/share/fonts/**/*Nanum*Bold*.ttf",
        "/usr/share/fonts/**/*Nanum*.ttf",
        "/usr/share/fonts/**/*malgun*.ttf",
        "/usr/share/fonts/**/*CJK*.ttc",
    ):
        hits = glob.glob(pattern, recursive=True)
        if hits:
            return hits[0]
    return None


def _render_text_png(text: str, out_path: str, font_path: str, font_size: int) -> tuple[int, int]:
    """텍스트를 외곽선 있는 투명 PNG로 그린다. (width, height) 반환."""
    font = ImageFont.truetype(font_path, font_size)
    stroke = max(3, font_size // 12)
    pad = stroke + 12
    # 가로가 너무 길면 화면(1080)에 맞춰 줄바꿈
    max_w = TARGET_W - 80
    words = text.split(" ")
    lines: list[str] = []
    cur = ""
    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    for w in words:
        trial = f"{cur} {w}".strip()
        if dummy.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)

    ascent, descent = font.getmetrics()
    line_h = ascent + descent
    widths = [dummy.textlength(ln, font=font) for ln in lines]
    box_w = int(max(widths)) + pad * 2
    box_h = line_h * len(lines) + pad * 2

    img = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    y = pad
    for ln in lines:
        w = dummy.textlength(ln, font=font)
        x = (box_w - w) / 2
        draw.text(
            (x, y), ln, font=font, fill=(255, 255, 255, 255),
            stroke_width=stroke, stroke_fill=(0, 0, 0, 230),
        )
        y += line_h
    img.save(out_path)
    return box_w, box_h


def _position(pos: str, w: int, h: int) -> tuple[int, int]:
    if pos == "top":
        return (TARGET_W - w) // 2, int(TARGET_H * 0.09)
    if pos == "center":
        return (TARGET_W - w) // 2, (TARGET_H - h) // 2
    if pos == "watermark":
        return TARGET_W - w - 36, TARGET_H - h - 36
    return (TARGET_W - w) // 2, int(TARGET_H * 0.78)  # bottom


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace")[-1500:]
        raise RuntimeError(f"ffmpeg 실패:\n{tail}")


def build_reel(
    clip_paths: list[str],
    output_path: str,
    *,
    target_seconds: int | None = None,
    hook: str | None = None,
    menu: str | None = None,
    cta: str | None = None,
    watermark: str = "베어글스 송도",
    music_path: str | None = None,
    font_path: str | None = None,
) -> str:
    """클립들을 릴스 mp4로 편집해 output_path에 저장하고 그 경로를 반환.

    hook: 첫 2.5초 상단 문구 / menu: 하단 상시 문구 /
    cta: 마지막 무렵 하단 문구(menu 없을 때) / watermark: 우하단 상시 로고.
    """
    if not clip_paths:
        raise ValueError("클립이 없습니다.")
    ff = ffmpeg_exe()
    font = font_path or find_korean_font()

    with tempfile.TemporaryDirectory() as tmp:
        # ── 1단계: 9:16 정규화 + 이어붙이기 (영상만) ──
        concat = os.path.join(tmp, "concat.mp4")
        inputs: list[str] = []
        for p in clip_paths:
            inputs += ["-i", p]
        n = len(clip_paths)
        norm = "".join(f"[{i}:v]{_NORMALIZE}[v{i}];" for i in range(n))
        chain = "".join(f"[v{i}]" for i in range(n))
        _run([ff, "-y", *inputs, "-filter_complex",
              f"{norm}{chain}concat=n={n}:v=1:a=0[v]", "-map", "[v]", "-an", concat])

        # ── 2단계: 오버레이 PNG 준비 (Pillow) ──
        overlays: list[dict] = []  # {path, x, y, enable}
        if font:
            specs = []
            if hook:
                specs.append(("hook", hook, "top", 68, "between(t,0,2.5)"))
            if menu:
                specs.append(("menu", menu, "bottom", 58, None))
            elif cta:
                specs.append(("cta", cta, "bottom", 48, None))
            if watermark:
                specs.append(("wm", watermark, "watermark", 34, None))
            for key, text, pos, size, enable in specs:
                png = os.path.join(tmp, f"{key}.png")
                w, h = _render_text_png(text, png, font, size)
                x, y = _position(pos, w, h)
                overlays.append({"path": png, "x": x, "y": y, "enable": enable})
        else:
            logger.warning("한글 폰트를 못 찾아 자막 없이 편집합니다.")

        # ── 3단계: overlay 합성 + 배경음악 + 길이 ──
        cmd = [ff, "-y", "-i", concat]
        for ov in overlays:
            cmd += ["-i", ov["path"]]
        music_idx = None
        if music_path:
            music_idx = 1 + len(overlays)
            cmd += ["-stream_loop", "-1", "-i", music_path]

        if overlays:
            steps = []
            prev = "[0:v]"
            for i, ov in enumerate(overlays):
                idx = 1 + i
                out = f"[t{i}]"
                en = f":enable='{ov['enable']}'" if ov["enable"] else ""
                steps.append(f"{prev}[{idx}:v]overlay={ov['x']}:{ov['y']}{en}{out}")
                prev = out
            filter_complex = ";".join(steps)
            cmd += ["-filter_complex", filter_complex, "-map", prev]
        else:
            cmd += ["-map", "0:v"]

        if music_idx is not None:
            cmd += ["-map", f"{music_idx}:a", "-c:a", "aac", "-shortest"]
        if target_seconds:
            cmd += ["-t", str(target_seconds)]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", output_path]
        _run(cmd)

    logger.info("릴스 생성 완료: %s", output_path)
    return output_path
