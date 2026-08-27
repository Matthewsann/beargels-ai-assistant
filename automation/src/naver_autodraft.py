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
    # 본문 툴바의 '사진' 버튼 — 누르면 파일 선택창이 열린다
    "image_button": [
        "button.se-image-toolbar-button",
        "button[data-name='image']",
        ".se-toolbar-item-image button",
        "button[title='사진']",
        "button:has-text('사진')",
    ],
    # 본문 툴바의 '동영상' 버튼 — 사진 업로더는 mp4 를 '파일 형식 오류'로 거부한다
    # (2026-08-27 실측). 영상은 반드시 이 버튼의 별도 흐름으로 넣어야 한다.
    "video_button": [
        "button.se-video-toolbar-button",
        "button[data-name='video']",
        ".se-toolbar-item-video button",
        "button[title='동영상']",
        "button:has-text('동영상')",
    ],
    # 업로드 오류·안내 팝업의 확인/완료 버튼 — 안 닫으면 dim 막이 저장 클릭을 가로챈다
    "popup_ok": [
        ".se-popup button:has-text('확인')",
        ".se-popup-button-confirm",
        "button:has-text('확인')",
    ],
    # 사진을 넣은 뒤 뜨는 '사진 설명' 칸(여기에 글이 잘못 들어가는 걸 막는 데 씀)
    "image_caption": [
        ".se-caption .se-text-paragraph",
        ".se-module-caption .se-text-paragraph",
    ],
    # 임시저장 버튼
    "save": [
        "button.save_btn__bzc5B",
        "button:has-text('저장')",
        ".btn_save",
    ],
    # 발행(게시) 버튼 — 발행 설정 레이어를 연다
    "publish_open": [
        "button.publish_btn__m9KHH",
        "button:has-text('발행')",
        ".btn_publish",
    ],
    # 발행 설정 레이어의 '예약' 라디오/탭
    "reserve_radio": [
        "label:has-text('예약')",
        "input[type='radio'][value='reserve']",
        ".radio_time:has-text('예약')",
    ],
    # 예약 레이어의 날짜 입력
    "reserve_date": [
        "input.input_date",
        ".se-popup input[placeholder*='날짜']",
    ],
    # 최종 확정(예약 발행) 버튼
    "reserve_confirm": [
        "button.confirm_btn__WEaBq",
        "button:has-text('예약 발행')",
        "button:has-text('발행')",
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


def clear_popups(page: Page, frame: Frame, selectors: dict) -> bool:
    """떠 있는 오류/안내 팝업을 닫는다('파일 전송 오류' 등).

    팝업의 반투명 막(se-popup-dim)이 남아 있으면 이후 모든 클릭이 막힌다
    — 저장 실패의 실제 원인이었다(2026-08-27 실측). 닫은 게 있으면 True.
    """
    closed = False
    for _ in range(3):                       # 팝업이 겹쳐 뜨는 경우까지
        loc = first_working(frame, selectors["popup_ok"])
        if loc is None:
            break
        try:
            loc.click(timeout=2000)
            page.wait_for_timeout(400)
            closed = True
        except Exception:  # noqa: BLE001
            break
    return closed


def _refocus_body(page: Page, frame: Frame) -> None:
    """커서를 본문 맨 끝 문단으로 되돌린다(사진 설명 칸에 글이 새는 것 방지)."""
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    try:
        last = frame.locator(".se-component.se-text .se-text-paragraph").last
        if last.count() > 0:
            last.click(timeout=3000)
            page.wait_for_timeout(300)       # 포커스가 자리잡기 전에 치면 글자가 샌다
            page.keyboard.press("End")
    except Exception:  # noqa: BLE001
        pass


def _close_layer(page: Page, frame: Frame) -> None:
    """동영상 첨부 같은 전면 레이어를 닫는다(X 버튼 → Escape 순).

    이 레이어가 남아 있으면 투명 dim 이 이후 모든 클릭을 가로챈다
    — 사진 11·13번과 저장이 전부 막혔던 실제 원인(2026-08-28 실측).
    """
    for css in ("button.se-popup-close-button", ".se-popup-close-button",
                "button[data-name='close']", ".se-popup button:has-text('닫기')",
                "button[title='닫기']"):
        try:
            loc = frame.locator(css).first
            if loc.count() > 0:
                loc.click(timeout=2000)
                page.wait_for_timeout(500)
                return
        except Exception:  # noqa: BLE001
            continue
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)


def _dim_gone(frame: Frame) -> bool:
    try:
        return frame.locator(".se-popup-dim").count() == 0
    except Exception:  # noqa: BLE001
        return True


def insert_video(page: Page, frame: Frame, selectors: dict, path: str,
                 timeout_ms: int = 180000) -> bool:
    """'동영상' 버튼 흐름으로 영상 하나를 넣는다. 실패하면 False(글은 계속).

    실측한 실제 흐름(2026-08-28):
      툴바 [동영상] → '일반 동영상/360VR' 첨부 레이어 → 레이어 안 [동영상 추가]
      버튼이 파일선택창을 연다 → 업로드 후 (제목 입력) → [완료] → 본문 삽입.
    사진 업로더는 mp4 를 '파일 형식 오류'로 거부하므로 반드시 이 경로여야 한다.
    어떤 단계에서 실패하든 레이어를 확실히 닫고 나온다.
    """
    btn = first_working(frame, selectors["video_button"])
    if btn is None:
        print("    · 동영상 버튼을 찾지 못해 영상은 건너뜁니다.")
        return False
    try:
        btn.click(timeout=5000)
    except Exception as e:  # noqa: BLE001
        print(f"    · 동영상 버튼 클릭 실패({str(e)[:60]}) — 건너뜁니다.")
        return False

    # 첨부 레이어의 '동영상 추가' 버튼을 기다린다
    add = None
    for _ in range(16):                       # 최대 8초
        loc = frame.locator("button:has-text('동영상 추가')").first
        try:
            if loc.count() > 0:
                add = loc
                break
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(500)
    if add is None:
        print("    · 동영상 첨부 레이어가 안 열렸습니다 — 건너뜁니다.")
        _close_layer(page, frame)
        _refocus_body(page, frame)
        return False

    try:
        with page.expect_file_chooser(timeout=10000) as fc:
            add.click(timeout=5000)
        fc.value.set_files(path)
    except Exception as e:  # noqa: BLE001
        print(f"    · 동영상 파일 선택 실패({str(e)[:60]}) — 건너뜁니다.")
        _close_layer(page, frame)
        _refocus_body(page, frame)
        return False

    # 업로드 완료 대기 → 제목 채우고 완료. 오류 팝업이면 닫고 포기.
    inserted = False
    waited = 0
    while waited < timeout_ms:
        page.wait_for_timeout(1000)
        waited += 1000
        try:
            if frame.locator(".se-popup:has-text('오류')").count() > 0:
                print("    · 동영상 업로드 오류 — 닫고 건너뜁니다.")
                clear_popups(page, frame, selectors)
                break
        except Exception:  # noqa: BLE001
            pass
        try:
            done_btn = frame.locator(
                "button:has-text('완료'):visible").first
            title_box = frame.locator(
                "input[placeholder*='제목'], .se-popup input[type='text']").first
            if title_box.count() > 0:
                try:
                    title_box.click(timeout=1500)
                    title_box.fill("베어글스 송도")
                    page.wait_for_timeout(300)
                except Exception:  # noqa: BLE001
                    pass
            if done_btn.count() > 0:
                done_btn.click(timeout=3000)
                page.wait_for_timeout(2000)
                inserted = True
                break
            if _dim_gone(frame) and waited > 5000:
                inserted = True               # 레이어가 스스로 닫힘 = 삽입 완료형
                break
        except Exception:  # noqa: BLE001
            pass

    # 무슨 일이 있었든 레이어가 남아 있으면 닫는다
    if not _dim_gone(frame):
        _close_layer(page, frame)
    if not _dim_gone(frame):                  # 그래도 남았으면 한 번 더
        _close_layer(page, frame)
    _refocus_body(page, frame)
    if inserted:
        print("    · 동영상 삽입 완료")
    return inserted


def insert_media(page: Page, frame: Frame, selectors: dict, path: str,
                 timeout_ms: int = 60000) -> bool:
    """본문 커서 위치에 사진(또는 영상) 파일 하나를 넣는다.

    네이버 에디터의 '사진' 버튼은 눌리면 파일 선택창(file chooser)을 연다.
    Playwright 가 그 창을 가로채 파일 경로를 넘겨주면 사람 손 없이 올라간다.
    버튼을 못 찾으면 에디터 안의 숨은 file 입력칸에 직접 넣어 본다(보조 경로).
    """
    if not _dim_gone(frame):                  # 앞 단계가 남긴 레이어부터 청소
        clear_popups(page, frame, selectors)
        _close_layer(page, frame)
    btn = first_working(frame, selectors["image_button"])
    if btn is not None:
        try:
            with page.expect_file_chooser(timeout=10000) as fc:
                btn.click(timeout=5000)
            fc.value.set_files(path)
        except Exception as e:  # noqa: BLE001 — 보조 경로로 넘어간다
            print(f"    · 사진 버튼 경로 실패({str(e)[:60]}) → 숨은 입력칸으로 시도")
            btn = None
    if btn is None:
        loc = frame.locator("input[type='file']").first
        try:
            if loc.count() == 0:
                print("    ✗ 사진을 넣을 방법을 찾지 못했습니다.")
                return False
            loc.set_input_files(path)
        except Exception as e:  # noqa: BLE001
            print(f"    ✗ 사진 넣기 실패: {str(e)[:80]}")
            return False

    # 업로드가 끝나 본문에 이미지 덩어리가 하나 늘어날 때까지 기다린다.
    # (파일이 크면 몇 초 걸린다 — 안 기다리면 다음 글자가 엉뚱한 데 들어간다)
    before = frame.locator(".se-component.se-image").count()
    waited = 0
    while waited < timeout_ms:
        page.wait_for_timeout(500)
        waited += 500
        try:
            if frame.locator(".se-component.se-image").count() > before:
                break
        except Exception:  # noqa: BLE001
            pass
    page.wait_for_timeout(800)

    # 업로드 오류 팝업이 남아 있으면 닫는다(안 닫으면 이후 클릭 전부 막힘)
    if clear_popups(page, frame, selectors):
        print("    · 업로드 안내/오류 팝업을 닫았습니다.")
    # 사진을 넣으면 커서가 '사진 설명' 칸에 가 있을 수 있다. 그대로 두면
    # 다음 문단이 사진 설명으로 들어가 버린다 → 본문 맨 끝으로 커서를 되돌린다.
    _refocus_body(page, frame)
    return True


def type_blocks(page: Page, frame: Frame, selectors: dict, body_loc,
                blocks: list) -> None:
    """글 토막과 사진을 순서대로 넣는다.

    blocks 예: [{"type":"photo","path":"…jpg"}, {"type":"text","text":"…"}, …]
    사진이 하나도 없는 예전 방식(글자만)도 그대로 돌아간다.
    """
    body_loc.click()
    frame.wait_for_timeout(300)
    first_text = True
    for i, b in enumerate(blocks):
        if b.get("type") == "text":
            text = b.get("text", "")
            if not text.strip():
                continue
            if not first_text:
                page.keyboard.press("Enter")
            lines = text.replace("\r\n", "\n").split("\n")
            for j, line in enumerate(lines):
                if line:
                    page.keyboard.type(line, delay=8)
                if j < len(lines) - 1:
                    page.keyboard.press("Enter")
            first_text = False
        else:
            path = b.get("path")
            if not path:
                continue
            if not first_text:
                page.keyboard.press("Enter")
            name = pathlib.Path(path).name
            if b.get("type") == "video":
                print(f"    · 동영상 넣는 중 ({i + 1}/{len(blocks)}) {name}")
                ok = insert_video(page, frame, selectors, path)
            else:
                print(f"    · 사진 넣는 중 ({i + 1}/{len(blocks)}) {name}")
                ok = insert_media(page, frame, selectors, path)
            b["inserted"] = ok           # 호출자(worker)가 사용완료 판단에 쓴다
            if ok:
                first_text = False


def save_debug(page: Page, tag: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{tag}.png"), full_page=True)
        (DEBUG_DIR / f"{tag}.html").write_text(page.content(), encoding="utf-8")
        print(f"    ↳ 디버그 저장: posts/_debug/{tag}.png / .html")
    except Exception:
        pass


def fill_editor(page: Page, cfg: dict, post: dict) -> Frame | None:
    """에디터를 열고 제목·본문을 입력한다. 성공 시 에디터 frame 을 반환(저장/발행은 호출자가).

    임시저장과 예약발행이 공통으로 쓰는 핵심 입력 로직입니다.
    """
    selectors = cfg["_selectors"]
    blog_id = cfg["naver"]["blog_id"]
    title = post.get("title", "")
    body = post.get("body", "")
    # blocks 가 있으면 사진까지 같이 넣는다(없으면 예전처럼 글자만).
    blocks = post.get("blocks")

    print(f"  · '{title}' 작성 시작")
    page.goto(write_url(blog_id), wait_until="domcontentloaded")

    try:
        frame = find_editor_frame(page, selectors)
    except PWTimeout:
        save_debug(page, "no_editor")
        print("    ✗ 에디터를 찾지 못했습니다. --inspect 로 화면을 확인해 selectors 를 맞춰주세요.")
        return None

    dismiss_popup(frame, selectors)

    title_loc = first_working(frame, selectors["title"])
    if not title_loc:
        save_debug(page, "no_title")
        print("    ✗ 제목 입력 영역을 찾지 못했습니다.")
        return None
    title_loc.click()
    frame.wait_for_timeout(300)
    page.keyboard.type(title, delay=10)

    body_loc = first_working(frame, selectors["body"])
    if not body_loc:
        save_debug(page, "no_body")
        print("    ✗ 본문 입력 영역을 찾지 못했습니다.")
        return None
    if blocks:
        n_photo = sum(1 for b in blocks if b.get("type") != "text")
        print(f"    · 글 {len(blocks) - n_photo}토막 + 사진 {n_photo}장")
        type_blocks(page, frame, selectors, body_loc, blocks)
    else:
        type_body(frame, body_loc, body)
    frame.wait_for_timeout(500)
    return frame


def draft_one(page: Page, cfg: dict, post: dict) -> bool:
    """글 하나를 에디터에 입력하고 임시저장한다. 성공 시 True."""
    selectors = cfg["_selectors"]
    frame = fill_editor(page, cfg, post)
    if frame is None:
        return False

    # 임시저장 — 떠 있는 팝업/레이어부터 닫는다(dim 막이 클릭을 가로챈다)
    clear_popups(page, frame, selectors)
    if not _dim_gone(frame):
        _close_layer(page, frame)
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
    # 저장된 로그인 세션(쿠키)을 주입 — 세션 쿠키가 디스크에 보존되지 않는 문제 우회.
    state_file = ROOT / "naver_state.json"
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            cookies = state.get("cookies", [])
            if cookies:
                ctx.add_cookies(cookies)
        except Exception as e:
            print(f"  · 저장된 세션 주입 경고: {e}")

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
    """--post 는 특정 파일, --all 은 라이브러리의 ready 항목 전체를 임시저장."""
    if getattr(args, "post", None):
        p = pathlib.Path(args.post)
        if not p.is_absolute():
            p = ROOT / args.post
        return [json.loads(p.read_text(encoding="utf-8"))]
    import library
    metas = library.list_items(status=library.STATUS_READY)
    if not metas:
        sys.exit("라이브러리에 ready 상태 글이 없습니다. 먼저 generate_post.py 를 실행하세요.")
    return [library.load_post(m["id"]) for m in metas]


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
