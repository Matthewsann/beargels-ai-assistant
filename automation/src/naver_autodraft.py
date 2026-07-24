"""
네이버 블로그 '임시저장' 자동화 (크롬 직접 구동 방식).

네이버는 개인 블로그 글쓰기 공식 API 가 없으므로, 켜둔 PC의 크롬으로
스마트에디터에 직접 제목/본문을 입력하고 '임시저장' 버튼까지 눌러줍니다.
발행(게시)은 사장님이 사진 확인 후 직접 하시는 것을 기본으로 합니다.

사용법
------
1) 처음 한 번, 전용 프로필로 네이버에 로그인 (세션 저장):
     python src/naver_autodraft.py --login

2) 생성된 초안 하나를 임시저장:
     python src/naver_autodraft.py --post posts/01-신메뉴.json

3) posts/ 의 모든 초안을 임시저장:
     python src/naver_autodraft.py --all

4) 화면 구조(선택자)가 안 맞을 때 직접 확인/튜닝:
     python src/naver_autodraft.py --inspect
   → 에디터를 열고 멈춥니다. 개발자도구로 요소를 확인해 config.yaml 의
     selectors 를 조정하세요.

주의
----
- 네이버는 자동화를 감지·제한할 수 있습니다. 로그인은 사람이 직접(이 스크립트는
  로그인 자동화를 하지 않음), 발행도 사람이 직접 하는 지금 방식이 가장 안전합니다.
- 스마트에디터 화면 구조는 수시로 바뀝니다. 첫 실행은 headful(창 보이게)로,
  실패하면 posts/_debug 에 스크린샷이 남습니다.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

import yaml
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Frame, Page

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEBUG_DIR = ROOT / "posts" / "_debug"

# 스마트에디터 기본 선택자 후보들. config.yaml 의 selectors 로 덮어쓸 수 있습니다.
# 각 항목은 '먼저 되는 것을 쓰는' 후보 리스트입니다.
DEFAULT_SELECTORS = {
    # 이어쓰기/도움말 등 시작 팝업의 '취소' 버튼
    "popup_cancel": [
        "button.se-popup-button-cancel",
        ".se-popup-button-cancel",
        "button:has-text('취소')",
    ],
    # 제목 입력 영역
    "title": [
        ".se-section-documentTitle .se-text-paragraph",
        ".se-documentTitle .se-text-paragraph",
        "span.se-placeholder:has-text('제목')",
    ],
    # 본문 첫 문단
    "body": [
        ".se-component.se-text .se-text-paragraph",
        ".se-module-text .se-text-paragraph",
        "span.se-placeholder:has-text('내용')",
    ],
    # 임시저장 버튼
    "save": [
        "button.save_btn__bzc5B",
        "button:has-text('저장')",
        ".btn_save",
    ],
}


def load_config() -> dict:
    cfg_path = ROOT / "config.yaml"
    if not cfg_path.exists():
        sys.exit("config.yaml 이 없습니다. config.example.yaml 을 복사해 만들어주세요.")
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    # 사용자 정의 선택자 병합
    sel = dict(DEFAULT_SELECTORS)
    for k, v in (cfg.get("naver", {}).get("selectors") or {}).items():
        sel[k] = v if isinstance(v, list) else [v]
    cfg["_selectors"] = sel
    return cfg


def write_url(blog_id: str) -> str:
    return f"https://blog.naver.com/{blog_id}?Redirect=Write&"


def find_editor_frame(page: Page, selectors: dict, timeout_ms: int = 20000) -> Frame:
    """에디터(제목 입력)가 들어있는 프레임을 페이지/모든 iframe 에서 찾는다."""
    page.wait_for_timeout(2000)
    deadline = timeout_ms
    step = 500
    title_candidates = selectors["title"]
    while deadline > 0:
        for frame in [page.main_frame, *page.frames]:
            for css in title_candidates:
                try:
                    if frame.locator(css).count() > 0:
                        return frame
                except Exception:
                    continue
        page.wait_for_timeout(step)
        deadline -= step
    raise PWTimeout("에디터 프레임(제목 입력 영역)을 찾지 못했습니다.")


def first_working(frame: Frame, candidates: list[str]):
    """후보 선택자 중 실제로 존재하는 첫 번째 Locator 를 돌려준다."""
    for css in candidates:
        loc = frame.locator(css).first
        try:
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def dismiss_popup(frame: Frame, selectors: dict) -> None:
    loc = first_working(frame, selectors["popup_cancel"])
    if loc:
        try:
            loc.click(timeout=3000)
            frame.wait_for_timeout(500)
        except Exception:
            pass


def type_body(frame: Frame, body_loc, text: str) -> None:
    """본문 영역을 클릭한 뒤 줄 단위로 입력한다(문단은 Enter 로 구분)."""
    body_loc.click()
    frame.wait_for_timeout(300)
    lines = text.replace("\r\n", "\n").split("\n")
    page = frame.page
    for i, line in enumerate(lines):
        if line:
            page.keyboard.type(line, delay=8)
        if i < len(lines) - 1:
            page.keyboard.press("Enter")


def save_debug(page: Page, tag: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{tag}.png"), full_page=True)
        (DEBUG_DIR / f"{tag}.html").write_text(page.content(), encoding="utf-8")
        print(f"    ↳ 디버그 저장: posts/_debug/{tag}.png / .html")
    except Exception:
        pass


def draft_one(page: Page, cfg: dict, post: dict) -> bool:
    """글 하나를 에디터에 입력하고 임시저장한다. 성공 시 True."""
    selectors = cfg["_selectors"]
    blog_id = cfg["naver"]["blog_id"]
    title = post.get("title", "")
    body = post.get("body", "")

    print(f"  · '{title}' 작성 시작")
    page.goto(write_url(blog_id), wait_until="domcontentloaded")

    try:
        frame = find_editor_frame(page, selectors)
    except PWTimeout:
        save_debug(page, "no_editor")
        print("    ✗ 에디터를 찾지 못했습니다. --inspect 로 화면을 확인해 selectors 를 맞춰주세요.")
        return False

    dismiss_popup(frame, selectors)

    # 제목
    title_loc = first_working(frame, selectors["title"])
    if not title_loc:
        save_debug(page, "no_title")
        print("    ✗ 제목 입력 영역을 찾지 못했습니다.")
        return False
    title_loc.click()
    frame.wait_for_timeout(300)
    page.keyboard.type(title, delay=10)

    # 본문
    body_loc = first_working(frame, selectors["body"])
    if not body_loc:
        save_debug(page, "no_body")
        print("    ✗ 본문 입력 영역을 찾지 못했습니다.")
        return False
    type_body(frame, body_loc, body)
    frame.wait_for_timeout(500)

    # 임시저장
    save_loc = first_working(frame, selectors["save"])
    if not save_loc:
        save_debug(page, "no_save")
        print("    ✗ 임시저장 버튼을 찾지 못했습니다. (입력은 되었으니 창에서 직접 저장 가능)")
        return False
    try:
        save_loc.click(timeout=5000)
        frame.wait_for_timeout(1500)
        print("    ✓ 임시저장 완료")
        return True
    except Exception as e:
        save_debug(page, "save_fail")
        print(f"    ✗ 임시저장 클릭 실패: {e}")
        return False


def launch(cfg: dict, headful: bool):
    naver = cfg["naver"]
    profile_dir = (ROOT / naver.get("profile_dir", "./chrome_profile")).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        channel="chrome",             # PC에 설치된 실제 크롬 사용
        headless=not headful,
        viewport={"width": 1280, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return pw, ctx, page


def cmd_login(cfg: dict) -> None:
    pw, ctx, page = launch(cfg, headful=True)
    print("네이버 로그인 창을 엽니다. 직접 로그인해주세요(2차인증 포함).")
    page.goto("https://nid.naver.com/nidlogin.login")
    input("\n로그인을 마쳤으면 이 창(터미널)에서 Enter 를 누르세요… ")
    print("세션이 프로필에 저장되었습니다. 이제 --post / --all 로 자동 임시저장이 가능합니다.")
    ctx.close(); pw.stop()


def cmd_inspect(cfg: dict) -> None:
    pw, ctx, page = launch(cfg, headful=True)
    page.goto(write_url(cfg["naver"]["blog_id"]), wait_until="domcontentloaded")
    print("에디터를 열었습니다. 개발자도구(F12)로 제목/본문/저장 요소의 선택자를 확인하세요.")
    input("확인이 끝나면 Enter … ")
    ctx.close(); pw.stop()


def load_posts(args) -> list[dict]:
    posts_dir = ROOT / "posts"
    if args.post:
        p = pathlib.Path(args.post)
        if not p.is_absolute():
            p = ROOT / args.post
        return [json.loads(p.read_text(encoding="utf-8"))]
    files = sorted(posts_dir.glob("*.json"))
    if not files:
        sys.exit("posts/ 에 초안(.json)이 없습니다. 먼저 generate_post.py 를 실행하세요.")
    return [json.loads(f.read_text(encoding="utf-8")) for f in files]


def main() -> None:
    ap = argparse.ArgumentParser(description="네이버 블로그 임시저장 자동화")
    ap.add_argument("--login", action="store_true", help="전용 프로필로 네이버 로그인(최초 1회)")
    ap.add_argument("--inspect", action="store_true", help="에디터를 열고 멈춰 선택자 확인")
    ap.add_argument("--post", metavar="FILE", help="특정 초안 JSON 하나만 임시저장")
    ap.add_argument("--all", action="store_true", help="posts/ 의 모든 초안을 임시저장")
    args = ap.parse_args()

    cfg = load_config()
    headful = bool(cfg.get("naver", {}).get("headful", True))

    if args.login:
        return cmd_login(cfg)
    if args.inspect:
        return cmd_inspect(cfg)
    if not (args.post or args.all):
        ap.print_help()
        return

    posts = load_posts(args)
    pw, ctx, page = launch(cfg, headful=headful)
    ok = 0
    try:
        for post in posts:
            if draft_one(page, cfg, post):
                ok += 1
    finally:
        ctx.close(); pw.stop()
    print(f"\n완료: {ok}/{len(posts)} 건 임시저장. 네이버 블로그 > 임시저장 목록에서 확인 후 발행하세요.")


if __name__ == "__main__":
    main()
