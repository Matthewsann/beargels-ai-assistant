"""기존에 찍어둔 사진을 '베어글스_블로그_사진함' 으로 모아 온다.

블로그 파이프라인은 **사진함 한 곳만** 읽는다(사장님 확정 2026-08-27).
규칙이 하나여야 폰에서 올릴 때도 헷갈리지 않기 때문이다. 그런데 그동안
찍어둔 사진은 드라이브 여기저기(음식사진·매장사진·원본소재)에 흩어져
있어서, 그것들을 한 번 사진함으로 옮겨 담는 것이 이 스크립트다.

    py scripts/import_to_shelf.py --dry     무엇이 복사될지만 보기
    py scripts/import_to_shelf.py           실제 복사

- 원본은 그대로 두고 **복사**만 한다(되돌리려면 사진함에서 지우면 끝).
- 드라이브가 자동으로 만든 " (1)", " (2)" 중복본은 건너뛴다.
- 이미 사진함에 같은 이름이 있으면 건너뛴다(여러 번 돌려도 안전).
- 영상 원본(4K·수백MB)은 복사하지 않는다 — 파이프라인이 편집한
  블로그용 클립만 '영상' 칸에 들어간다(scripts/make_blog_clips.py).
"""
from __future__ import annotations

import pathlib
import re
import shutil
import sys

DRIVE = pathlib.Path(
    r"C:\Users\명구\Google Drive\1. Project_현재진행하는일\1. Business"
    r"\베어글스_송도_타임스페이스\오픈후"
)
SHELF = DRIVE / "브랜딩" / "베어글스_블로그_사진함"

PHOTO_EXT = {".jpg", ".jpeg", ".png", ".heic", ".webp"}

# (사진함 안의 칸, 가져올 원본 폴더) — 위에서부터 순서대로 담는다.
SOURCES = [
    ("메뉴",      DRIVE / "사진" / "음식사진"),
    ("매장",      DRIVE / "사진" / "매장사진"),
    ("매장",      DRIVE / "브랜딩" / "베어글스_매장이미지"),
    ("만드는과정", DRIVE / "콘텐츠 생성" / "원본소재" / "제철 과일산도 단면"),
    ("기타",      DRIVE / "사진"),          # 최상위에 흩어진 것들
]

# 드라이브 중복본: "IMG_5909 (1).jpg"
DUP = re.compile(r" \(\d+\)$")

ANNOUNCE = """# 📸 블로그 사진함

블로그 자동화는 **이 폴더만** 봅니다. 여기 넣은 사진·영상이 블로그 글에 들어갑니다.

| 칸 | 무엇을 넣나 |
|----|-------------|
| `메뉴` | 베이글·샌드위치·음료 등 **음식 사진** |
| `매장` | 매장 내부·외부·좌석·카운터 |
| `만드는과정` | 자르는 순간, 크림 바르는 순간 같은 **과정 컷** |
| `기타` | 위에 안 맞는 것 (손님·소품·이벤트 등) |
| `영상` | 블로그에 넣을 **짧은 영상** (자동 편집본이 여기 쌓입니다) |

## 폰에서 올리는 법
갤러리에서 사진 선택 → 공유 → 구글 드라이브 → 위 칸 중 하나 선택.

## 안 해도 되는 것
- 이름 바꾸기 ❌ — AI 가 사진을 직접 보고 무엇인지 알아냅니다.
- 고르기 ❌ — 여러 장 올려두면 글마다 어울리는 것을 알아서 씁니다.
- 보정 ❌ — 올릴 때 자동으로 회전·크기·화질을 맞춥니다.
"""


def slots() -> list[str]:
    return ["메뉴", "매장", "만드는과정", "기타", "영상"]


def collect(folder: pathlib.Path, recursive: bool) -> list[pathlib.Path]:
    if not folder.exists():
        return []
    it = folder.rglob("*") if recursive else folder.glob("*")
    out = []
    for f in it:
        if not f.is_file() or f.suffix.lower() not in PHOTO_EXT:
            continue
        if DUP.search(f.stem):          # 드라이브가 만든 중복본
            continue
        out.append(f)
    return sorted(out)


def main() -> None:
    dry = "--dry" in sys.argv
    if not SHELF.exists():
        sys.exit(f"사진함 폴더를 못 찾았어요: {SHELF}")

    for s in slots():
        (SHELF / s).mkdir(exist_ok=True) if not dry else None

    taken: set[str] = set()             # 같은 파일을 두 칸에 넣지 않도록
    total = skipped = 0
    for slot, src in SOURCES:
        # '기타' 는 최상위만(하위 폴더는 앞에서 이미 각자 칸으로 갔다)
        files = collect(src, recursive=(slot != "기타"))
        picked = []
        for f in files:
            if str(f).lower() in taken:
                continue
            dest = SHELF / slot / f.name
            used = SHELF / "사용완료" / slot / f.name
            if dest.exists() or used.exists():
                # 이미 사진함에 있거나, 한 번 글에 써서 사용완료로 옮겨진 사진
                # → 다시 가져오면 재사용 금지 규칙이 깨진다
                skipped += 1
                taken.add(str(f).lower())
                continue
            picked.append((f, dest))
            taken.add(str(f).lower())
        if not picked:
            continue
        print(f"[{slot}] {src.name} → {len(picked)}장")
        for f, dest in picked:
            if dry:
                print(f"    · {f.name}")
            else:
                shutil.copy2(f, dest)
            total += 1

    guide = SHELF / "_사진함 안내.md"
    if not dry and not guide.exists():
        guide.write_text(ANNOUNCE, encoding="utf-8")

    verb = "복사할 사진" if dry else "복사 완료"
    print(f"\n{verb}: {total}장 (이미 있어서 건너뜀 {skipped}장)")
    if dry:
        print("실제로 복사하려면 --dry 없이 다시 실행하세요.")


if __name__ == "__main__":
    main()
