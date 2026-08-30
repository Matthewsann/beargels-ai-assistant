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
import re
import subprocess
import tempfile

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger(__name__)

TARGET_W, TARGET_H = 1080, 1920
FPS = 30

_NORMALIZE = (
    f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
    f"crop={TARGET_W}:{TARGET_H},setsar=1,fps={FPS},format=yuv420p"
)

# ── 베어글스 릴스 룩 (사장님 확정 2026-08-17~18) ──────────────────
# "깔끔하고 트렌디, 그러면서 아기자기하고 귀여운 — 일본 베이커리 카페 느낌"
#
# 두꺼운 흰 글씨 + 검정 외곽선은 **쓰지 않는다**. 감성을 깬다는 사장님 지적.
# 대신 크림색 라운드 칩(알약) 위에 코코아색 손글씨 = 카페 라벨 느낌.
BRAND_FONT = "Cafe24Ssukssuk.ttf"          # 손글씨 — 아기자기함
CHIP_FILL = (255, 251, 243, 238)           # 크림 칩 배경
CHIP_INK = (72, 52, 38, 255)               # 코코아 글씨

# 하이키·저채도·따뜻한 화이트밸런스 (밝고 부드러운 일본 카페 톤)
GRADE = ("eq=brightness=0.035:saturation=0.97:contrast=0.97,"
         "colorbalance=rs=0.03:bs=-0.03")

# 릴스 리서치 반영 (2026-08-18)
HOOK_Y = 0.18        # 맨 위는 인스타 UI에 가림 → 중앙 상단
CAPTION_Y = 0.77     # 하단 UI·캡션 영역 회피
HOOK_SIZE = 70       # 훅은 썸네일에서 읽혀야 한다
CAPTION_SIZE = 44
LABEL_SIZE = 34
CTA_SIZE = 42
TRANSITION = 0.25    # 샷 사이 크로스디졸브


def brand_font_path() -> str | None:
    """브랜드 손글씨 폰트. 없으면 기존 한글 폰트로 폴백."""
    for d in (r"C:\Windows\Fonts",
              os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Microsoft\Windows\Fonts")):
        if not d:
            continue
        p = os.path.join(d, BRAND_FONT)
        if os.path.exists(p):
            return p
    return find_korean_font()


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


def probe_seconds(path: str, ff: str | None = None) -> float:
    """파일 길이(초). 0이면 못 읽은 것.

    ⚠️ xfade 의 offset 은 **계산값이 아니라 이 실측값**으로 잡아야 한다.
    인코딩 결과가 요청한 길이와 미세하게 다르면(특히 setpts 슬로우모션)
    뒤쪽 전환이 통째로 어긋나 마지막 샷이 사라진다(2026-08-17 실제로 당함).
    """
    ff = ff or ffmpeg_exe()
    info = subprocess.run(
        [ff, "-i", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ).stderr.decode("utf-8", "replace")
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", info)
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


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


# ══════════════════════════════════════════════════════════════════
#  샷 구성표 기반 편집 — 기획안을 그대로 실행한다
# ══════════════════════════════════════════════════════════════════

def _chip_png(text: str, size: int, dst: str, font_path: str | None = None) -> tuple[int, int]:
    """크림색 라운드 칩 위에 코코아 글씨. 외곽선 없음. (w, h) 반환.

    긴 말은 최대 2줄까지 나누고, 그래도 넘치면 글씨를 줄인다.
    '\\n' 이 있으면 그 자리에서 끊는다(의미 단위로 끊기 위해).
    """
    font_path = font_path or brand_font_path()
    if not font_path:
        raise RuntimeError("자막용 한글 폰트를 찾지 못했습니다.")
    d0 = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    max_w = TARGET_W - 150

    def wrap(f):
        if "\n" in text:
            return text.split("\n")
        if d0.textlength(text, font=f) <= max_w:
            return [text]
        lines, cur = [], ""
        for w in text.split(" "):
            trial = f"{cur} {w}".strip()
            if d0.textlength(trial, font=f) <= max_w or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        return lines

    while True:
        font = ImageFont.truetype(font_path, size)
        lines = wrap(font)
        if len(lines) <= 2 or size <= 30:
            break
        size -= 2

    asc, desc = font.getmetrics()
    line_h = int((asc + desc) * 1.12)
    tw = max(d0.textlength(ln, font=font) for ln in lines)
    th = line_h * len(lines)
    px, py = int(size * 0.72), int(size * 0.42)
    cw, ch = int(tw) + px * 2, th + py * 2
    m = 26                                        # 그림자 여유
    img = Image.new("RGBA", (cw + m * 2, ch + m * 2), (0, 0, 0, 0))
    box = (m, m, m + cw, m + ch)
    r = min(ch // 2, int(size * 1.1))             # 한 줄=알약, 두 줄=둥근 사각

    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle(
        (box[0], box[1] + 7, box[2], box[3] + 7), radius=r, fill=(60, 45, 32, 70))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(12)))

    d = ImageDraw.Draw(img)
    d.rounded_rectangle(box, radius=r, fill=CHIP_FILL)
    y = m + py
    for ln in lines:
        lw = d0.textlength(ln, font=font)
        d.text((m + px + (tw - lw) / 2, y), ln, font=font, fill=CHIP_INK)
        y += line_h
    img.save(dst)
    return img.size


def build_reel_from_plan(plan: dict, clip_dir: str, output_path: str, *,
                         transition: float = TRANSITION,
                         font_path: str | None = None) -> dict:
    """샷 구성표대로 릴스를 만든다.

    build_reel() 과 달리 **클립 안의 구간만 잘라** 쓰고, 샷마다 다른 자막을 얹으며,
    같은 클립의 서로 다른 순간을 여러 샷으로 쓸 수 있다.

    소리: 기본 무음(발행 때 인기 음원을 얹으므로). 단 `audio: true` 인 샷만
    원본 소리를 남긴다 — 자르는 소리 같은 실제 ASMR이 합성 효과음보다 낫다.

    반환: {"path", "seconds", "shots": [{"start", "dur", ...}]}
    """
    from . import shot_plan as sp

    plan = sp.normalize(plan)
    shots = plan["shots"]
    ff = ffmpeg_exe()
    font = font_path or brand_font_path()

    with tempfile.TemporaryDirectory() as tmp:
        # ── 1) 샷별 구간 컷 + 9:16 정규화 + 색보정 (+슬로우) ──
        norms = []
        for i, s in enumerate(shots):
            src = os.path.join(clip_dir, s["clip"])
            if not os.path.exists(src):
                raise RuntimeError(f"클립을 찾지 못했습니다: {s['clip']}")
            dst = os.path.join(tmp, f"s{i}.mp4")
            slow = s["slow"]
            vf = f"{_NORMALIZE},{GRADE}" + (f",setpts={slow}*PTS" if slow != 1.0 else "")
            # ⚠️ -t 는 반드시 -i **앞**(입력 길이). 뒤에 두면 출력이 잘려
            #    setpts 로 늘린 길이가 사라지고 뒤쪽 xfade offset 이 전부 어긋난다.
            _run([ff, "-y", "-ss", f"{s['in']:.3f}", "-t", f"{s['dur'] / slow:.3f}",
                  "-i", src, "-an", "-vf", vf,
                  "-c:v", "libx264", "-preset", "veryfast",
                  "-map_metadata", "-1", "-metadata:s:v:0", "rotate=0", dst])
            norms.append(dst)

        # ── 2) 실측 길이로 크로스디졸브 체인 ──
        durs = [probe_seconds(p, ff) or shots[i]["dur"] for i, p in enumerate(norms)]
        starts, parts, prev = [0.0], [], "[0:v]"
        acc = durs[0]
        for i in range(1, len(norms)):
            off = max(0.0, acc - transition)
            starts.append(off)
            parts.append(f"{prev}[{i}:v]xfade=transition=fade:"
                         f"duration={transition}:offset={off:.3f}[x{i}]")
            prev = f"[x{i}]"
            acc = acc + durs[i] - transition
        total = round(acc, 2)

        # ── 3) 자막 — 같은 자리에 두 개가 겹치지 않게 시간을 나눈다 ──
        overlays: list[dict] = []

        def add(text, size, y_ratio, t0, t1):
            if not text or t1 <= t0:
                return
            png = os.path.join(tmp, f"o{len(overlays)}.png")
            w, h = _chip_png(text, size, png, font)
            overlays.append({
                "path": png, "x": (TARGET_W - w) // 2,
                "y": int(TARGET_H * y_ratio - h / 2),
                "enable": f"between(t,{t0:.2f},{t1:.2f})",
            })

        hook_end = min(_f(plan["hook"]["seconds"], 2.4), total - 0.2)
        add(plan["hook"]["text"], HOOK_SIZE, HOOK_Y, 0.0, hook_end)
        if plan.get("label"):
            add(plan["label"], LABEL_SIZE, HOOK_Y + 0.065, 0.0, hook_end)

        cta = plan["cta"]["text"]
        last_i = len(shots) - 1
        for i, s in enumerate(shots):
            if not s["caption"]:
                continue
            a = max(starts[i] + 0.35, hook_end + 0.12)
            b = starts[i] + durs[i] - 0.35
            if cta and i == last_i:
                b = min(b, starts[i] + durs[i] / 2 - 0.1)  # 뒷자리는 CTA 몫
            add(s["caption"], CAPTION_SIZE, CAPTION_Y, a, b)
        if cta:
            add(cta, CTA_SIZE, CAPTION_Y, starts[last_i] + durs[last_i] / 2 + 0.15, total - 0.1)

        # ── 4) 합성 ──
        cmd = [ff, "-y"]
        for p in norms:
            cmd += ["-i", p]
        for o in overlays:
            cmd += ["-i", o["path"]]
        for i, o in enumerate(overlays):
            idx = len(norms) + i
            parts.append(f"{prev}[{idx}:v]overlay={o['x']}:{o['y']}:"
                         f"enable='{o['enable']}'[y{i}]")
            prev = f"[y{i}]"

        # ⚠️ 오프닝 페이드인은 넣지 않는다. 인스타는 첫 0.5초로 이탈을 판단하는데
        #    페이드인은 그 시간을 어두운 화면으로 날린다. 엔딩만 페이드아웃.
        parts.append(f"{prev}fade=t=out:st={max(0.0, total - 0.45):.2f}:d=0.45[vout]")

        # ── 5) 소리: 무음 바탕 + audio:true 샷의 원본 소리만 ──
        audio_shots = [(i, s) for i, s in enumerate(shots)
                       if s["audio"] and _has_audio(ff, os.path.join(clip_dir, s["clip"]))]
        sil_idx = len(norms) + len(overlays)
        cmd += ["-f", "lavfi", "-t", f"{total:.2f}", "-i", "anullsrc=r=48000:cl=stereo"]
        mix = ["[%d:a]" % sil_idx]
        for n, (i, s) in enumerate(audio_shots):
            cmd += ["-ss", f"{s['in']:.3f}", "-t", f"{s['dur']:.3f}",
                    "-i", os.path.join(clip_dir, s["clip"])]
            src_i = sil_idx + 1 + n
            delay = int(starts[i] * 1000)
            parts.append(
                f"[{src_i}:a]afade=t=in:st=0:d=0.25,"
                f"afade=t=out:st={max(0.0, s['dur'] - 0.4):.2f}:d=0.4,"
                f"adelay={delay}|{delay}[a{n}]")
            mix.append(f"[a{n}]")
        if len(mix) > 1:
            parts.append(f"{''.join(mix)}amix=inputs={len(mix)}:"
                         f"duration=first:dropout_transition=0[aout]")
            amap = "[aout]"
        else:
            amap = f"{sil_idx}:a"

        cmd += ["-filter_complex", ";".join(parts),
                "-map", "[vout]", "-map", amap,
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-t", f"{total:.2f}",
                "-map_metadata", "-1", "-movflags", "+faststart", output_path]
        _run(cmd)

    logger.info("릴스 생성 완료(구성표 %d샷, %.1f초): %s", len(shots), total, output_path)
    return {
        "path": output_path,
        "seconds": total,
        "shots": [{**s, "start": round(starts[i], 2), "rendered": round(durs[i], 2)}
                  for i, s in enumerate(shots)],
    }


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default
