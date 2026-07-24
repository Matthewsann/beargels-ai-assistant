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


def _has_audio(ff: str, path: str) -> bool:
    """영상 파일에 소리 트랙이 있는지 확인."""
    info = subprocess.run(
        [ff, "-i", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ).stderr.decode("utf-8", "replace")
    return "Audio:" in info


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
    music_volume: float = 0.35,
    keep_audio: bool = True,
    font_path: str | None = None,
) -> str:
    """클립들을 릴스 mp4로 편집해 output_path에 저장하고 그 경로를 반환.

    hook: 첫 2.5초 상단 문구 / menu: 하단 상시 문구 /
    cta: 마지막 무렵 하단 문구(menu 없을 때) / watermark: 우하단 상시 로고.
    keep_audio: 원본 영상 소리(먹방 ASMR 등) 유지. music_path가 있으면
    배경음악을 music_volume(0~1)로 낮춰 원본 소리와 믹스한다.
    keep_audio=False + music_path 있으면 음악만, 둘 다 없으면 무음.
    """
    if not clip_paths:
        raise ValueError("클립이 없습니다.")
    ff = ffmpeg_exe()
    font = font_path or find_korean_font()

    with tempfile.TemporaryDirectory() as tmp:
        # ── 1단계: 각 클립을 개별적으로 9:16 정규화 ──
        # 폰 영상은 "가로로 저장 + 회전 플래그"인 경우가 많다. filter_complex로
        # 여러 입력을 한 번에 처리하면 이 회전 보정이 적용되지 않아 영상이 눕는다.
        # → 클립마다 단일 -i 로 처리하면 회전 자동보정이 적용되고,
        #   출력에서 회전 메타데이터를 제거해 이후 단계로 새어나가지 않게 한다.
        # 소리 트랙도 유지한다(원본 ASMR). 소리 없는 클립은 무음 트랙을 넣어
        # 이어붙일 때 트랙 개수를 맞춘다.
        n = len(clip_paths)
        norm_paths: list[str] = []
        for i, p in enumerate(clip_paths):
            np_ = os.path.join(tmp, f"norm{i}.mp4")
            has_a = _has_audio(ff, p)
            cmd = [ff, "-y", "-i", p]
            if not has_a:
                cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            cmd += [
                "-vf", _NORMALIZE,
                "-map", "0:v:0",
                "-map", ("1:a" if not has_a else "0:a:0"),
                "-c:v", "libx264", "-preset", "veryfast",
                "-c:a", "aac", "-ar", "48000", "-ac", "2",
                "-map_metadata", "-1", "-metadata:s:v:0", "rotate=0",
            ]
            if not has_a:
                cmd += ["-shortest"]
            cmd.append(np_)
            _run(cmd)
            norm_paths.append(np_)

        # ── 1-b단계: 정규화된(업라이트) 클립 이어붙이기 (영상+소리) ──
        concat = os.path.join(tmp, "concat.mp4")
        inputs: list[str] = []
        for np_ in norm_paths:
            inputs += ["-i", np_]
        chain = "".join(f"[{i}:v][{i}:a]" for i in range(n))
        _run([ff, "-y", *inputs, "-filter_complex",
              f"{chain}concat=n={n}:v=1:a=1[v][a]",
              "-map", "[v]", "-map", "[a]", "-c:a", "aac",
              "-map_metadata", "-1", concat])

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

        # ── 3단계: overlay(자막) 합성 + 소리(원본/음악) + 길이 ──
        # 입력 순서: 0=concat(영상+원본소리), 1..=자막 png, 마지막=배경음악(있으면)
        cmd = [ff, "-y", "-i", concat]
        for ov in overlays:
            cmd += ["-i", ov["path"]]
        music_idx = None
        if music_path:
            music_idx = 1 + len(overlays)
            cmd += ["-stream_loop", "-1", "-i", music_path]

        # 영상: 자막 overlay 체인
        vfilters: list[str] = []
        prev = "[0:v]"
        for i, ov in enumerate(overlays):
            idx = 1 + i
            out = f"[t{i}]"
            en = f":enable='{ov['enable']}'" if ov["enable"] else ""
            vfilters.append(f"{prev}[{idx}:v]overlay={ov['x']}:{ov['y']}{en}{out}")
            prev = out

        # 소리: 원본 + 음악 조합
        afilters: list[str] = []
        aout: str | None = None
        if keep_audio and music_idx is not None:
            afilters.append(f"[{music_idx}:a]volume={music_volume}[am]")
            afilters.append("[0:a][am]amix=inputs=2:duration=first:dropout_transition=0[aout]")
            aout = "[aout]"
        elif music_idx is not None:
            afilters.append(f"[{music_idx}:a]volume=0.6[aout]")
            aout = "[aout]"

        parts = vfilters + afilters
        if parts:
            cmd += ["-filter_complex", ";".join(parts)]
        cmd += ["-map", prev] if vfilters else ["-map", "0:v"]
        if aout:
            cmd += ["-map", aout, "-c:a", "aac"]
        elif keep_audio:
            cmd += ["-map", "0:a?", "-c:a", "aac"]  # 원본 소리 그대로

        if music_idx is not None:
            cmd += ["-shortest"]
        if target_seconds:
            cmd += ["-t", str(target_seconds)]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-map_metadata", "-1", "-movflags", "+faststart", output_path]
        _run(cmd)

    logger.info("릴스 생성 완료: %s", output_path)
    return output_path
