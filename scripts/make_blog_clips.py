"""찍어둔 긴 영상들 → 블로그용 짧은 클립 → 사진함 '영상' 칸에 넣기.

원본 4K 영상은 한 개가 수십~수백 MB 라 사진함(드라이브)에 그대로 두면 동기화
용량만 잡아먹고 블로그에 올리기도 무겁다. 그래서 원본은 원본소재 폴더에 두고,
**AI가 고른 핵심 8~12초만 잘라 만든 가벼운 클립**을 사진함에 넣는다.

    py scripts/make_blog_clips.py                    기본 폴더(원본소재) 전부
    py scripts/make_blog_clips.py "다른폴더"          그 폴더의 영상들
    py scripts/make_blog_clips.py --limit 3          3개만

이미 만든 클립은 건너뛴다(여러 번 돌려도 안전).
"""
from __future__ import annotations

import io
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "worker"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import blog_media  # noqa: E402
import blog_video  # noqa: E402

DEFAULT_SRC = (
    r"C:\Users\명구\Google Drive\1. Project_현재진행하는일\1. Business"
    r"\베어글스_송도_타임스페이스\오픈후\콘텐츠 생성\원본소재"
)
DUP = re.compile(r" \(\d+\)$")           # 드라이브가 만든 중복본


def sources(folder: pathlib.Path) -> list[pathlib.Path]:
    out = []
    for f in sorted(folder.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in blog_media.VIDEO_EXT:
            continue
        if DUP.search(f.stem):
            continue
        out.append(f)
    return out


def main() -> None:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    argv = sys.argv[1:]
    limit = None
    if "--limit" in argv:
        i = argv.index("--limit")
        limit = int(argv[i + 1])
        del argv[i:i + 2]              # 옵션 값이 폴더 이름으로 오해되지 않게
    args = [a for a in argv if not a.startswith("--")]

    src_dir = pathlib.Path(args[0]) if args else pathlib.Path(DEFAULT_SRC)
    shelf_videos = blog_media.shelf_dir() / "영상"
    shelf_videos.mkdir(parents=True, exist_ok=True)

    vids = sources(src_dir)
    todo = [v for v in vids if not (shelf_videos / f"{v.stem}.mp4").exists()]
    if limit:
        todo = todo[:limit]
    print(f"원본 영상 {len(vids)}개 · 새로 만들 것 {len(todo)}개\n")

    idx = blog_media.load_index()
    made = 0
    for i, v in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {v.name}")
        try:
            info = blog_video.build(v, shelf_videos)
        except Exception as e:  # noqa: BLE001 — 하나 실패로 전체를 멈추지 않는다
            print(f"    ✗ 실패: {str(e)[:120]}")
            continue
        if not info:
            print("    · 쓸 만한 구간이 없어 건너뜁니다.")
            continue
        out = pathlib.Path(info["path"])
        print(f"    ✓ {info['start']}~{info['end']}초 → {out.name} "
              f"({info['size'] / 1_000_000:.1f}MB)")
        print(f"      {info.get('subject', '')}")
        # AI 가 영상을 보며 적은 설명을 사진함 인덱스에 그대로 넣어 둔다
        # (사진처럼 '무엇이 찍혔는지' 를 알아야 글에 골라 쓸 수 있다)
        rel = f"영상/{out.name}"
        st = out.stat()
        idx[rel] = {
            "rel": rel, "slot": "영상", "kind": "video",
            "size": st.st_size, "mtime": int(st.st_mtime),
            "subject": info.get("subject", ""), "scene": "영상",
            "caption": info.get("caption", ""),
            "keywords": info.get("keywords") or [],
            "quality": "good", "hero": False,
            "source": info.get("source", v.name),
        }
        blog_media.save_index(idx)
        made += 1

    print(f"\n완료 — 클립 {made}개를 사진함 '영상' 칸에 넣었습니다.")


if __name__ == "__main__":
    main()
