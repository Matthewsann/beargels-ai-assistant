"""블로그 사진함 — 사진을 읽고, AI가 무엇인지 알아보고, 글에 맞는 걸 골라준다.

왜 필요한가:
    지금까지 초안은 본문에 `[📷 사진: 크림치즈 바르는 장면]` 이라는 **글자만**
    남겼다. 사장님이 네이버 에디터를 열어 그 자리마다 사진을 직접 찾아 넣어야
    글이 완성됐고, 그래서 아무도 안 썼다. 이 모듈은 그 자리표시자에 넣을
    **실제 사진 파일**을 골라주는 일을 한다.

읽는 곳은 딱 하나 — 드라이브의 `베어글스_블로그_사진함` (사장님 확정 2026-08-27).
규칙이 하나여야 폰에서 올릴 때 헷갈리지 않는다. 사진함은 PC에 자동 동기화되므로
구글 API·로그인 없이 그냥 폴더로 읽는다.

    py worker/blog_media.py            새로 들어온 사진만 AI 태깅 + 요약
    py worker/blog_media.py --all      전부 다시 태깅
    py worker/blog_media.py --list     인덱스 내용 보기

파일 이름은 아무래도 된다(IMG_5946.JPG 여도 됨) — AI가 사진을 **직접 보고**
무엇이 찍혔는지 적어 두기 때문이다. 그 기록이 data/blog_media_index.json 이다.
"""
from __future__ import annotations

import io
import json
import logging
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

logger = logging.getLogger(__name__)

# 소재 창고 = 드라이브 '콘텐츠 생성 > 원본소재' — **전 채널 공용** 단일 지점
# (사장님 확정 2026-08-28). 예전 '베어글스_블로그_사진함'은 여기로 흡수했다.
# 하위 폴더 하나 = 주제(콘텐츠) 하나. '_'로 시작하는 폴더는 상시 소재
# (_상시_메뉴 등 — 계속 재활용하는 대표컷), 주제 폴더 안 '_클립'은 편집 파생물.
DEFAULT_SHELF = (
    r"C:\Users\명구\Google Drive\1. Project_현재진행하는일\1. Business"
    r"\베어글스_송도_타임스페이스\오픈후\콘텐츠 생성\원본소재"
)
# 사이클 끝난 주제 폴더가 옮겨지는 곳(드라이브 정리). 스캔 대상 아님.
ARCHIVE_DIR = (
    r"C:\Users\명구\Google Drive\1. Project_현재진행하는일\1. Business"
    r"\베어글스_송도_타임스페이스\오픈후\콘텐츠 생성\보관"
)
INDEX_PATH = ROOT / "data" / "blog_media_index.json"
# 사용완료로 옮긴 것들의 기록. 영상은 "원본의 어느 구간을 썼는지"가 여기 남아,
# 같은 원본으로 다음 클립을 만들 때 그 구간을 피한다(다르게 편집하면 재사용 OK
# — 사장님 확정 2026-08-28).
USED_LOG = ROOT / "data" / "blog_used_log.json"
# 네이버에 올릴 용도로 변환해 둔 사진(회전·크기·HEIC 처리 완료본).
# 드라이브가 아니라 PC 안에 둔다 — 동기화 용량을 잡아먹지 않게.
CACHE_DIR = ROOT / "data" / "blog_media_cache"

PHOTO_EXT = {".jpg", ".jpeg", ".png", ".heic", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v"}
SKIP_NAMES = {"desktop.ini"}
# (구) 사용완료 폴더 이동 방식의 흔적 — 지금은 파일을 옮기지 않고
# media_ledger(사용 원장)가 채널별 재사용을 막는다(사장님 확정 2026-08-28).
USED_DIR = "사용완료"

# 네이버 블로그 본문 사진 권장 폭. 이보다 크면 네이버가 어차피 줄인다.
UPLOAD_MAX_PX = int(os.getenv("BLOG_PHOTO_MAX_PX", "1600"))

# 한 번의 AI 호출에 사진 몇 장을 같이 보여줄지. 너무 많으면 설명이 뭉개진다.
BATCH = 4


def shelf_dir() -> pathlib.Path:
    return pathlib.Path(os.getenv("BLOG_MEDIA_DIR", DEFAULT_SHELF))


def _register_heif() -> None:
    """아이폰 HEIC 사진도 열 수 있게 한다(라이브러리가 있으면)."""
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:  # noqa: BLE001 — 없으면 HEIC 만 건너뛴다
        pass


# ---------------------------------------------------------------------------
# 사진함 훑기
# ---------------------------------------------------------------------------

def scan() -> list[dict]:
    """사진함 안의 사진·영상 목록. 칸(메뉴/매장/만드는과정/기타/영상)도 같이."""
    shelf = shelf_dir()
    if not shelf.exists():
        raise FileNotFoundError(f"사진함 폴더가 없어요: {shelf}")
    out = []
    for f in sorted(shelf.rglob("*")):
        if not f.is_file() or f.name in SKIP_NAMES or f.name.startswith("_"):
            continue
        rel_parts = f.relative_to(shelf).parts
        if any(part.startswith("_사진함") for part in rel_parts):
            continue
        if re.search(r" \(\d+\)$", f.stem):
            continue                     # 드라이브가 만든 중복본(IMG_1 (1).jpg)
        ext = f.suffix.lower()
        kind = "photo" if ext in PHOTO_EXT else ("video" if ext in VIDEO_EXT else None)
        if kind is None:
            continue
        rel = f.relative_to(shelf)
        st = f.stat()
        out.append({
            "rel": rel.as_posix(),
            "slot": rel.parts[0] if len(rel.parts) > 1 else "기타",
            "kind": kind,
            "size": st.st_size,
            "mtime": int(st.st_mtime),
        })
    return out


# 사장님이 글 화면에서 폰으로 올린 사진(2026-09-15). 웹(PythonAnywhere)은
# 소재함 폴더에 못 쓰므로 공개 버킷 `blogup/<글번호>/<파일>` 에 두고, 집 PC 가
# 여기로 가져온다: 원본소재/업로드/글<번호>/<파일>. 버킷 키는 ASCII 만 되므로
# 파일 이름은 웹이 ASCII 로 짓고, 한글 폴더 이름은 이쪽에서만 붙인다.
UPLOAD_DIR = "업로드"
UPLOAD_PREFIX = "blogup"


def pull_uploads() -> int:
    """버킷 blogup/ 의 사진을 소재함 업로드/ 로 내려받고 버킷에서 지운다. 받은 개수."""
    try:
        from sns_automation import cloud_sync
        b = cloud_sync._bucket()
        folders = b.list(UPLOAD_PREFIX) or []
    except Exception as e:  # noqa: BLE001 — 버킷이 안 닿아도 나머지는 돌아야 한다
        logger.warning("업로드 우편함 확인 실패: %s", str(e)[:100])
        return 0
    got = 0
    for fo in folders:
        pid = fo.get("name") or ""
        if not pid or fo.get("id"):            # 파일이면 id 가 있다 — 폴더만
            continue
        try:
            files = b.list(f"{UPLOAD_PREFIX}/{pid}") or []
        except Exception:  # noqa: BLE001
            continue
        dest = shelf_dir() / UPLOAD_DIR / f"글{pid}"
        for f in files:
            name = f.get("name") or ""
            if not name or not f.get("id"):
                continue
            key = f"{UPLOAD_PREFIX}/{pid}/{name}"
            try:
                data = b.download(key)
                dest.mkdir(parents=True, exist_ok=True)
                (dest / name).write_bytes(data)
                b.remove([key])
                got += 1
            except Exception as e:  # noqa: BLE001 — 한 장 실패가 나머지를 막지 않는다
                logger.warning("업로드 사진 가져오기 실패(%s): %s", key, str(e)[:100])
    if got:
        logger.info("업로드 사진 %d장을 소재함으로 가져옴", got)
    return got


def full_path(rel: str) -> pathlib.Path:
    """사진함 안의 실제 경로. 사용완료로 옮겨진 파일도 찾아준다.

    발행된 글의 본문은 옮기기 전 경로를 기억하고 있으므로, 그 글을 다시
    네이버에 넣을 때(재발행)도 끊기지 않아야 한다.
    """
    p = shelf_dir() / rel
    if p.exists():
        return p
    if rel.startswith(UPLOAD_DIR + "/"):
        pull_uploads()                     # 폰에서 올린 사진은 아직 버킷에만 있을 수 있다
        if p.exists():
            return p
    for base in (pathlib.Path(ARCHIVE_DIR), shelf_dir() / USED_DIR):
        if not base.exists():
            continue
        cand = base / rel
        if cand.exists():
            return cand
        # 보관은 연도 폴더(보관/2026/주제/…) 아래일 수 있다
        hits = list(base.glob(f"*/{rel}"))
        if hits:
            return hits[0]
    # 창고 재편으로 주제 폴더가 바뀐 옛 글 — 파일 이름으로 찾는다
    name = rel.rsplit("/", 1)[-1]
    hits = list(shelf_dir().rglob(name))
    return hits[0] if hits else p


def mark_used(rels: list[str], label: str = "", channel: str = "blog") -> int:
    """소재를 '이 채널이 썼다'고 원장에 기록한다. 파일은 옮기지 않는다.

    (구버전은 사용완료/ 폴더로 이동했지만, 여러 채널이 같은 창고를 쓰게 되면서
    이동은 채널 간 공유를 깨뜨린다 → 원장 방식으로 전환. 드라이브 정리는
    주제 사이클이 끝났을 때 폴더째 보관/ 으로 — scripts/archive_topic.py.)
    """
    import media_ledger
    return media_ledger.record_many(rels, channel, ref=label)


def load_index() -> dict:
    if not INDEX_PATH.exists():
        return {}
    try:
        return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_index(idx: dict) -> None:
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")


TAG_PROMPT = """너는 베어글스(인천 송도 베이글 카페)의 블로그 사진을 정리하는 사람이다.
사진 {n}장을 순서대로 보여준다. 각 사진에 대해 아래를 채워라.

- subject: 무엇이 찍혔나 (예: "잠봉뵈르 베이글 샌드위치", "매장 창가 좌석", "귤 산도 단면")
- scene: 장면 종류 — 메뉴컷 / 과정컷 / 매장컷 / 사람 / 소품 / 로고간판 / 기타
- caption: 블로그 사진 밑에 달 만한 담백한 한 줄 (과장·이모지 금지)
- keywords: 이 사진을 찾을 때 쓸 낱말 3~6개
- quality: good / soso / bad  (초점·흔들림·어두움 기준. 흐리면 bad)
- hero: 이 사진이 글 맨 위 대표사진으로 쓸 만하면 true, 아니면 false

지어내지 마라. 안 보이면 모른다고 적어라. 메뉴 이름이 확실하지 않으면
"베이글 샌드위치"처럼 보이는 대로만 적어라.

JSON 배열 하나만 출력(설명·코드블록 금지). 사진 순서와 같은 순서로 {n}개:
[{{"subject":"","scene":"","caption":"","keywords":[],"quality":"good","hero":false}}]"""


def _extract_array(text: str) -> list:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\[.*\])\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    else:
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            text = m.group(0)
    return json.loads(text)


def _tag_batch(paths: list[pathlib.Path]) -> list[dict]:
    import llm
    # 사진 설명은 무료 등급으로 충분하다 — Claude 크레딧을 아낀다.
    raw = llm.see(paths, user=TAG_PROMPT.format(n=len(paths)),
                  max_tokens=250 * len(paths) + 300, prefer="gemini")
    got = _extract_array(raw)
    if len(got) != len(paths):
        logger.warning("사진 %d장을 보냈는데 설명 %d개가 왔습니다 — 개수를 맞춥니다.",
                       len(paths), len(got))
    got = (got + [{} for _ in paths])[:len(paths)]
    return got


def build_index(force: bool = False, limit: int | None = None,
                progress=None) -> dict:
    """사진함을 훑어 새로(또는 바뀐) 사진만 AI 로 태깅해 인덱스를 갱신한다."""
    _register_heif()
    idx = {} if force else load_index()
    files = scan()
    alive = {f["rel"] for f in files}
    for gone in [k for k in idx if k not in alive]:
        del idx[gone]                    # 사진함에서 지운 사진은 인덱스에서도 뺀다

    todo = [f for f in files
            if f["kind"] == "photo"
            and (f["rel"] not in idx or idx[f["rel"]].get("mtime") != f["mtime"])]
    if limit:
        todo = todo[:limit]

    # 못 여는 파일은 묶음에 넣기 전에 걸러낸다. 예전엔 묶음(4장) 중 한 장이
    # 안 열리면 나머지 세 장까지 통째로 버렸다 — 드라이브가 스캔과 태깅 사이에
    # 파일 이름을 바꾸거나(IMG_2623.PNG → "IMG_2623 (1).PNG") 아직 내려받지
    # 않은 파일이 그렇다. 그 묶음에 있던 HEIC 3장이 매번 조용히 빠졌다
    # (2026-09-15 실측). 사라진 파일은 다음 스캔에서 새 이름으로 다시 잡힌다.
    ok = []
    for f in todo:
        path = full_path(f["rel"])
        try:
            with open(path, "rb") as fh:
                fh.read(16)
            ok.append(f)
        except OSError as e:
            logger.warning("사진을 못 열어 건너뜀(%s): %s", f["rel"], str(e)[:80])
    todo = ok

    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        paths = [full_path(f["rel"]) for f in chunk]
        if progress:
            progress(min(i + len(chunk), len(todo)), len(todo))
        try:
            tags = _tag_batch(paths)
        except Exception as e:  # noqa: BLE001 — 한 묶음 실패로 전체를 멈추지 않는다
            logger.warning("사진 태깅 실패(%s): %s", chunk[0]["rel"], str(e)[:120])
            continue
        for f, tag in zip(chunk, tags):
            idx[f["rel"]] = {**f, **{
                "subject": (tag.get("subject") or "").strip(),
                "scene": (tag.get("scene") or "").strip(),
                "caption": (tag.get("caption") or "").strip(),
                "keywords": tag.get("keywords") or [],
                "quality": (tag.get("quality") or "good").strip(),
                "hero": bool(tag.get("hero")),
            }}
        # 한 묶음 끝날 때마다 저장한다. 사진이 많으면 몇 분씩 걸리는데,
        # 중간에 AI가 한도에 걸려 멈추면 그때까지 살펴본 게 다 날아간다.
        save_index(idx)

    # 영상은 사진처럼 한 장으로 볼 수 없다 → blog_video.py 가 따로 기록한다.
    for f in files:
        if f["kind"] == "video" and f["rel"] not in idx:
            idx[f["rel"]] = {**f, "subject": "", "scene": "영상", "caption": "",
                             "keywords": [], "quality": "good", "hero": False}
    save_index(idx)
    return idx


# ---------------------------------------------------------------------------
# 글에 맞는 사진 고르기
# ---------------------------------------------------------------------------

_SCENE_HINT = {
    "메뉴컷": ("메뉴", "음식", "베이글", "샌드위치", "음료", "커피", "세트"),
    "과정컷": ("과정", "만드는", "자르", "단면", "크림", "굽", "토스팅", "바르"),
    "매장컷": ("매장", "내부", "좌석", "인테리어", "공간", "카운터", "외관", "간판"),
}


def _score(item: dict, want: str) -> float:
    """자리표시자 문구(want)와 사진 기록이 얼마나 맞는지 점수."""
    if item.get("kind") != "photo":
        return -1
    want = want.lower()
    hay = " ".join([item.get("subject", ""), item.get("caption", ""),
                    item.get("scene", ""), item.get("slot", ""),
                    " ".join(item.get("keywords") or [])]).lower()
    score = 0.0
    # 낱말 겹침 — 두 글자 이상만 센다("의","를" 같은 조각 제외)
    for w in set(re.findall(r"[가-힣a-z0-9]{2,}", want)):
        if w in hay:
            score += 2.0
    # 장면 종류가 맞으면 가산 (예: '자르는 순간' → 과정컷)
    for scene, hints in _SCENE_HINT.items():
        if any(h in want for h in hints) and scene in item.get("scene", ""):
            score += 1.5
    q = item.get("quality")
    score += {"good": 0.6, "soso": 0.0, "bad": -3.0}.get(q, 0)
    return score


def pick(want: str, index: dict | None = None, used: set | None = None,
         hero: bool = False) -> dict | None:
    """자리표시자 문구에 가장 어울리는 사진 1장. 이미 쓴 사진(used)은 피한다."""
    idx = index if index is not None else load_index()
    used = used or set()
    best, best_score = None, 0.5          # 이 점수도 못 넘으면 안 넣는 게 낫다
    for rel, item in idx.items():
        if rel in used or item.get("kind") != "photo":
            continue
        s = _score(item, want)
        if hero and item.get("hero"):
            s += 2.0
        if s > best_score:
            best, best_score = {**item, "rel": rel}, s
    return best


def pick_video(want: str = "", index: dict | None = None,
               used: set | None = None) -> dict | None:
    """사진함 '영상' 칸에서 글에 넣을 영상 1개."""
    idx = index if index is not None else load_index()
    used = used or set()
    cands = [{**v, "rel": k} for k, v in idx.items()
             if v.get("kind") == "video" and k not in used]
    if not cands:
        return None
    if want:
        cands.sort(key=lambda c: _score({**c, "kind": "photo"}, want), reverse=True)
    return cands[0]


# ---------------------------------------------------------------------------
# 글 쓰는 AI 에게 "지금 쓸 수 있는 사진" 을 보여주기
# ---------------------------------------------------------------------------
#
# 사진을 나중에 억지로 끼워 넣으면 글과 따로 논다. 그래서 초안을 쓰기 **전에**
# 사진 목록을 먼저 보여주고, AI가 있는 사진으로 글을 짜게 한다. 글 안에서는
# `[📷 P07]` 처럼 번호로 가리키고, 나중에 resolve() 가 실제 파일로 바꾼다.

def held_rels(except_post_id=None) -> dict:
    """살아있는 글(휴지통 제외)들이 본문에 쥐고 있는 사진 → {rel: 쥔 글 수}.

    원장은 **네이버에 넣은 뒤**에야 적히므로, 초안을 연달아 열 편 만들면 열 편이
    같은 사진함을 보고 같은 대표컷을 고른다(2026-09-15 실측: 매장 사진 한 장이 7편에).
    그래서 초안 단계부터 '누가 쥐고 있나'를 센다. except_post_id 는 자기 글(다시
    뽑기·사진 고르기)은 빼고 세기 위한 것.
    """
    out: dict = {}
    try:
        from database import blog_store
        rows = (blog_store.get_client().table("blog_posts")
                .select("id,body,status,published_at,created_at")
                .neq("status", "trashed").execute().data) or []
    except Exception as e:  # noqa: BLE001 — DB 가 안 닿으면 예전처럼(원장만)
        logger.warning("쥔 사진 조회 실패(무시): %s", str(e)[:100])
        return out
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=HELD_PUBLISHED_DAYS)
    for r in rows:
        if except_post_id is not None and str(r.get("id")) == str(except_post_id):
            continue
        if r.get("status") == "published":
            # 발행된 지 오래된 글의 사진은 놓아준다 — 상시(매장·메뉴) 컷이 영원히
            # 잠기면 몇 달 뒤엔 쓸 사진이 없다. 주제 사진은 어차피 원장이 막는다.
            ts = str(r.get("published_at") or r.get("created_at") or "")
            try:
                when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                if when < cutoff:
                    continue
            except ValueError:
                pass
        for m in MARK.finditer(r.get("body") or ""):
            tok = m.group(1).strip()
            if tok.lower().endswith(tuple(PHOTO_EXT | VIDEO_EXT)):
                out[tok] = out.get(tok, 0) + 1
    return out


# 사장님 지시(2026-09-15 저녁): "다음부터 만드는 초안에서는 이미지 중복 안 되게" —
# 다른 살아있는 글이 쥔 사진은 상시(매장·메뉴) 컷이라도 **절대** 다시 내놓지 않는다.
# 대신 발행된 지 이만큼 지난 글은 사진을 놓아준다(상시 컷 회전). 쓸 사진이 모자라면
# 초안 메시지가 "사진을 더 올려 달라"고 말한다 — 몰래 겹치지 않는다.
HELD_PUBLISHED_DAYS = 60
# 새 초안에 내놓을 사진이 이보다 적으면 메시지로 경고한다
THIN_POOL = 6


def catalog(index: dict | None = None, include_bad: bool = False,
            channel: str = "blog", except_post_id=None) -> dict:
    """{"P01": {사진 기록}, ...} — AI 에게 보여줄 번호표를 붙인 사진 목록.

    channel 이 이미 쓴 소재는 원장 기준으로 뺀다(같은 채널 재탕 방지).
    다른 채널이 쓴 건 남는다 — 채널 간 재사용은 허용이다.
    """
    import media_ledger
    idx = index if index is not None else load_index()
    held = held_rels(except_post_id) if channel == "blog" else {}

    def last_used(rel):
        # 채널 구분 없이 본다 — 사장님이 막고 싶은 건 '콘텐츠 1·2·3 에 같은
        # 사진이 반복해서 나오는 것'(시간축)이지 채널 간 동시 사용이 아니다
        # (2026-08-28 확정). 어제 인스타에 쓴 상시 컷은 오늘 블로그에서도 뒤로.
        us = [u.get("date", "") for u in media_ledger.uses(rel)]
        return max(us) if us else ""

    items = []
    for rel, v in idx.items():
        if v.get("kind") != "photo":
            continue
        if not include_bad and v.get("quality") == "bad":
            continue
        evergreen = (v.get("slot") or "").startswith("_")
        if not evergreen and media_ledger.used_in(rel, channel):
            continue    # 주제 소재는 채널당 1회 — 소진되면 끝(주제 단위 소진 모델)
        if held.get(rel):
            continue    # 다른 살아있는 글이 쥔 사진 — 상시 컷이라도 다시 안 내놓는다(중복 금지)
        items.append((rel, v, last_used(rel) if evergreen else ""))
    # 정렬: 상시(_*)를 앞에 두되 **안 쓴 것·오래전에 쓴 것 우선**(회전),
    # 주제 소재는 주제 이름순 + 대표사진 후보 우선.
    items.sort(key=lambda t: (0 if t[1].get("slot", "").startswith("_") else 1,
                              t[2],                       # LRU — 빈 문자열(미사용)이 맨 앞
                              t[1].get("slot", ""),
                              0 if t[1].get("hero") else 1, t[0]))
    items = [(rel, v) for rel, v, _ in items]
    out = {}
    for i, (rel, v) in enumerate(items, 1):
        out[f"P{i:02d}"] = {**v, "rel": rel}
    # 영상 후보는 **편집 클립(_클립/)만** — 원본 4K MOV(91~193MB)를 AI가
    # 그대로 골라 블로그에 통째 업로드하는 사고를 막는다(2026-08-30 감사).
    # 원본은 blog_video.py 가 구간을 골라 클립으로 만들 때만 쓰인다.
    vids = [(k, v) for k, v in idx.items()
            if v.get("kind") == "video" and "/_클립/" in f"/{k}"
            and not media_ledger.used_in(k, channel) and not held.get(k)]
    for j, (rel, v) in enumerate(vids, 1):
        out[f"V{j:02d}"] = {**v, "rel": rel}
    return out


def catalog_text(cat: dict | None = None, limit: int = 120) -> str:
    """프롬프트에 넣을 사진 목록 글. 한 줄에 사진 하나."""
    cat = cat if cat is not None else catalog()
    lines = []
    for pid, v in list(cat.items())[:limit]:
        star = " ★대표감" if v.get("hero") else ""
        what = v.get("subject") or v.get("caption") or "(설명 없음)"
        kws = ", ".join(v.get("keywords") or [])
        kind = "영상" if v.get("kind") == "video" else v.get("scene", "")
        lines.append(f"{pid} | {kind} | {what} | {kws}{star}")
    return "\n".join(lines)


# 본문 안에서 사진을 가리키는 표시.
#   AI 가 갓 쓴 초안:  [📷 P07]                 ← 목록 번호
#   창고에 저장된 글:  [📷 메뉴/잠봉뵈르.JPG]    ← 파일 경로로 굳힌 것
# 번호는 사진함에 사진이 하나만 늘어도 밀려버린다. 그래서 초안을 저장하기
# 전에 freeze_marks() 로 **경로**로 바꿔 굳힌다 — 그러면 나중에 사진함이
# 바뀌어도 그 글이 쓰던 사진은 그대로다. DB 컬럼을 새로 만들 필요도 없다.
MARK = re.compile(r"\[\s*[📷🎬]?\s*([^\[\]\n]{1,200}?)\s*\]")
_PID = re.compile(r"^(?:P|V)\d{1,3}$", re.IGNORECASE)


def _lookup(token: str, cat: dict, idx: dict) -> dict | None:
    """표시 안의 글자를 실제 사진 기록으로 바꾼다(번호든 경로든)."""
    token = token.strip()
    if _PID.match(token):
        return cat.get(token.upper())
    item = idx.get(token)
    if item:
        return {**item, "rel": token}
    # 경로가 안 맞으면 **파일 이름**으로 찾아준다. 창고 재편(사진함→원본소재,
    # 2026-08-28)으로 옛 글 본문의 '만드는과정/IMG_x.jpg' 같은 경로가 통째로
    # 어긋났는데, 예전 코드는 전체 토큰과 파일명을 비교해서 절대 못 찾았다
    # (글#1 미디어 7개가 전부 0개로 죽어 있던 원인 — 2026-08-30 감사).
    name = token.rsplit("/", 1)[-1]
    for rel, v in idx.items():
        if rel.rsplit("/", 1)[-1] == name:
            return {**v, "rel": rel}
    # 사진함에 아직 안 들어갔어도 **파일이 실제로 있으면** 쓴다 — 사장님이 글 화면에서
    # 폰으로 올린 사진(업로드/글N/…)은 다음 '사진함 훑기' 전까지 인덱스에 없다.
    # 예전엔 여기서 None 을 돌려 네이버 넣기가 그 사진을 조용히 빼먹었다(2026-09-15).
    if "/" in token and token.lower().endswith(tuple(PHOTO_EXT | VIDEO_EXT)):
        try:
            if full_path(token).is_file():
                kind = "video" if token.lower().endswith(tuple(VIDEO_EXT)) else "photo"
                return {"rel": token, "kind": kind, "slot": token.split("/", 1)[0],
                        "subject": "", "caption": "", "keywords": [],
                        "quality": "good", "hero": False}
        except Exception:  # noqa: BLE001
            pass
    return None


def freeze_marks(body: str, cat: dict | None = None) -> str:
    """`[📷 P07]` 을 `[📷 메뉴/잠봉뵈르.JPG]` 로 굳힌다(못 찾은 표시는 지운다)."""
    cat = cat if cat is not None else catalog()
    idx = load_index()

    def sub(m):
        token = m.group(1)
        item = _lookup(token, cat, idx)
        if not item:
            # 사진 표시가 아니라 그냥 대괄호 글([참고] 등)이면 건드리지 않는다
            return "" if re.match(r"\[\s*[📷🎬]", m.group(0)) else m.group(0)
        icon = "🎬" if item.get("kind") == "video" else "📷"
        return f"[{icon} {item['rel']}]"

    out = MARK.sub(sub, body)
    return re.sub(r"\n{3,}", "\n\n", out)      # 지운 자리에 빈 줄이 남지 않게


PHOTO_TARGET = 7      # 프롬프트 '7~9장(최소 6)' — 채점기가 6 미만이면 감점한다

# 사진 부탁 메모 — 사진을 못 찾은 자리에 "어떤 사진이 있으면 좋을지"를 남긴다(사장님 2026-09-15).
# 📸(U+1F4F8)는 사진 표시 📷(U+1F4F7)와 다른 글자라 사진으로 세지 않고, 네이버·채점 전엔 걷어낸다.
WISH_RE = re.compile(r"\[\s*📸\s*부탁\s*[:：]\s*([^\]\n]{1,120}?)\s*\]")


def strip_wishes(body: str) -> str:
    """부탁 메모를 뺀 본문(네이버에 넣을 때·채점할 때). 빈 줄은 정리한다."""
    out = WISH_RE.sub("", body or "")
    return re.sub(r"\n{3,}", "\n\n", out)


def wishes(body: str) -> list[str]:
    return [m.group(1).strip() for m in WISH_RE.finditer(body or "")]


def _wish_for(section_text: str, heading: str) -> str:
    """절 내용으로 '어떤 사진'이 좋을지 한 줄 짓는다(AI 없이 규칙)."""
    t = section_text
    if any(h in t for h in _SCENE_HINT["과정컷"]):
        kind = "만드는 과정이나 단면이 보이는 컷"
    elif any(h in t for h in _SCENE_HINT["매장컷"]):
        kind = "매장 안 장면(좌석·창가·카운터)"
    else:
        kind = "이 절에서 말한 메뉴의 실물 컷(위에서 내려찍은 것)"
    h = re.sub(r"^#{1,4}\s*", "", heading).strip()
    return f"[📸 부탁: 「{h[:24]}」 절에 어울리는 {kind}]"


# fill_photos 안에서는 사진·영상 표시만 센다 — MARK 는 [매장 정보] 같은 일반 대괄호도 잡아서
# 매장 정보 블록이 든 절을 "사진 있음"으로 오판했다(2026-09-15 실측).
_MEDIA_MARK = re.compile(r"\[\s*[📷🎬][^\]]*\]")


def fill_photos(body: str, target: int = PHOTO_TARGET,
                except_post_id=None) -> tuple[str, int]:
    """사진이 모자란 초안에 절(## 소제목) 내용과 어울리는 사진을 채워 넣는다. (본문, 넣은 수)

    무료 모델은 '7~9장'이라 해도 3~4장만 놓는다(2026-09-15 실측). 사진 수는 네이버
    D.I.A. 점수의 핵심이라, 사진이 없는 절마다 그 절의 글과 가장 잘 맞는 **안 쓴**
    사진(다른 글이 쥔 것 제외)을 첫 문단 뒤에 한 장씩 넣는다. 맞는 게 없으면 안 넣는다.
    """
    body = body or ""
    have = [m.group(1).strip() for m in MARK.finditer(body)
            if m.group(1).strip().lower().endswith(tuple(PHOTO_EXT | VIDEO_EXT))]
    if len(have) >= target:
        return body, 0
    cat = catalog(except_post_id=except_post_id)
    pool = {v["rel"]: v for v in cat.values() if v.get("kind") == "photo" and v["rel"] not in have}
    if not pool:
        return body, 0
    # 절 나누기: 빈 줄 기준 토막, `## ` 로 시작하는 토막이 절의 머리
    chunks = [c for c in re.split(r"\n\s*\n", body.strip()) if c.strip()]
    used = set(have)
    added = 0
    i = 0
    while i < len(chunks) and len(have) + added < target:
        if chunks[i].startswith("## "):
            # 이 절의 범위: 다음 소제목 전까지
            j = i + 1
            while j < len(chunks) and not chunks[j].startswith("## "):
                j += 1
            section = chunks[i:j]
            if not any(_MEDIA_MARK.search(c) for c in section):
                want = " ".join(section)[:400]
                best, best_s = None, 1.0             # 낱말이 하나는 겹치거나 장면이 맞아야(품질 가산 0.6 만으론 안 됨)
                for rel, v in pool.items():
                    if rel in used:
                        continue
                    s = _score(v, want)
                    if s > best_s:
                        best, best_s = rel, s
                if best:
                    # 첫 문단(소제목 다음 토막) 뒤에 넣는다. 문단이 없으면 소제목 뒤.
                    at = i + 1 if j > i + 1 else i
                    chunks.insert(at + 1, f"[📷 {best}]")
                    used.add(best)
                    added += 1
                    j += 1
            i = j
        else:
            i += 1
    # 두 번째 돌기: 아직 모자라면 긴 절(400자 이상) 끝에 한 장씩 더 — 절당 최대 2장
    i = 0
    while i < len(chunks) and len(have) + added < target:
        if chunks[i].startswith("## "):
            j = i + 1
            while j < len(chunks) and not chunks[j].startswith("## "):
                j += 1
            section = chunks[i:j]
            n_photos = sum(1 for c in section if _MEDIA_MARK.search(c))
            text = " ".join(c for c in section if not _MEDIA_MARK.search(c))
            if n_photos < 2 and len(text) >= 400:
                best, best_s = None, 1.0             # 두 번째 장도 내용이 맞을 때만
                for rel, v in pool.items():
                    if rel in used:
                        continue
                    s = _score(v, text[-400:])
                    if s > best_s:
                        best, best_s = rel, s
                if best:
                    chunks.insert(j, f"[📷 {best}]")
                    used.add(best)
                    added += 1
                    j += 1
            i = j
        else:
            i += 1
    # 그래도 모자라면(절이 적으면) 맨 앞 대표컷 — 본문이 사진으로 시작하지 않을 때만
    if len(have) + added < target and not _MEDIA_MARK.match(chunks[0] if chunks else ""):
        best, best_s = None, 0.5
        for rel, v in pool.items():
            if rel in used:
                continue
            s = _score(v, " ".join(chunks[:2])[:300]) + (2.0 if v.get("hero") else 0)
            if s > best_s:
                best, best_s = rel, s
        if best:
            chunks.insert(0, f"[📷 {best}]")
            used.add(best)
            added += 1
    # 그래도 사진이 없는 절엔 '어떤 사진이 있으면 좋을지' 부탁 메모를 남긴다(이미 있으면 안 겹침)
    wished = 0
    if len(have) + added < target:
        i = 0
        while i < len(chunks):
            if chunks[i].startswith("## "):
                j = i + 1
                while j < len(chunks) and not chunks[j].startswith("## "):
                    j += 1
                section = chunks[i:j]
                if not any(_MEDIA_MARK.search(c) or WISH_RE.search(c) for c in section):
                    text = " ".join(c for c in section[1:])
                    at = i + 1 if j > i + 1 else i
                    chunks.insert(at + 1, _wish_for(text, chunks[i]))
                    wished += 1
                    j += 1
                i = j
            else:
                i += 1
    if added or wished:
        logger.info("사진 자동 채움: %d장 → 총 %d장 · 부탁 메모 %d개", added, len(have) + added, wished)
    return "\n\n".join(chunks) + ("\n" if body.endswith("\n") else ""), added


def dedupe_marks(body: str) -> tuple[str, int]:
    """한 글 안에서 같은 사진 표시가 두 번 나오면 뒤의 것을 지운다. (본문, 지운 수)."""
    seen, dropped = set(), 0

    def sub(m):
        nonlocal dropped
        tok = m.group(1).strip()
        if not tok.lower().endswith(tuple(PHOTO_EXT | VIDEO_EXT)):
            return m.group(0)
        if tok in seen:
            dropped += 1
            return ""
        seen.add(tok)
        return m.group(0)

    out = MARK.sub(sub, body or "")
    return re.sub(r"\n{3,}", "\n\n", out), dropped


def used_media(body: str, cat: dict | None = None) -> list[dict]:
    """이 글이 쓰는 사진·영상 목록(등장 순서, 중복 없이)."""
    return resolve_body(body, cat)[1]


def resolve_body(body: str, cat: dict | None = None) -> tuple[list[dict], list[dict]]:
    """본문을 '글 토막 / 사진' 순서대로 쪼갠다.

    돌려주는 값:
        blocks — [{"type":"text","text":…} 또는 {"type":"photo","rel":…,"caption":…}] 순서대로
        media  — 이 글에 실제로 쓰인 사진·영상 목록(중복 없이, 등장 순서대로)
    없는 번호를 가리키면 그 표시는 그냥 지운다(빈 자리로 두지 않는다).
    """
    cat = cat if cat is not None else catalog()
    idx = load_index()
    blocks: list[dict] = []
    media: list[dict] = []
    seen: set[str] = set()
    pos = 0
    for m in MARK.finditer(body):
        token = m.group(1)
        item = _lookup(token, cat, idx)
        chunk = body[pos:m.start()]
        pos = m.end()
        if chunk.strip():
            blocks.append({"type": "text", "text": chunk.strip("\n")})
        if not item:
            if re.match(r"\[\s*[📷🎬]", m.group(0)):
                logger.warning("사진함에 없는 사진을 가리켰습니다: %s", token[:40])
            else:
                # 사진 표시가 아니라 그냥 대괄호 글 → 본문 그대로 살린다
                blocks.append({"type": "text", "text": m.group(0)})
            continue
        kind = "video" if item.get("kind") == "video" else "photo"
        blocks.append({"type": kind, "rel": item["rel"],
                       "caption": item.get("caption", "")})
        if item["rel"] not in seen:
            seen.add(item["rel"])
            media.append({"rel": item["rel"], "kind": kind,
                          "caption": item.get("caption", ""),
                          "subject": item.get("subject", "")})
    tail = body[pos:]
    if tail.strip():
        blocks.append({"type": "text", "text": tail.strip("\n")})
    return blocks, media


def strip_marks(body: str) -> str:
    """사진 표시를 뺀 순수 글(글자 수 세기·미리보기용)."""
    return re.sub(r"\[\s*[📷🎬][^\[\]\n]{0,200}\]", "", body)


# ---------------------------------------------------------------------------
# 네이버에 올릴 수 있는 형태로 변환
# ---------------------------------------------------------------------------

def prepare(rel: str) -> pathlib.Path:
    """사진 1장을 업로드용 JPEG 로 만들어 그 경로를 돌려준다.

    · 아이폰 세로사진이 눕는 문제(EXIF) 보정
    · HEIC → JPEG (네이버 에디터가 HEIC 를 못 받는다)
    · 긴 변 1600px 로 축소 (원본 4K 를 올리면 업로드가 느리다)
    이미 만들어 둔 게 있으면 다시 만들지 않는다.
    """
    _register_heif()
    from PIL import Image, ImageOps

    src = full_path(rel)
    st = src.stat()
    safe = re.sub(r"[^\w가-힣]+", "_", rel.rsplit(".", 1)[0])
    dest = CACHE_DIR / f"{safe}_{int(st.st_mtime)}.jpg"
    if dest.exists():
        return dest

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    img = Image.open(src)
    img = ImageOps.exif_transpose(img)
    img.thumbnail((UPLOAD_MAX_PX, UPLOAD_MAX_PX), Image.LANCZOS)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.save(dest, "JPEG", quality=88, optimize=True)
    return dest


# ---------------------------------------------------------------------------
# 웹에서 볼 작은 미리보기(썸네일)
# ---------------------------------------------------------------------------
# 사진은 집 PC(드라이브 동기화 폴더)에만 있고 직원 웹은 PythonAnywhere 에 있다.
# 그래서 웹은 지금까지 '어떤 파일이 들어가는지' 이름만 보여줬다 — 사장님은
# 파일명만 보고 어떤 사진인지 알 수 없다(2026-09-07 요청).
#
# 새 표를 만들지 않는다: 이미 있는 **공개 버킷 sns-media** 를 우편함으로 쓴다
# (인스타 완성본이 쓰는 그 버킷). 키는 rel 경로의 해시라 ASCII 이고, 웹이
# 같은 해시를 계산하면 URL 이 나온다 — 주고받을 목록이 따로 필요 없다.
THUMB_PREFIX = "blogthumbs"
THUMB_PX = 640                      # 글 화면이 본문 안에 사진을 통째로 보여준다(2026-09-14). 40~80KB.
THUMB_STATE = ROOT / "data" / "blog_thumbs.json"


def thumb_key(rel: str) -> str:
    """rel 경로 → 스토리지 키(웹도 똑같이 계산한다)."""
    import hashlib
    return hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16] + ".jpg"


def _video_frame(src: pathlib.Path) -> bytes | None:
    """영상은 1초 지점 한 장을 뽑아 미리보기로 쓴다."""
    import subprocess
    import tempfile
    try:
        import blog_video
        exe = blog_video.ffmpeg_exe()
    except Exception as e:  # noqa: BLE001 — ffmpeg 이 없어도 사진은 되게
        logger.warning("ffmpeg 없음(영상 미리보기 건너뜀): %s", str(e)[:80])
        return None
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / "f.jpg"
        subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-y",
                        "-ss", "1", "-i", str(src), "-frames:v", "1",
                        "-vf", f"scale={THUMB_PX}:-2", str(out)],
                       capture_output=True, timeout=60)
        return out.read_bytes() if out.exists() else None


def _thumb_bytes(rel: str) -> bytes | None:
    """사진·영상 한 개의 작은 JPEG. 못 만들면 None."""
    import io
    src = full_path(rel)
    if src.suffix.lower() in VIDEO_EXT:
        return _video_frame(src)
    _register_heif()
    from PIL import Image, ImageOps
    img = Image.open(src)
    img = ImageOps.exif_transpose(img)
    img.thumbnail((THUMB_PX, THUMB_PX), Image.LANCZOS)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=72, optimize=True)
    return buf.getvalue()


def ensure_thumbs(rels) -> int:
    """이 글이 쓰는 사진들의 미리보기를 공개 버킷에 올린다(이미 있으면 건너뜀).

    돌려주는 값: 이번에 새로 올린 개수. 실패해도 예외를 밖으로 내보내지
    않는다 — 미리보기는 덤이고, 글 저장이 먼저다.
    """
    import json
    names = []
    for r in rels or []:
        rel = r.get("rel") if isinstance(r, dict) else r
        if rel and rel not in names:
            names.append(rel)
    if not names:
        return 0
    try:
        done = json.loads(THUMB_STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — 기록이 없으면 처음부터
        done = {}
    made = 0
    try:
        from sns_automation import cloud_sync
        bucket = cloud_sync._bucket()
    except Exception as e:  # noqa: BLE001
        logger.warning("미리보기 업로드 준비 실패: %s", str(e)[:120])
        return 0
    for rel in names:
        key = thumb_key(rel)
        try:
            stamp = int(full_path(rel).stat().st_mtime)
        except Exception:  # noqa: BLE001 — 파일이 없으면(보관 이동 등) 건너뛴다
            continue
        if done.get(key) == f"{stamp}:{THUMB_PX}":   # 크기를 바꾸면 다시 만든다
            continue
        try:
            data = _thumb_bytes(rel)
            if not data:
                continue
            bucket.upload(f"{THUMB_PREFIX}/{key}", data,
                          {"content-type": "image/jpeg", "upsert": "true"})
            done[key] = f"{stamp}:{THUMB_PX}"
            made += 1
        except Exception as e:  # noqa: BLE001 — 한 장 실패가 나머지를 막지 않는다
            logger.warning("미리보기 실패(%s): %s", rel, str(e)[:100])
    if made:
        try:
            THUMB_STATE.parent.mkdir(parents=True, exist_ok=True)
            THUMB_STATE.write_text(json.dumps(done, indent=1), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            logger.warning("미리보기 기록 저장 실패: %s", str(e)[:100])
        logger.info("사진 미리보기 %d장 올림", made)
    return made


# 웹의 ③ '사진 선택'이 읽는 목록. 사진함(인덱스)은 집 PC 파일이라 웹이 못 본다.
# 그래서 사진함을 훑거나 글을 네이버에 넣을 때마다 "지금 블로그가 쓸 수 있는
# 사진" 목록을 공개 버킷에 올려 둔다(briefs·ideas 와 같은 state/ 우편함 방식).
CATALOG_KEY = "state/blog_catalog.json"


def publish_catalog() -> int:
    """카탈로그(이미 쓴 소재 제외)를 미리보기와 함께 버킷에 올린다. 올린 개수."""
    import json
    import time
    try:
        cat = catalog()
    except Exception as e:  # noqa: BLE001
        logger.warning("카탈로그 발행 실패(목록): %s", str(e)[:120])
        return 0
    items = []
    for _pid, v in cat.items():
        rel = v.get("rel")
        if not rel:
            continue
        items.append({
            "rel": rel, "slot": v.get("slot") or "", "kind": v.get("kind") or "photo",
            "subject": v.get("subject") or v.get("caption") or "",
            "scene": v.get("scene") or "", "hero": bool(v.get("hero")),
            "key": thumb_key(rel),
        })
    ensure_thumbs([i["rel"] for i in items])
    try:
        from sns_automation import cloud_sync
        cloud_sync._bucket().upload(
            CATALOG_KEY,
            json.dumps({"updated": int(time.time()), "items": items},
                       ensure_ascii=False).encode("utf-8"),
            {"content-type": "application/json; charset=utf-8", "upsert": "true"})
    except Exception as e:  # noqa: BLE001
        logger.warning("카탈로그 발행 실패(업로드): %s", str(e)[:120])
        return 0
    logger.info("사진 목록 발행: %d개", len(items))
    return len(items)


# ---------------------------------------------------------------------------
# 손으로 돌려보기
# ---------------------------------------------------------------------------

def _summary(idx: dict) -> str:
    from collections import Counter
    photos = [v for v in idx.values() if v.get("kind") == "photo"]
    videos = [v for v in idx.values() if v.get("kind") == "video"]
    slots = Counter(v.get("slot") for v in photos)
    qual = Counter(v.get("quality") for v in photos)
    heroes = [v for v in photos if v.get("hero")]
    return "\n".join([
        f"사진 {len(photos)}장 · 영상 {len(videos)}개",
        "  칸별: " + ", ".join(f"{k} {n}" for k, n in slots.items()),
        "  화질: " + ", ".join(f"{k} {n}" for k, n in qual.items()),
        f"  대표사진 후보: {len(heroes)}장",
    ])


def main() -> None:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    args = sys.argv[1:]
    if "--list" in args:
        idx = load_index()
        for rel, v in sorted(idx.items()):
            mark = "★" if v.get("hero") else " "
            print(f"{mark} [{v.get('slot', '')}/{v.get('scene', '')}] {rel}\n"
                  f"    {v.get('subject', '')} — {v.get('caption', '')}")
        print("\n" + _summary(idx))
        return

    def show(done, total):
        print(f"  … {done}/{total}장 살펴보는 중", flush=True)

    print(f"사진함: {shelf_dir()}")
    idx = build_index(force="--all" in args, progress=show)
    print("\n" + _summary(idx))
    print(f"\n인덱스 저장: {INDEX_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()


def used_segments(source_name: str) -> list[tuple[float, float]]:
    """이 원본 영상에서 이미 쓴 구간들(전 채널). 다음 편집은 여길 피한다."""
    import media_ledger
    out = []
    # 원장은 rel 단위 — 파일 이름이 source 인 항목을 전부 훑는다
    for rel, us in media_ledger._load().items():
        if rel.rsplit("/", 1)[-1] not in (source_name,
                                          source_name.replace(".MOV", ".mp4")):
            continue
        for u in us:
            if u.get("segment"):
                out.append((u["segment"][0], u["segment"][1]))
    # 구 기록(blog_used_log)도 함께 본다
    if USED_LOG.exists():
        try:
            for e in json.loads(USED_LOG.read_text(encoding="utf-8")):
                if e.get("kind") == "video" and e.get("source") == source_name \
                        and e.get("start") is not None:
                    out.append((float(e["start"]), float(e["end"])))
        except Exception:  # noqa: BLE001
            pass
    return out