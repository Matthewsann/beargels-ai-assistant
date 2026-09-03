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
def push_reel(pid: str, title: str, video_path: str, caption: str = "",
              script: dict | None = None) -> dict:
    """완성본 mp4 + 캡션을 올리고 목록(index.json)을 갱신한다.

    script: {hook, cta, captions[]} — 완성본 카드의 '자막 고치기'가 보여줄
    텍스트. 사장님이 틀린 단어(산딸기→자몽)를 발견하면 그 자리에서 고쳐
    다시 만들 수 있게 한다.
    """
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
        "script": script or {},
        "video": public_url(f"{REELS}/{key}.mp4", c),
        "size_mb": round(len(data) / 1e6, 1),
        "uploaded": int(time.time()),
    }
    # 같은 pid 의 옛 항목은 통째로 바꾼다 — 발행 표시(published_at·좋아요)도 함께
    # 사라지는데, 이건 의도다: 다시 만든 완성본은 '새 판'이라 아직 안 올린 것이다
    # (publish_sync.new_version 이 project.json 쪽도 같은 규칙으로 되돌린다).
    index = [e for e in load_index(c) if e.get("id") != pid]
    index.insert(0, entry)
    _save_index(index, c)
    logger.info("완성본 클라우드 업로드: %s (%.1fMB)", pid, entry["size_mb"])
    return entry


def _save_index(index: list[dict], c=None) -> None:
    _bucket(c).upload(INDEX, json.dumps(index[:50], ensure_ascii=False).encode("utf-8"),
                      {"content-type": "application/json; charset=utf-8", "upsert": "true"})


def load_index(c=None) -> list[dict]:
    """완성본 목록. 없으면 빈 목록(첫 실행)."""
    try:
        raw = _bucket(c).download(INDEX)
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return []


def mark_published(pid: str, at: int, url: str | None = None, likes=None,
                   comments=None, reach=None, source: str | None = None, c=None) -> bool:
    """완성본 카드에 '✅ 올렸어요' 와 성과 숫자를 붙인다(집 PC 만 쓴다 — index.json
    의 쓰는 쪽은 집 PC 하나여야 서로 덮어쓰지 않는다). 반환: 목록에 있었는가.

    source: 'manual' | 'auto' — 카드의 되돌리기 문구가 갈린다(자동 감지면 '이 게시물이 아니에요').
    """
    c = c or client()
    index = load_index(c)
    hit = False
    for e in index:
        if e.get("id") != pid:
            continue
        e["published_at"] = int(at)
        if url:
            e["permalink"] = url
        if source:
            e["published_source"] = source
        for k, v in (("likes", likes), ("comments", comments), ("reach", reach)):
            if v is not None:
                e[k] = v
        hit = True
    if hit:
        _save_index(index, c)
    return hit


def unmark_published(pid: str, c=None) -> bool:
    """[잘못 눌렀어요] — 카드의 ✅ 와 성과 숫자를 뗀다. 반환: 목록에 있었는가."""
    c = c or client()
    index = load_index(c)
    hit = False
    for e in index:
        if e.get("id") != pid:
            continue
        for k in ("published_at", "permalink", "published_source", "likes", "comments", "reach"):
            e.pop(k, None)
        hit = True
    if hit:
        _save_index(index, c)
    return hit


# ── 대본 검수함 (영상 만들기 전 사람 게이트) ──────────────────
# 사장님 지적(2026-09-02): 메모가 틀리면(산딸기 vs 자몽) AI 는 그대로 쓴다.
# 그래서 영상 전에 대본(훅·자막·캡션)을 웹에서 검수·수정하는 단계를 둔다.
SCRIPTS = "state/scripts.json"


def load_scripts(c=None) -> list[dict]:
    """검수 대기 중인 대본 목록."""
    try:
        raw = _bucket(c).download(SCRIPTS)
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return []


def _save_scripts(items: list[dict], c=None) -> None:
    _bucket(c).upload(SCRIPTS,
                      json.dumps(items[:20], ensure_ascii=False).encode("utf-8"),
                      {"content-type": "application/json; charset=utf-8",
                       "upsert": "true"})


def push_script(entry: dict) -> None:
    """대본 하나를 검수함에 넣는다(같은 프로젝트는 교체)."""
    c = client()
    items = [s for s in load_scripts(c) if s.get("pid") != entry.get("pid")]
    items.insert(0, entry)
    _save_scripts(items, c)


def remove_script(pid: str) -> None:
    """영상까지 만들어졌으면 검수함에서 뺀다."""
    c = client()
    _save_scripts([s for s in load_scripts(c) if s.get("pid") != pid], c)


# ── 촬영 아이디어함 (MKT 파트너의 '먼저 제안') ─────────────────
IDEAS = "state/ideas.json"


def load_ideas(c=None) -> dict:
    try:
        raw = _bucket(c).download(IDEAS)
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def push_ideas(ideas: list[dict], *, source: str = "weekly") -> None:
    """촬영 아이디어를 올린다. source='ref' 는 레퍼런스 기반(앞에 붙임)."""
    import time
    c = client()
    cur = load_ideas(c).get("ideas") or []
    for i in ideas:
        i["source"] = source
        i["created"] = int(time.time())
    items = (ideas + cur) if source == "ref" else ideas + [
        x for x in cur if x.get("source") == "ref"]
    _bucket(c).upload(IDEAS, json.dumps(
        {"updated": int(time.time()), "ideas": items[:6]},
        ensure_ascii=False).encode("utf-8"),
        {"content-type": "application/json; charset=utf-8", "upsert": "true"})


# ── 파이프라인 미러 (PA 웹사이트에서 세부 편집) ────────────────
# 사장님 확정(2026-09-03): 파이프라인 화면을 PA 웹사이트 안에서 쓴다.
# 집 PC 가 구성표·썸네일·미리보기를 여기로 올리고, PA 는 보여주고
# 수정·버튼을 잡 큐로 되돌려보낸다. 영상 파일·렌더만 집 PC 몫.
PIPE = "state/pipeline.json"


def load_pipe(c=None) -> dict:
    try:
        raw = _bucket(c).download(PIPE)
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def push_pipe(data: dict) -> None:
    _bucket(client()).upload(
        PIPE, json.dumps(data, ensure_ascii=False).encode("utf-8"),
        {"content-type": "application/json; charset=utf-8", "upsert": "true"})


def push_preview(pid: str, video_path: str) -> str:
    """편집 미리보기 mp4 업로드 → 공개 URL (완성 전 확인용)."""
    c = client()
    path = f"state/previews/{enc(pid)}.mp4"
    with open(video_path, "rb") as f:
        _bucket(c).upload(path, f.read(),
                          {"content-type": "video/mp4", "upsert": "true"})
    return public_url(path, c)


def push_script_thumbs(pid: str, jpegs: list[bytes]) -> list[str]:
    """대본 검수용 샷 썸네일 업로드 → 공개 URL 목록(샷 순서 유지).

    자막만 보고는 산딸기인지 자몽인지 알 수 없다(사장님 지적 2026-09-02) —
    검수 화면에 그 샷의 실제 화면을 같이 보여주기 위한 것. 빈 항목은 "" 유지.
    """
    c = client()
    b = _bucket(c)
    key = enc(pid)
    urls: list[str] = []
    for i, data in enumerate(jpegs):
        if not data:
            urls.append("")
            continue
        path = f"state/thumbs/{key}/{i}.jpg"
        b.upload(path, data, {"content-type": "image/jpeg", "upsert": "true"})
        urls.append(public_url(path, c))
    return urls


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
