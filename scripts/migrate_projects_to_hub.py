# -*- coding: utf-8 -*-
"""인스타 프로젝트의 소재를 공용 허브(원본소재)로 이사 — 한 번 실행.

사장님 확정(2026-08-30): 업로드는 한 곳(원본소재/<주제>/), 블로그·인스타가
같이 쓴다. 예전 인스타 웹앱은 projects/<id>/raw/ 에 따로 받아 왔는데,
그 파일들을 대응 주제 폴더로 옮기고 프로젝트에 source_dir 를 연결한다.

    py scripts/migrate_projects_to_hub.py --dry    무슨 일이 일어날지만
    py scripts/migrate_projects_to_hub.py          실제 실행

- 허브에 같은 이름 파일이 이미 있으면 옮기지 않고 프로젝트 쪽을 지운다(중복 소거).
- 이동 후 raw/ 가 비면 폴더를 지운다. reel.mp4·project.json 은 그대로.
"""
from __future__ import annotations

import io
import json
import pathlib
import re
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sns_automation import source_watch  # noqa: E402

PROJECTS = ROOT / "projects"


def _slug(text: str) -> str:
    s = re.sub(r"[^\w가-힣]+", "-", text.strip()).strip("-")
    return s[:40] or "topic"


def main() -> None:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    dry = "--dry" in sys.argv
    root = source_watch.source_root()
    if not root:
        sys.exit("허브(원본소재) 폴더를 못 찾았어요.")
    hub = pathlib.Path(root)
    hub_by_slug = {_slug(d.name): d for d in hub.iterdir()
                   if d.is_dir() and not d.name.startswith(("_", "."))}

    moved = deduped = linked = 0
    for pj in sorted(PROJECTS.glob("*/project.json")):
        meta = json.loads(pj.read_text(encoding="utf-8"))
        title = meta.get("title") or pj.parent.name
        raw = pj.parent / "raw"
        if meta.get("source_dir") and pathlib.Path(meta["source_dir"]).is_dir():
            print(f"이미 연결됨: {title}")
            continue
        topic_dir = hub_by_slug.get(_slug(title))
        if topic_dir is None:
            topic_dir = hub / title
            print(f"허브에 주제 폴더 생성: {topic_dir.name}")
            if not dry:
                topic_dir.mkdir(exist_ok=True)
            hub_by_slug[_slug(title)] = topic_dir
        n_move = n_dup = 0
        if raw.is_dir():
            for f in sorted(raw.iterdir()):
                if not f.is_file():
                    continue
                dest = topic_dir / f.name
                if dest.exists() and dest.stat().st_size == f.stat().st_size:
                    n_dup += 1               # 허브에 같은 파일이 이미 — 중복 소거
                    if not dry:
                        f.unlink()
                else:
                    n_move += 1
                    if not dry:
                        shutil.move(str(f), str(dest))
            if not dry:
                try:
                    raw.rmdir()
                except OSError:
                    pass
        meta["source_dir"] = str(topic_dir)
        if not dry:
            pj.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        linked += 1
        moved += n_move
        deduped += n_dup
        print(f"연결: {title} → {topic_dir.name} (이동 {n_move} · 중복소거 {n_dup})")

    print(f"\n{'[미리보기] ' if dry else ''}프로젝트 {linked}개 연결 · "
          f"파일 이동 {moved} · 중복 소거 {deduped}")


if __name__ == "__main__":
    main()
