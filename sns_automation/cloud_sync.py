"""집 PC ↔ 직원 웹앱 다리 (Supabase Storage `sns-media`).

편집·렌더는 집 PC에서만 되지만(사장님 확정 아키텍처), **업로드와 결과물
받기는 어디서든** 돼야 한다. 그래서 공개 버킷 하나를 우편함처럼 쓴다.

    sns-media/
      inbox/<주제>/<파일>      ← 직원 웹앱에서 올린 촬영본. 집 PC가 가져간다.
      reels/<프로젝트id>.mp4   ← 집 PC가 만든 완성본. 웹앱에서 받는다.
      reels/<프로젝트id>.txt   ← 그 릴스의 캡션 (복사용)
      reels/index.json         ← 완성본 목록 (웹앱이 이것만 읽으면 된다)

**새 테이블을 만들지 않는다** — SQL 마이그레이션은 사장님이 직접 실행해야 하는
블로커라, 이미 있는 공개 버킷(002_media_bucket.sql)만으로 끝낸다.
"""

from __future__ import annotations

import base64
import json
import logging
import os

logger = logging.getLogger(__name__)

BUCKET = "sns-media"
INBOX = "inbox"
REELS = "reels"
INDEX = f"{REELS}/index.json"


# ⚠️ Supabase Storage 키는 **ASCII 만** 받는다(한글이면 400 InvalidKey).
# 주제·파일명이 한글이라 그대로 못 쓰므로 base64url 로 감쌌다 풀어서 쓴다.
# 스토리지에는 알아볼 수 없는 이름으로 들어가지만, 사람은 화면에서만 보므로 무방.
def enc(name: str) -> str:
    return base64.urlsafe_b64encode(name.encode("utf-8")).decode("ascii").rstrip("=")


def dec(key: str) -> str:
    pad = "=" * (-len(key) % 4)
    try:
        return base64.urlsafe_b64decode(key + pad).decode("utf-8")
    except Exception:
        return key          # 예전 방식으로 올라온 것(ASCII)은 그대로 쓴다


class CloudError(RuntimeError):
    """스토리지 접근 실패. 화면에 그대로 보여줄 한글 메시지."""


def client():
    """Supabase 클라이언트. 설정이 없으면 CloudError."""
    try:
        from database.supabase_client import get_client
        return get_client()
    except Exception as e:                       # 키 없음·네트워크 등
        raise CloudError(f"Supabase 연결이 안 돼요: {e}") from e


def _bucket(c=None):
    return (c or client()).storage.from_(BUCKET)


def public_url(path: str, c=None) -> str:
    return _bucket(c).get_public_url(path).rstrip("?")


# ── 집 PC → 클라우드 (완성본 올리기) ───────────────────────────
def push_reel(pid: str, title: str, video_path: str, caption: str = "") -> dict:
    """완성본 mp4 + 캡션을 올리고 목록(index.json)을 갱신한다."""
    c = client()
    b = _bucket(c)
    key = enc(pid)                     # 프로젝트 id 에도 한글이 들어간다
    with open(video_path, "rb") as f:
        data = f.read()
    b.upload(f"{REELS}/{key}.mp4", data,
             {"content-type": "video/mp4", "upsert": "true"})
    b.upload(f"{REELS}/{key}.txt", (caption or "").encode("utf-8"),
             {"content-type": "text/plain; charset=utf-8", "upsert": "true"})

    import time
    entry = {
        "id": pid,
        "title": title,
        "caption": caption or "",
        "video": public_url(f"{REELS}/{key}.mp4", c),
        "size_mb": round(len(data) / 1e6, 1),
        "uploaded": int(time.time()),
    }
    index = [e for e in load_index(c) if e.get("id") != pid]
    index.insert(0, entry)
    b.upload(INDEX, json.dumps(index[:50], ensure_ascii=False).encode("utf-8"),
             {"content-type": "application/json; charset=utf-8", "upsert": "true"})
    logger.info("완성본 클라우드 업로드: %s (%.1fMB)", pid, entry["size_mb"])
    return entry


def load_index(c=None) -> list[dict]:
    """완성본 목록. 없으면 빈 목록(첫 실행)."""
    try:
        raw = _bucket(c).download(INDEX)
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return []


# ── 클라우드 → 집 PC (올라온 촬영본 가져오기) ──────────────────
#
# 설계 확정(2026-09-02): 업로드는 '주제'가 아니라 **묶음(batch) + 한 줄 메모**다.
# 주제는 입력이 아니라 출력 — 집 PC에서 AI가 메모·화면을 보고 제안한다.
# 메모는 사실(메뉴명·한정 여부)과 의도의 출처라서 캡션·자막의 근거가 된다.
MEMO = "_memo.txt"


def list_inbox(c=None) -> list[dict]:
    """직원 웹앱에서 올라온 촬영본 묶음들. [{batch, memo, files, keys}]."""
    b = _bucket(c)
    out: list[dict] = []
    try:
        batches = b.list(INBOX)
    except Exception as e:
        raise CloudError(f"우편함을 읽지 못했어요: {e}") from e
    for t in batches or []:
        key = t.get("name")
        if not key or key.startswith("."):
            continue
        try:
            files = b.list(f"{INBOX}/{key}")
        except Exception:
            continue
        names = [f["name"] for f in (files or [])
                 if f.get("name") and not f["name"].startswith(".")]
        media = [n for n in names if n != MEMO]
        if not media:
            continue
        memo = ""
        if MEMO in names:
            try:
                memo = b.download(f"{INBOX}/{key}/{MEMO}").decode("utf-8")
            except Exception:
                pass
        out.append({
            "batch": key,
            "memo": memo,
            "files": [dec(n) for n in media],   # 사람이 읽을 파일명
            "keys": media,                       # 스토리지 실제 키
        })
    return out


def _folder_name(memo: str) -> str:
    """메모 첫 줄로 소재 폴더 이름을 만든다. 비어 있으면 날짜로."""
    import re
    line = memo.splitlines()[0] if memo else ""
    name = re.sub(r"[^\w가-힣 .-]", " ", line)
    name = re.sub(r"\s+", " ", name).strip(" .")[:24].strip(" .")
    if not name:
        from datetime import datetime
        name = "새소재 " + datetime.now().strftime("%m%d-%H%M")
    return name


def pull_inbox(dest_root: str, *, delete: bool = True) -> dict:
    """우편함의 묶음을 소재 폴더로 내려받는다. 받은 것은 지운다(중복 방지).

    폴더 이름은 메모 첫 줄에서 만들고, 메모 전문은 `촬영메모.txt` 로 같이
    저장한다 — 기획·자막 AI 가 이 메모를 사실의 출처로 쓴다.
    """
    c = client()
    b = _bucket(c)
    got, topics = 0, []
    for item in list_inbox(c):
        folder_name = _folder_name(item["memo"])
        folder = os.path.join(dest_root, folder_name)
        os.makedirs(folder, exist_ok=True)
        done = []
        for name, fkey in zip(item["files"], item["keys"]):
            key = f"{INBOX}/{item['batch']}/{fkey}"
            try:
                data = b.download(key)
            except Exception as e:
                logger.warning("내려받기 실패 %s: %s", key, e)
                continue
            with open(os.path.join(folder, os.path.basename(name)), "wb") as f:
                f.write(data)
            done.append(key)
            got += 1
        if done:
            if item["memo"]:
                with open(os.path.join(folder, "촬영메모.txt"), "w",
                          encoding="utf-8") as f:
                    f.write(item["memo"])
            topics.append(folder_name)
            if delete:
                try:
                    b.remove(done + [f"{INBOX}/{item['batch']}/{MEMO}"])
                except Exception:
                    logger.warning("우편함 정리 실패(무시): %s", item["batch"])
    return {"files": got, "topics": topics}
