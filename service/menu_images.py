"""메뉴 사진 정본 — 원본 1장을 받아 채널별 규격으로 잘라 저장한다.

채널마다 요구하는 사진 규격이 달라(배민 4:3, 쿠팡 1080×660, 네이버·키오스크
1:1) 신메뉴마다 편집을 반복하던 것을, 원본 1장 업로드로 끝낸다(사장님 확정
2026-08-26 인터뷰). 저장은 Supabase Storage 공개 버킷 `menu-images` —
직원은 채널별 파일을 내려받아 각 포털에 올리면 된다.

경로 규칙(컬럼 없음 — 규칙이 곧 저장소다):
    {sku}/original.jpg   원본(긴 변 1600px 로 줄인 것)
    {sku}/{channel}.jpg  채널 규격 크롭

크롭은 중앙 기준 cover(꽉 채우고 넘치는 쪽을 자름). 음식 사진은 대부분
가운데 놓고 찍으므로 이걸로 충분하고, 아쉬운 사진만 다시 찍는 편이
초점 지정 UI 를 만드는 것보다 싸다.
"""
from __future__ import annotations

import io

from PIL import Image, ImageOps

BUCKET = "menu-images"

# 채널별 규격 (가로, 세로) — 출처는 각 채널 공식 가이드(2026-08 조사):
#  · 배민 1280×960, 음식이 중앙 960×960 안에 들어와야 함(중앙 크롭이라 충족)
#  · 쿠팡이츠 1080×660 ("별도 사이즈는 변형됨")
#  · 네이버·키오스크(토스포스)는 1:1 관행 — 1000×1000
SPECS = {
    "baemin": (1280, 960),
    "coupang": (1080, 660),
    "naver": (1000, 1000),
    "kiosk": (1000, 1000),
}
LABELS = {"baemin": "배민", "coupang": "쿠팡이츠", "naver": "네이버", "kiosk": "키오스크"}

MAX_UPLOAD = 15 * 1024 * 1024      # 폰 원본 사진도 충분한 15MB
_ORIGINAL_MAX = 1600               # 원본 보관용 축소(긴 변)


def _cover_crop(img: Image.Image, w: int, h: int) -> Image.Image:
    """비율을 맞춰 꽉 채우고 넘치는 쪽을 중앙에서 자른 뒤 (w, h)로 줄인다."""
    return ImageOps.fit(img, (w, h), Image.LANCZOS, centering=(0.5, 0.5))


def _jpeg(img: Image.Image, quality: int = 88) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def build_variants(raw: bytes) -> dict[str, bytes]:
    """원본 바이트 → {파일명: JPEG 바이트}. 형식이 아니면 ValueError."""
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"이미지 파일이 아닙니다 ({e})") from None
    img = ImageOps.exif_transpose(img)          # 폰 세로 사진의 회전 정보 반영
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    out = {}
    o = img.copy()
    o.thumbnail((_ORIGINAL_MAX, _ORIGINAL_MAX), Image.LANCZOS)
    out["original.jpg"] = _jpeg(o)
    for ch, (w, h) in SPECS.items():
        out[f"{ch}.jpg"] = _jpeg(_cover_crop(img, w, h))
    return out


def upload_all(client, sku: str, raw: bytes) -> dict:
    """변환하고 버킷에 저장. 같은 이름은 덮어쓴다(재업로드=교체).

    원본은 **마지막에** 올린다 — 중간에 끊기면 채널 파일 일부만 남는데,
    화면은 원본 유무로 '사진 있음'을 판단하므로 원본이 마지막이어야
    반쯤 실패한 업로드가 완료로 위장하지 못한다.
    """
    files = build_variants(raw)
    st = client.storage.from_(BUCKET)
    names = sorted(files, key=lambda n: n == "original.jpg")   # 원본이 맨 뒤
    for name in names:
        st.upload(f"{sku}/{name}", files[name],
                  {"content-type": "image/jpeg", "upsert": "true"})
    return {"files": sorted(files), "channels": list(SPECS)}


def delete_all(client, sku: str) -> int:
    st = client.storage.from_(BUCKET)
    names = [o["name"] for o in st.list(sku)]
    if names:
        st.remove([f"{sku}/{n}" for n in names])
    return len(names)
