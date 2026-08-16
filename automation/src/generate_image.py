"""
이미지(+영상 계획) 생성 모듈.

본문의 [📷 사진: ...] 표시를 읽어 두 가지를 만듭니다.
1) shotlist.md — 사장님이 폰으로 직접 찍을 '실물 사진/영상 촬영 목록'
   (네이버 상위 노출은 직접 촬영 실물 사진을 가장 우대 → 음식 사진은 실사진 권장)
2) AI 이미지 — 썸네일/배경/일러스트/이벤트 포스터 등 보조 이미지
   provider 설정에 따라 실제 생성하거나(=openai), 프롬프트만 저장(=none).

config.yaml 예:
  images:
    provider: none        # none | openai
    per_post_ai: 1        # AI 이미지 개수 (썸네일 등 보조용)

영상: 자동 생성은 비용·품질·정책상 권장하지 않습니다. 대신 shotlist 에
      '5~10초 짧은 영상' 아이디어를 넣어 폰 촬영을 안내합니다.
"""

from __future__ import annotations

import os
import pathlib
import re

import library

MARKER = re.compile(r"\[📷\s*사진:\s*(.+?)\]")


def extract_shots(body: str) -> list[str]:
    return [m.strip() for m in MARKER.findall(body)]


def write_shotlist(out_dir: pathlib.Path, title: str, shots: list[str]) -> None:
    lines = [f"# 촬영 목록 — {title}", "",
             "> 네이버 상위 노출은 **직접 찍은 실물 사진**을 가장 우대합니다.",
             "> 아래를 폰으로 찍어 임시저장 글의 [📷 사진] 위치에 넣어주세요. (7~10장 권장)", ""]
    for i, s in enumerate(shots, 1):
        lines.append(f"{i}. {s}")
    lines += ["", "## 🎬 짧은 영상(선택, 체류시간↑)",
              "- 5~10초면 충분합니다. 예: 그릴에서 베이글 굽는 순간 / 크림치즈 바르기 / "
              "샌드위치 조립 / 커피 라떼아트. (※ 반죽·오븐 장면은 우리 매장에 없음)",
              "- 폰 세로 영상으로 찍어 글 중간에 하나만 넣어도 가점에 유리합니다."]
    (out_dir / "shotlist.md").write_text("\n".join(lines), encoding="utf-8")


def ai_image_prompts(title: str, keyword: str, n: int) -> list[str]:
    """보조 AI 이미지용 프롬프트(썸네일/배경/일러스트). 음식 실물 대체가 아님."""
    base = (
        f"베어글스 베이커리 카페 블로그용 보조 이미지. 주제: {title}. 키워드: {keyword}. "
        "곰(bear) 캐릭터 친근한 무드, 따뜻한 베이커리 감성, 텍스트 없는 배경/일러스트 스타일."
    )
    variants = ["아늑한 베이커리 배경", "곰돌이 캐릭터 일러스트 썸네일", "베이글·커피 플랫레이 무드컷"]
    return [f"{base} 세부: {variants[i % len(variants)]}" for i in range(n)]


def _try_openai_images(prompts: list[str], out_dir: pathlib.Path) -> int:
    """OPENAI_API_KEY + openai 패키지가 있을 때만 실제 생성. 없으면 0 반환."""
    if not os.getenv("OPENAI_API_KEY"):
        return 0
    try:
        from openai import OpenAI  # 선택 의존성
        import base64
    except Exception:
        print("    (openai 패키지 없음 → AI 이미지는 프롬프트만 저장)")
        return 0
    client = OpenAI()
    made = 0
    for i, p in enumerate(prompts, 1):
        try:
            res = client.images.generate(model="gpt-image-1", prompt=p, size="1024x1024")
            img_b64 = res.data[0].b64_json
            (out_dir / f"ai_{i}.png").write_bytes(base64.b64decode(img_b64))
            made += 1
        except Exception as e:
            print(f"    AI 이미지 {i} 실패: {e}")
    return made


def process_item(cfg: dict, item_id: int) -> None:
    post = library.load_post(item_id)
    d = library.item_dir(item_id)
    imgs = d / "images"
    imgs.mkdir(parents=True, exist_ok=True)

    title = post.get("title", "")
    keyword = post.get("main_keyword", "")
    shots = extract_shots(post.get("body", "")) or ["대표 메뉴 정면 컷", "매장 분위기 컷"]
    write_shotlist(imgs, title, shots)

    img_cfg = cfg.get("images", {})
    n = int(img_cfg.get("per_post_ai", 1))
    prompts = ai_image_prompts(title, keyword, n)
    (imgs / "ai_prompts.txt").write_text("\n\n".join(prompts), encoding="utf-8")

    provider = img_cfg.get("provider", "none")
    made = _try_openai_images(prompts, imgs) if provider == "openai" else 0
    tag = f"AI이미지 {made}장 생성" if made else "AI이미지 프롬프트만 저장"
    print(f"    🖼  #{item_id:04d}: 촬영목록 {len(shots)}컷 + {tag}")


if __name__ == "__main__":
    import sys
    import generate_post
    cfg = generate_post.load_config()
    ids = [int(x) for x in sys.argv[1:]] or [m["id"] for m in library.list_items()]
    for i in ids:
        process_item(cfg, i)
