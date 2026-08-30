"""미디어 사용 원장 — 어떤 소재를 어느 채널의 어느 글에 썼는지의 단일 장부.

왜 필요한가 (사장님 확정 2026-08-28):
    인스타·블로그·당근·네이버소식 등 여러 채널이 **같은 소재 창고**
    (원본소재/<주제>/)를 쓴다. 파일을 채널별로 복사하거나 옮기면
    ①같은 컷을 다른 채널이 또 쓰는지 아무도 모르고(중복)
    ②어느 채널도 안 쓴 컷이 묻힌다(누락).
    그래서 파일은 제자리에 두고, 이 장부가 사용을 기록·차단한다.

재사용 규칙:
    · 같은 채널 안에서는 같은 소재 재사용 금지 (available 이 걸러줌)
    · 채널 사이에는 허용 — 보는 사람이 다르다. 단 변주(크롭·다른 구간) 권장.
    · 영상 원본은 '구간'이 다르면 같은 채널이라도 다른 편집으로 재사용 가능.

장부: data/media_ledger.json
    {"<주제>/<파일>": [{"channel":"blog","ref":"#1","date":"...","segment":[6,11.6]}]}
"""
from __future__ import annotations

import json
import logging
import pathlib
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
LEDGER = ROOT / "data" / "media_ledger.json"

logger = logging.getLogger(__name__)

CHANNELS = ("blog", "insta", "danggeun", "place", "etc")
CHANNEL_KO = {"blog": "블로그", "insta": "인스타", "danggeun": "당근",
              "place": "플레이스", "etc": "기타"}


def _load() -> dict:
    if not LEDGER.exists():
        return {}
    try:
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save(d: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def record(rel: str, channel: str, ref: str = "",
           segment: tuple[float, float] | list | None = None,
           note: str = "") -> None:
    """소재 하나를 채널이 썼다고 기록한다. rel 은 창고 기준 상대경로."""
    d = _load()
    entry = {"channel": channel, "ref": ref,
             "date": datetime.now(timezone.utc).date().isoformat()}
    if segment:
        entry["segment"] = [round(float(segment[0]), 2), round(float(segment[1]), 2)]
    if note:
        entry["note"] = note
    d.setdefault(rel, []).append(entry)
    _save(d)


def record_many(rels: list[str], channel: str, ref: str = "") -> int:
    d = _load()
    today = datetime.now(timezone.utc).date().isoformat()
    n = 0
    for rel in rels:
        uses = d.setdefault(rel, [])
        if any(u.get("channel") == channel and u.get("ref") == ref for u in uses):
            continue                      # 같은 글로 두 번 기록하지 않는다(재발행)
        uses.append({"channel": channel, "ref": ref, "date": today})
        n += 1
    _save(d)
    return n


def uses(rel: str) -> list[dict]:
    return _load().get(rel, [])


def used_in(rel: str, channel: str) -> bool:
    return any(u.get("channel") == channel for u in uses(rel))


def used_segments(rel: str, channel: str | None = None) -> list[list[float]]:
    """이 영상 원본에서 이미 쓴 구간들(채널 지정 시 그 채널만)."""
    out = []
    for u in uses(rel):
        if channel and u.get("channel") != channel:
            continue
        if u.get("segment"):
            out.append(u["segment"])
    return out


def filter_unused(rels: list[str], channel: str) -> list[str]:
    """이 채널이 아직 안 쓴 것만."""
    d = _load()
    return [r for r in rels
            if not any(u.get("channel") == channel for u in d.get(r, []))]


def release_ref(ref: str) -> int:
    """특정 글(ref)이 잡아둔 사용 기록을 전부 해제한다.

    임시저장 시점에 기록하는데, 그 글이 발행 없이 휴지통으로 가면 소재가
    영영 잠긴다(2026-08-30 감사) — 글을 지울 때 이걸 불러 소재를 되살린다.
    """
    d = _load()
    n = 0
    for rel in list(d):
        kept = [u for u in d[rel] if u.get("ref") != ref]
        n += len(d[rel]) - len(kept)
        if kept:
            d[rel] = kept
        else:
            del d[rel]
    if n:
        _save(d)
        logger.info("원장 해제: %s → %d건", ref, n)
    return n


def rename(old_rel: str, new_rel: str) -> None:
    """창고 재편으로 경로가 바뀔 때 기록을 따라 옮긴다."""
    d = _load()
    if old_rel in d:
        d.setdefault(new_rel, []).extend(d.pop(old_rel))
        _save(d)


# ---------------------------------------------------------------------------
# 요약 — 웹 메시지·보관 판단에 쓴다
# ---------------------------------------------------------------------------

def topic_summary(index: dict) -> dict:
    """주제 폴더별 {전체, 사용(채널별), 미사용} 집계.

    index 는 blog_media.load_index() — rel 의 첫 폴더가 주제다.
    """
    d = _load()
    out: dict = {}
    for rel, item in index.items():
        topic = rel.split("/")[0]
        t = out.setdefault(topic, {"total": 0, "unused": 0, "channels": set(),
                                   "last_used": ""})
        t["total"] += 1
        us = d.get(rel, [])
        if not us:
            t["unused"] += 1
        for u in us:
            t["channels"].add(u.get("channel"))
            if u.get("date", "") > t["last_used"]:
                t["last_used"] = u["date"]
    for t in out.values():
        t["channels"] = sorted(c for c in t["channels"] if c)
    return out


def archive_candidates(index: dict, min_days_quiet: int = 14,
                       min_used_ratio: float = 0.5) -> list[str]:
    """보관해도 되는 주제 — 소재의 절반 이상을 썼고 2주간 조용한 것.

    상시 소재 폴더(_로 시작)는 제외 — 메뉴 대표컷처럼 계속 쓰는 것들이다.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    out = []
    for topic, t in topic_summary(index).items():
        if topic.startswith("_") or not t["total"]:
            continue
        used_ratio = 1 - t["unused"] / t["total"]
        if used_ratio < min_used_ratio or not t["last_used"]:
            continue
        quiet = (datetime.fromisoformat(today) -
                 datetime.fromisoformat(t["last_used"])).days
        if quiet >= min_days_quiet:
            out.append(topic)
    return out
