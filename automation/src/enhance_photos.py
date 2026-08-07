"""
[미디어 1] 실물 사진 자동 보정.

media/inbox/ 에 넣은 사진을 음식사진 프리셋으로 자동 보정해 media/enhanced/ 에 저장합니다.
- EXIF 회전 보정, 블로그용 리사이즈(기본 최대 1600px)
- 프리셋: food(따뜻함+채도+선명, 기본) / soft(부드럽게) / none(리사이즈만)

    python src/enhance_photos.py            # inbox 전체 보정
    python src/enhance_photos.py 사진.jpg   # 특정 파일만
"""

from __future__ import annotations

import pathlib
import sys

from PIL import Image, ImageEnhance, ImageOps

ROOT = pathlib.Path(__file__).resolve().parent.parent
INBOX = ROOT / "media" / "inbox"
OUT = ROOT / "media" / "enhanced"

EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic"}

# 프리셋: (밝기, 대비, 채도, 선명도, 따뜻함 0~1)
PRESETS = {
    "food": (1.03, 1.08, 1.16, 1.35, 0.10),   # 먹음직스럽게: 따뜻+쨍하게
    "soft": (1.05, 1.03, 1.06, 1.10, 0.05),   # 감성 톤: 은은하게
    "none": (1.00, 1.00, 1.00, 1.00, 0.00),
}


def load_media_cfg() -> dict:
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        return cfg.get("media", {}) or {}
    except Exception:
        return {}


def warm(img: Image.Image, amount: float) -> Image.Image:
    """따뜻한 톤 보정: R을 살짝 올리고 B를 살짝 내림."""
    if amount <= 0:
        return img
    r, g, b = img.split()
    r = r.point(lambda v: min(255, int(v * (1 + 0.35 * amount))))
    b = b.point(lambda v: int(v * (1 - 0.30 * amount)))
    return Image.merge("RGB", (r, g, b))


def enhance_one(src: pathlib.Path, out_dir: pathlib.Path, preset: str, max_size: int) -> pathlib.Path | None:
    try:
        img = Image.open(src)
    except Exception as e:
        print(f"  ✗ {src.name}: 열기 실패 ({e})")
        return None

    img = ImageOps.exif_transpose(img)  # 폰 세로/가로 회전 정보 반영
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.thumbnail((max_size, max_size), Image.LANCZOS)

    bright, contrast, color, sharp, warmth = PRESETS.get(preset, PRESETS["food"])
    img = ImageOps.autocontrast(img, cutoff=1)          # 물빠진 톤 정리
    img = warm(img, warmth)
    img = ImageEnhance.Brightness(img).enhance(bright)
    img = ImageEnhance.Contrast(img).enhance(contrast)
    img = ImageEnhance.Color(img).enhance(color)
    img = ImageEnhance.Sharpness(img).enhance(sharp)

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{src.stem}_보정.jpg"
    img.save(out, "JPEG", quality=90)
    print(f"  ✓ {src.name} → {out.name}")
    return out


def main() -> None:
    mcfg = load_media_cfg()
    preset = mcfg.get("preset", "food")
    max_size = int(mcfg.get("max_size", 1600))

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        files = [pathlib.Path(a) if pathlib.Path(a).is_absolute() else ROOT / a for a in args]
    else:
        INBOX.mkdir(parents=True, exist_ok=True)
        files = sorted(p for p in INBOX.iterdir() if p.suffix.lower() in EXTS)

    if not files:
        print(f"media/inbox/ 에 사진이 없습니다. 폰 사진을 복사해 넣고 다시 실행하세요.")
        return

    print(f"사진 {len(files)}장 자동 보정 (프리셋: {preset})")
    done = sum(1 for f in files if enhance_one(f, OUT, preset, max_size))
    print(f"\n완료: {done}/{len(files)}장 → media/enhanced/ (블로그에 이 폴더 사진을 쓰세요)")


if __name__ == "__main__":
    main()
