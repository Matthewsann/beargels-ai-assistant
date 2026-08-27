# -*- coding: utf-8 -*-
"""사진함 → 원본소재 통합 마이그레이션 (한 번 실행, 2026-08-28 사장님 확정).

무엇을 하나:
    · 사진함의 상시 컷(메뉴/매장/기타)  → 원본소재/_상시_메뉴 등으로 이동
    · 사진함의 만드는과정(과일산도 복사본) → 원본이 원본소재/제철…에 있으므로 삭제
    · 사진함의 영상 클립               → 원본소재/제철…/_클립/ 으로 이동
    · 사용완료(글 #1이 쓴 6장 복사본)   → 원장(media_ledger)에 기록 후 삭제
    · AI 태깅 인덱스 키를 새 경로로 이어받음 (81장 재태깅 비용 0)
    · 구 사용 기록(blog_used_log)      → 원장으로 이관

    py scripts/migrate_media_hub.py --dry    무슨 일이 일어날지만 보기
    py scripts/migrate_media_hub.py          실제 실행
"""
from __future__ import annotations

import io
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "worker"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import blog_media  # noqa: E402  (이미 원본소재를 보게 전환된 상태)
import media_ledger  # noqa: E402

BASE = pathlib.Path(
    r"C:\Users\명구\Google Drive\1. Project_현재진행하는일\1. Business"
    r"\베어글스_송도_타임스페이스\오픈후"
)
OLD_SHELF = BASE / "브랜딩" / "베어글스_블로그_사진함"
HUB = BASE / "콘텐츠 생성" / "원본소재"
FRUIT = "제철 과일산도 단면"          # 만드는과정 복사본들의 원 주제

# 옛 칸 → 새 위치. None 이면 '원본이 이미 있으니 복사본 삭제'.
MOVES = {
    "메뉴": "_상시_메뉴",
    "매장": "_상시_매장",
    "기타": "_상시_기타",
    "만드는과정": None,               # 과일산도 원본이 HUB/제철…에 그대로 있다
    "영상": f"{FRUIT}/_클립",
}


def files_in(d: pathlib.Path):
    return [f for f in sorted(d.glob("*")) if f.is_file() and f.name != "desktop.ini"
            and not f.name.startswith("_")]


def main() -> None:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    dry = "--dry" in sys.argv
    idx_old = blog_media.load_index()
    idx_new: dict = {}
    remap: dict[str, str] = {}
    moved = deleted = 0

    for slot, target in MOVES.items():
        src_dir = OLD_SHELF / slot
        if not src_dir.exists():
            continue
        for f in files_in(src_dir):
            old_rel = f"{slot}/{f.name}"
            if target is None:
                orig = HUB / FRUIT / f.name
                new_rel = f"{FRUIT}/{f.name}"
                if orig.exists():
                    print(f"삭제(복사본): {old_rel} → 원본 {new_rel}")
                    if not dry:
                        f.unlink()
                    deleted += 1
                else:                     # 혹시 원본이 없으면 이동으로 보존
                    print(f"이동(원본 없음): {old_rel} → {new_rel}")
                    if not dry:
                        orig.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(f), str(orig))
                    moved += 1
            else:
                new_rel = f"{target}/{f.name}"
                dest = HUB / new_rel
                print(f"이동: {old_rel} → {new_rel}")
                if not dry:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(f), str(dest))
                moved += 1
            remap[old_rel] = new_rel

    # 사용완료(글 #1 이 쓴 복사본) → 원장 기록 후 삭제
    used_dir = OLD_SHELF / "사용완료"
    used_rels = []
    if used_dir.exists():
        for f in sorted(used_dir.rglob("*")):
            if not f.is_file() or f.name == "desktop.ini":
                continue
            slot = f.relative_to(used_dir).parts[0]
            target = MOVES.get(slot)
            new_rel = (f"{FRUIT}/{f.name}" if target is None
                       else f"{target}/{f.name}")
            used_rels.append(new_rel)
            orig = HUB / new_rel
            print(f"사용기록: {new_rel} (블로그 글#1) — "
                  f"{'복사본 삭제' if orig.exists() else '이동'}")
            if not dry:
                if orig.exists():
                    f.unlink()
                else:
                    orig.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(f), str(orig))
    if not dry and used_rels:
        media_ledger.record_many(used_rels, "blog", ref="글 #1 송도 과일산도")
        # 글 #1 에 들어간 클립도(본문에 있었으나 삽입 실패 — 기록하지 않음)

    # 인덱스 키 이어받기 — 재태깅 비용 0
    for old_rel, item in idx_old.items():
        new_rel = remap.get(old_rel)
        if not new_rel:
            continue
        item = dict(item)
        item["rel"] = new_rel
        item["slot"] = new_rel.split("/")[0]
        p = HUB / new_rel
        if p.exists():
            item["mtime"] = int(p.stat().st_mtime)
        idx_new[new_rel] = item
    print(f"\n인덱스 이어받음: {len(idx_new)}건 (재태깅 불필요)")
    if not dry:
        blog_media.save_index(idx_new)

    # 구 사용 기록(blog_used_log) → 원장 (영상 구간 등)
    log_p = ROOT / "data" / "blog_used_log.json"
    if log_p.exists() and not dry:
        import json
        for e in json.loads(log_p.read_text(encoding="utf-8")):
            new_rel = remap.get(e.get("rel") or "", e.get("rel"))
            if not new_rel:
                continue
            media_ledger.record(new_rel, "blog", ref=e.get("label", ""),
                                segment=([e["start"], e["end"]]
                                         if e.get("start") is not None else None))
        log_p.rename(log_p.with_suffix(".json.migrated"))
        print("구 사용 기록 → 원장 이관")

    # 빈 사진함 정리 + 안내
    if not dry:
        for slot in list(MOVES) + ["사용완료"]:
            d = OLD_SHELF / slot
            try:
                for sub in sorted(d.rglob("*"), reverse=True):
                    if sub.is_dir():
                        sub.rmdir()
                d.rmdir()
            except OSError:
                pass                      # desktop.ini 등이 남아 있으면 그대로 둔다
        (OLD_SHELF / "_이사갔어요.md").write_text(
            "# 📦 사진함은 '콘텐츠 생성 > 원본소재'로 통합됐습니다 (2026-08-28)\n\n"
            "이제 모든 채널(블로그·인스타·당근·플레이스)이 **원본소재/<주제>** 폴더\n"
            "한 곳을 같이 씁니다. 폰 업로드도 그쪽으로 해주세요.\n"
            "- 상시 컷(메뉴·매장): `원본소재/_상시_메뉴`, `_상시_매장`, `_상시_기타`\n"
            "- 촬영한 새 콘텐츠: `원본소재/<주제 이름>` 폴더를 만들어 그 안에\n",
            encoding="utf-8")

    print(f"\n{'[미리보기] ' if dry else ''}이동 {moved} · 복사본 삭제 {deleted}"
          f" · 사용기록 {len(used_rels)}건")


if __name__ == "__main__":
    main()
