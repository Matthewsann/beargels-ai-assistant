# -*- coding: utf-8 -*-
"""블로그 본문 → 에디터 블록(build_blocks) — 인용구·강조·스티커·지도·소제목 순서(사장님 2026-09-23)."""
import sys, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "worker", ROOT / "webapp"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import blog_jobs  # noqa: E402


BODY = """[📸 부탁: 대표 컷] (팁: 정면)

오늘 매장에서는 아침부터 반죽을 올렸어요.
송도 베이글 손님들이 많이 오셨어요.

> 한 줄 요약 인용

## 첫 절 소제목

첫 절 본문 한 줄.
**첫 절 핵심 문장입니다**
둘째 줄.

---

## 둘째 절 소제목

둘째 절 본문.

[매장 정보]
상호: 베어글스
주소: 송도

#송도베이글 #베어글스
"""


def _types(blocks):
    return [(b["type"], b.get("style")) for b in blocks]


def test_build_blocks_emits_quote_emph_sticker_map_in_order(monkeypatch):
    import blog_media
    monkeypatch.setattr(blog_media, "resolve_body", lambda body: ([{"type": "text", "text": blog_media.strip_wishes(body)}], []))
    blocks, n = blog_jobs.build_blocks(BODY, main_keyword="송도 베이글")
    kinds = _types(blocks)
    assert n == 0
    assert ("quote", None) in kinds
    q = next(b for b in blocks if b["type"] == "quote")
    assert q["text"] == "한 줄 요약 인용"
    heads = [b["text"] for b in blocks if b.get("style") == "heading"]
    assert heads == ["첫 절 소제목", "둘째 절 소제목"]
    emph = [b for b in blocks if b.get("style") == "emph"]
    assert len(emph) == 1 and emph[0]["text"] == "첫 절 핵심 문장입니다" and "**" not in emph[0]["text"]
    # 스티커: 도입 절 끝(첫 소제목 앞)·첫 절 끝(둘째 소제목 앞)·둘째 절 끝([매장 정보] 앞) = 3, 지도는 정보 블록 뒤 1
    assert sum(1 for b in blocks if b["type"] == "sticker") == 3
    i_info = next(i for i, b in enumerate(blocks) if b["type"] == "text" and b["text"].startswith("[매장 정보]"))
    assert blocks[i_info + 1]["type"] == "map"
    assert blocks[i_info - 1]["type"] == "sticker"
    # 첫 소제목 바로 앞이 스티커, 그 앞이 인용
    i_h = next(i for i, b in enumerate(blocks) if b.get("style") == "heading")
    assert blocks[i_h - 1]["type"] == "sticker"
    assert ("divider", None) in kinds
    # 해시태그는 그냥 본문(마지막 블록)
    assert blocks[-1]["type"] == "text" and blocks[-1]["text"].startswith("#")
    # 한 줄 = 한 문단이 유지된다(줄바꿈 보존)
    first = next(b for b in blocks if b["type"] == "text" and "반죽" in b["text"])
    assert "\n" in first["text"]


def test_sticker_cap_and_no_sticker_for_empty_section(monkeypatch):
    import blog_media
    monkeypatch.setattr(blog_media, "resolve_body", lambda body: ([{"type": "text", "text": body}], []))
    body = "\n".join(f"## 절 {k}\n본문 {k}\n" for k in range(9))
    blocks, _ = blog_jobs.build_blocks(body)
    assert sum(1 for b in blocks if b["type"] == "sticker") == blog_jobs.STICKER_MAX
    body2 = "## 절 하나\n## 절 둘\n본문"
    blocks2, _ = blog_jobs.build_blocks(body2)
    assert sum(1 for b in blocks2 if b["type"] == "sticker") == 0     # 빈 절 뒤엔 스티커가 없다


def test_wish_photos_tops_up_to_photo_min():
    import blog_media
    body = "도입 문장.\n\n## 절 하나\n" + "가나다라마바사 " * 30 + "\n\n## 절 둘\n" + "본문 " * 20
    out, added = blog_media.wish_photos(body)
    assert len(blog_media.wishes(out)) >= blog_media.PHOTO_MIN
    assert len(set(blog_media.wishes(out))) == len(blog_media.wishes(out))   # 같은 부탁을 두 번 시키지 않는다


def test_web_render_quote_and_emph():
    sys.path.insert(0, str(ROOT / "service"))
    import importlib
    app = importlib.import_module("app")
    blocks = app._blog_render("문단.\n\n> 한 줄 인용\n\n**핵심 줄**\n\n## 소제목")
    assert [b["t"] for b in blocks] == ["p", "quote", "p", "h"]
    assert blocks[1]["text"] == "한 줄 인용"
    html = str(app._emph("보통 줄\n**핵심 줄**"))
    assert "fff593" in html and "**" not in html and "보통 줄" in html
