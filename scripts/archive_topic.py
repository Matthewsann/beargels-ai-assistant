# -*- coding: utf-8 -*-
"""사이클 끝난 주제 폴더를 보관으로 — 드라이브가 계속 불어나지 않게.

사장님 요구(2026-08-28): 원장 방식은 좋은데 드라이브도 정리가 돼야 한다.
개별 파일을 옮기면 채널 간 공유가 깨지므로, **주제 사이클이 끝난 뒤**
폴더째 옮기는 것이 정답이다:

    원본소재/<주제>/  →  보관/<올해>/<주제>/   (사진·클립)
                          단, 4K 원본 영상(MOV)은 용량이 커서 드라이브에 두지 않고
                          집 PC(data/archive_media/<주제>/)로 내린 뒤 드라이브에선 삭제.

    py scripts/archive_topic.py               보관 후보 보기(원장 기준 자동 판정)
    py scripts/archive_topic.py "주제이름"     그 주제를 실제로 보관
    py scripts/archive_topic.py "주제이름" --keep-video   영상도 드라이브 보관에 유지

보관해도 원장 기록·발행된 글은 그대로다(full_path 가 보관 폴더까지 찾아본다).
"""
from __future__ import annotations

import io
import pathlib
import shutil
import sys
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "worker"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import blog_media  # noqa: E402
import media_ledger  # noqa: E402

LOCAL_VIDEO_STORE = ROOT / "data" / "archive_media"
VIDEO_EXT = {".mov", ".m4v"}          # 4K 원본류. 편집 mp4 클립(작음)은 드라이브 보관


def show_candidates() -> None:
    idx = blog_media.load_index()
    cands = media_ledger.archive_candidates(idx)
    summary = media_ledger.topic_summary(idx)
    print("주제별 현황:")
    for topic, t in sorted(summary.items()):
        ch = ",".join(media_ledger.CHANNEL_KO.get(c, c) for c in t["channels"]) or "-"
        mark = " ← 보관 후보" if topic in cands else ""
        print(f"  {topic}: 소재 {t['total']} · 미사용 {t['unused']} · "
              f"쓴 채널 [{ch}] · 마지막 사용 {t['last_used'] or '-'}{mark}")
    if not cands:
        print("\n보관 후보 없음 (소재 절반 이상 사용 + 2주 경과가 기준)")
    else:
        print(f"\n보관하려면:  py scripts/archive_topic.py \"{cands[0]}\"")


def archive(topic: str, keep_video: bool = False) -> None:
    hub = blog_media.shelf_dir()
    src = hub / topic
    if not src.exists():
        sys.exit(f"주제 폴더가 없어요: {src}")
    dest = pathlib.Path(blog_media.ARCHIVE_DIR) / str(date.today().year) / topic
    dest.mkdir(parents=True, exist_ok=True)

    moved_drive = moved_local = 0
    for f in sorted(src.rglob("*")):
        if not f.is_file() or f.name == "desktop.ini":
            continue
        rel_in_topic = f.relative_to(src)
        if not keep_video and f.suffix.lower() in VIDEO_EXT:
            # 4K 원본은 집 PC로 — 드라이브 용량을 돌려받는다
            target = LOCAL_VIDEO_STORE / topic / rel_in_topic
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(target))
            moved_local += 1
        else:
            target = dest / rel_in_topic
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(target))
            moved_drive += 1
    # 빈 폴더 정리
    for sub in sorted(src.rglob("*"), reverse=True):
        if sub.is_dir():
            try:
                sub.rmdir()
            except OSError:
                pass
    try:
        src.rmdir()
    except OSError:
        pass

    # 인덱스에서 이 주제를 뺀다(원장 기록은 이력이므로 그대로 둔다)
    idx = blog_media.load_index()
    for rel in [r for r in idx if r.split("/")[0] == topic]:
        del idx[rel]
    blog_media.save_index(idx)

    print(f"보관 완료 — 「{topic}」")
    print(f"  드라이브 보관: {moved_drive}개 → {dest}")
    if moved_local:
        print(f"  PC 백업(드라이브에서 제거): 영상 {moved_local}개 → "
              f"{LOCAL_VIDEO_STORE / topic}")


def main() -> None:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        show_candidates()
        return
    archive(args[0], keep_video="--keep-video" in sys.argv)


if __name__ == "__main__":
    main()
