"""소재 폴더 감시 — "폴더에 올리면 시작된다".

목표(사장님 확정 2026-08-28):
    "내가 특정 폴더에 찍은 영상과 사진을 올리면, ... 릴스가 만들어진다"

즉 **입구는 폴더**다. 웹에서 프로젝트를 먼저 만드는 건 사장님 몫이 아니다.

창고는 멀티채널 소재 허브와 같은 곳을 쓴다(2026-08-28 `worker/media_ledger.py`).
드라이브가 집 PC에 로컬 동기화되어 있으므로 **드라이브 API 없이 폴더만 본다.**

    <원본소재>/
        제철 과일산도 단면/   ← 주제 폴더 하나 = 릴스 프로젝트 하나
        _상시_메뉴/           ← '_' 로 시작하면 상시 창고. 프로젝트로 만들지 않음
        보관/                 ← 아카이브

원본은 **복사하지 않는다**(4K 영상이라 무겁고, 창고가 두 벌이 된다).
프로젝트에 `source_dir` 만 적어두고 편집할 때 거기서 바로 읽는다.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".webp"}

#: 프로젝트로 만들지 않을 폴더 (상시 창고·보관·작업용)
SKIP_PREFIXES = ("_", ".", "~")
SKIP_NAMES = {"보관", "완성본", "archive", "_클립"}


def source_root() -> str | None:
    """소재 창고 경로. .env 의 REEL_SOURCE_DIR 우선, 없으면 기본 드라이브 경로."""
    env = os.getenv("REEL_SOURCE_DIR", "").strip()
    if env:
        return env if os.path.isdir(env) else None
    guess = os.path.join(
        os.path.expanduser("~"), "Google Drive", "1. Project_현재진행하는일",
        "1. Business", "베어글스_송도_타임스페이스", "오픈후", "콘텐츠 생성", "원본소재")
    return guess if os.path.isdir(guess) else None


def _is_topic_dir(name: str) -> bool:
    if name in SKIP_NAMES:
        return False
    return not name.startswith(SKIP_PREFIXES)


def scan_media(folder: str) -> dict:
    """폴더 안의 영상·사진 목록과 최신 수정시각."""
    videos, images, newest = [], [], 0.0
    try:
        entries = sorted(os.listdir(folder))
    except OSError:
        return {"videos": [], "images": [], "newest": 0.0}
    for name in entries:
        p = os.path.join(folder, name)
        if not os.path.isfile(p):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext in VIDEO_EXT:
            videos.append(name)
        elif ext in IMAGE_EXT:
            images.append(name)
        else:
            continue
        try:
            newest = max(newest, os.path.getmtime(p))
        except OSError:
            pass
    return {"videos": videos, "images": images, "newest": newest}


def list_topics(root: str | None = None) -> list[dict]:
    """창고의 주제 폴더 목록. 영상이 하나라도 있어야 릴스를 만들 수 있다."""
    root = root or source_root()
    if not root:
        return []
    out = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isdir(path) or not _is_topic_dir(name):
            continue
        media = scan_media(path)
        out.append({
            "topic": name,
            "path": path,
            "videos": len(media["videos"]),
            "images": len(media["images"]),
            "newest": media["newest"],
            "ready": len(media["videos"]) > 0,
        })
    return out


def guide_text(folder: str) -> str:
    """주제 폴더에 넣어둔 촬영 가이드 문서가 있으면 읽는다(있으면 기획에 쓴다)."""
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if re.search(r"촬영\s*가이드|shot.?guide", name, re.I) and name.lower().endswith(
                (".txt", ".md")):
            try:
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    return f.read()[:4000]
            except OSError:
                pass
    return ""
