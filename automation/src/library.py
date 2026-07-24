"""
콘텐츠 라이브러리(재고 창고) 관리.

각 콘텐츠는 library/0001, library/0002 ... 폴더 하나로 관리되며,
안에 post.json(자동화용) · post.md(사람 확인용) · images/ · meta.yaml 가 들어갑니다.

상태(status) 흐름:
    ready      : 생성 완료, 발행 대기 (창고에 쌓인 상태)
    scheduled  : 사장님이 골라서 예약 발행 걸어둔 상태
    published  : 발행 완료(수동 표시)
"""

from __future__ import annotations

import datetime as _dt
import json
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
LIB = ROOT / "library"

STATUS_READY = "ready"
STATUS_SCHEDULED = "scheduled"
STATUS_PUBLISHED = "published"


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def next_id() -> int:
    LIB.mkdir(exist_ok=True)
    ids = [int(p.name) for p in LIB.iterdir() if p.is_dir() and p.name.isdigit()]
    return (max(ids) + 1) if ids else 1


def item_dir(item_id: int) -> pathlib.Path:
    return LIB / f"{item_id:04d}"


def existing_titles() -> list[str]:
    titles = []
    if not LIB.exists():
        return titles
    for meta in LIB.glob("*/meta.yaml"):
        try:
            titles.append(yaml.safe_load(meta.read_text(encoding="utf-8")).get("title", ""))
        except Exception:
            continue
    return [t for t in titles if t]


def create_item(post_type: str, data: dict) -> int:
    """생성된 글(data: title/body/tags/keywords)을 새 라이브러리 항목으로 저장."""
    item_id = next_id()
    d = item_dir(item_id)
    (d / "images").mkdir(parents=True, exist_ok=True)

    (d / "post.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    tags = " ".join(f"#{t.lstrip('#')}" for t in data.get("tags", []))
    kw = data.get("main_keyword", "")
    subs = ", ".join(data.get("sub_keywords", []))
    md = (
        f"# {data.get('title','(제목 없음)')}\n\n"
        f"> 대표 키워드: {kw} / 세부: {subs}\n\n"
        f"{data.get('body','')}\n\n{tags}\n"
    )
    (d / "post.md").write_text(md, encoding="utf-8")

    meta = {
        "id": item_id,
        "type": post_type,
        "title": data.get("title", ""),
        "main_keyword": kw,
        "status": STATUS_READY,
        "created": _now(),
        "scheduled_time": None,
    }
    save_meta(item_id, meta)
    return item_id


def load_meta(item_id: int) -> dict:
    return yaml.safe_load((item_dir(item_id) / "meta.yaml").read_text(encoding="utf-8"))


def save_meta(item_id: int, meta: dict) -> None:
    (item_dir(item_id) / "meta.yaml").write_text(
        yaml.safe_dump(meta, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def load_post(item_id: int) -> dict:
    return json.loads((item_dir(item_id) / "post.json").read_text(encoding="utf-8"))


def list_items(status: str | None = None) -> list[dict]:
    """상태별 항목 메타 목록(id 순)."""
    if not LIB.exists():
        return []
    metas = []
    for mp in sorted(LIB.glob("*/meta.yaml")):
        try:
            m = yaml.safe_load(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if status is None or m.get("status") == status:
            metas.append(m)
    metas.sort(key=lambda m: m.get("id", 0))
    return metas


def set_status(item_id: int, status: str, scheduled_time: str | None = None) -> None:
    meta = load_meta(item_id)
    meta["status"] = status
    if scheduled_time is not None:
        meta["scheduled_time"] = scheduled_time
    save_meta(item_id, meta)
