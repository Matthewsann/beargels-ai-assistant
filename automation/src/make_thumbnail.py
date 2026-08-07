"""
[미디어 3] 브랜드 썸네일 / 이벤트 포스터 / 카드뉴스 자동 생성.

베어글스 브랜드 톤(크림·브라운·곰돌이)으로 디자인 이미지를 코드로 그려냅니다.
AI 생성이 아니라 템플릿 방식이라 **매번 똑같은 브랜드 룩**이 보장됩니다.

    python src/make_thumbnail.py "송도 베이글 맛집, 소금빵 출시"
    python src/make_thumbnail.py "여름 이벤트" --type poster --sub "베이글 2개 사면 음료 반값"
    python src/make_thumbnail.py "베이글 데우는 법" --photo media/enhanced/사진_보정.jpg

옵션:  --type thumb(1000x1000, 기본) | poster(1080x1350) | card(1080x1080)
       --sub "부제목"        --photo 배경사진경로
"""

from __future__ import annotations

import argparse
import pathlib
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "media" / "thumbs"

# 베어글스 브랜드 팔레트
CREAM = "#F6EBD9"      # 배경
BROWN = "#4A2F1B"      # 본문 텍스트
BEAR = "#8B5E3C"       # 곰 몸통
BEAR_LIGHT = "#D9B38C" # 곰 주둥이/귀 안쪽
ACCENT = "#E8B04B"     # 포인트(꿀색)

SIZES = {"thumb": (1000, 1000), "poster": (1080, 1350), "card": (1080, 1080)}

FONT_CANDIDATES = [
    "C:/Windows/Fonts/malgunbd.ttf",            # 윈도우 맑은고딕 굵게
    "C:/Windows/Fonts/malgun.ttf",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # 한글 미지원 폴백
]


def font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def draw_bear(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int) -> None:
    """단순 도형으로 그리는 베어글스 곰 아이콘 — 항상 동일한 브랜드 마크."""
    er = int(r * 0.42)
    for dx in (-int(r * 0.72), int(r * 0.72)):   # 귀
        draw.ellipse([cx + dx - er, cy - r - er // 2, cx + dx + er, cy - r + er + er // 2], fill=BEAR)
        ir = int(er * 0.55)
        draw.ellipse([cx + dx - ir, cy - r - ir // 2 + int(er * 0.2),
                      cx + dx + ir, cy - r + ir + ir // 2 + int(er * 0.2)], fill=BEAR_LIGHT)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=BEAR)          # 얼굴
    mw, mh = int(r * 0.62), int(r * 0.45)                              # 주둥이
    draw.ellipse([cx - mw, cy + int(r * 0.15) - mh // 2, cx + mw, cy + int(r * 0.15) + mh], fill=BEAR_LIGHT)
    eye = max(4, int(r * 0.09))                                        # 눈
    for dx in (-int(r * 0.38), int(r * 0.38)):
        draw.ellipse([cx + dx - eye, cy - int(r * 0.22) - eye, cx + dx + eye, cy - int(r * 0.22) + eye], fill=BROWN)
    nr = max(5, int(r * 0.12))                                         # 코
    draw.ellipse([cx - nr, cy + int(r * 0.12) - nr, cx + nr, cy + int(r * 0.12) + nr], fill=BROWN)
    draw.arc([cx - int(r * 0.22), cy + int(r * 0.18),                  # 입
              cx + int(r * 0.22), cy + int(r * 0.42)], 20, 160, fill=BROWN, width=max(3, r // 25))


def wrap_text(draw, text: str, fnt, max_w: int) -> list[str]:
    lines, line = [], ""
    for ch in text:
        if draw.textlength(line + ch, font=fnt) <= max_w and ch != "\n":
            line += ch
        else:
            lines.append(line.strip())
            line = "" if ch == "\n" else ch
    if line.strip():
        lines.append(line.strip())
    return lines


def make(title: str, sub: str | None, kind: str, photo: str | None) -> pathlib.Path:
    w, h = SIZES.get(kind, SIZES["thumb"])
    img = Image.new("RGB", (w, h), CREAM)
    draw = ImageDraw.Draw(img)
    text_color = BROWN

    if photo:  # 배경사진 모드: 사진 + 어두운 오버레이 + 흰 글씨
        p = pathlib.Path(photo) if pathlib.Path(photo).is_absolute() else ROOT / photo
        bg = ImageOps.exif_transpose(Image.open(p)).convert("RGB")
        bg = ImageOps.fit(bg, (w, h), Image.LANCZOS)
        bg = bg.filter(ImageFilter.GaussianBlur(2))
        overlay = Image.new("RGB", (w, h), "#2A1A0E")
        img = Image.blend(bg, overlay, 0.45)
        draw = ImageDraw.Draw(img)
        text_color = "#FFF6E8"
    else:      # 브랜드 배경 모드: 크림 배경 + 테두리
        m = int(w * 0.035)
        draw.rounded_rectangle([m, m, w - m, h - m], radius=int(w * 0.04),
                               outline=BEAR, width=max(4, w // 180))

    bear_r = int(w * 0.10)
    draw_bear(draw, w // 2, int(h * 0.26), bear_r)

    tf = font(int(w * 0.075))
    lines = wrap_text(draw, title, tf, int(w * 0.82))[:3]
    y = int(h * 0.44)
    for ln in lines:
        lw = draw.textlength(ln, font=tf)
        draw.text(((w - lw) / 2, y), ln, font=tf, fill=text_color)
        y += int(w * 0.10)

    if sub:
        sf = font(int(w * 0.042))
        for ln in wrap_text(draw, sub, sf, int(w * 0.8))[:2]:
            lw = draw.textlength(ln, font=sf)
            draw.text(((w - lw) / 2, y + int(h * 0.015)), ln, font=sf, fill=ACCENT if not photo else "#FFD98A")
            y += int(w * 0.06)

    bf = font(int(w * 0.036))                                  # 하단 브랜드 라인
    brand = "🐻 베어글스 송도점 | 인천 송도 수제 베이글"
    brand = "베어글스 송도점  ·  인천 송도 수제 베이글"       # 이모지 렌더 불가 폰트 대비
    lw = draw.textlength(brand, font=bf)
    draw.text(((w - lw) / 2, h - int(h * 0.09)), brand, font=bf, fill=text_color)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    existing = len(list(OUT_DIR.glob(f"{kind}_*.png")))
    out = OUT_DIR / f"{kind}_{existing + 1:03d}.png"
    img.save(out, "PNG")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="베어글스 브랜드 썸네일/포스터/카드뉴스 생성")
    ap.add_argument("title", help="메인 문구 (예: '송도 베이글 맛집, 소금빵 출시')")
    ap.add_argument("--sub", help="부제목/부가 문구")
    ap.add_argument("--type", default="thumb", choices=list(SIZES), dest="kind")
    ap.add_argument("--photo", help="배경으로 쓸 실물 사진 경로 (media/enhanced/... )")
    args = ap.parse_args()

    out = make(args.title, args.sub, args.kind, args.photo)
    print(f"완료: {out.relative_to(ROOT)}")
    print("→ 같은 명령으로 계속 만들어도 브랜드 룩이 항상 동일하게 유지됩니다.")


if __name__ == "__main__":
    main()
