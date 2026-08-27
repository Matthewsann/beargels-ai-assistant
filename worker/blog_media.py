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

DEFAULT_SHELF = (
    r"C:\Users\명구\Google Drive\1. Project_현재진행하는일\1. Business"
    r"\베어글스_송도_타임스페이스\오픈후\브랜딩\베어글스_블로그_사진함"
)
INDEX_PATH = ROOT / "data" / "blog_media_index.json"
# 네이버에 올릴 용도로 변환해 둔 사진(회전·크기·HEIC 처리 완료본).
# 드라이브가 아니라 PC 안에 둔다 — 동기화 용량을 잡아먹지 않게.
CACHE_DIR = ROOT / "data" / "blog_media_cache"

PHOTO_EXT = {".jpg", ".jpeg", ".png", ".heic", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v"}
SKIP_NAMES = {"desktop.ini"}

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


def full_path(rel: str) -> pathlib.Path:
    return shelf_dir() / rel


# ---------------------------------------------------------------------------
# 인덱스(= AI 가 사진을 보고 적어 둔 기록)
# ---------------------------------------------------------------------------

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

def catalog(index: dict | None = None, include_bad: bool = False) -> dict:
    """{"P01": {사진 기록}, ...} — AI 에게 보여줄 번호표를 붙인 사진 목록."""
    idx = index if index is not None else load_index()
    items = [(rel, v) for rel, v in idx.items()
             if v.get("kind") == "photo" and (include_bad or v.get("quality") != "bad")]
    # 메뉴 → 만드는과정 → 매장 → 기타 순으로, 대표사진 후보를 앞에
    order = {"메뉴": 0, "만드는과정": 1, "매장": 2, "기타": 3, "영상": 4}
    items.sort(key=lambda kv: (order.get(kv[1].get("slot"), 9),
                               0 if kv[1].get("hero") else 1, kv[0]))
    out = {}
    for i, (rel, v) in enumerate(items, 1):
        out[f"P{i:02d}"] = {**v, "rel": rel}
    for j, (rel, v) in enumerate([(k, v) for k, v in idx.items()
                                  if v.get("kind") == "video"], 1):
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
    for rel, v in idx.items():          # 파일 이름만 적혀 있을 때도 찾아준다
        if rel.rsplit("/", 1)[-1] == token:
            return {**v, "rel": rel}
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
