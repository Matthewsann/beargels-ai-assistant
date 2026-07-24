"""GoBlogWrite.naver 로 실제 에디터를 열어 구조/셀렉터를 진단."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import naver_autodraft as na

cfg = na.load_config()
pw, ctx, page = na.launch(cfg, headful=False)
try:
    page.goto("https://blog.naver.com/GoBlogWrite.naver", wait_until="domcontentloaded")
    page.wait_for_timeout(5000)
    print("[최종 URL]", page.url)
    print("[제목]", page.title())
    print("[프레임 목록]")
    for f in page.frames:
        print("   -", (f.name or "(noname)"), "|", f.url[:110])

    # 각 프레임에서 SmartEditor 흔적 탐색
    probes = {
        "제목영역": [".se-documentTitle", ".se-section-documentTitle", ".se-title-text",
                  "[contenteditable='true']", ".se-placeholder"],
        "본문영역": [".se-component.se-text", ".se-module-text", ".se-text-paragraph"],
        "저장버튼": ["button:has-text('저장')", ".save_btn__bzc5B", "[class*='save']"],
        "발행버튼": ["button:has-text('발행')", "[class*='publish']"],
        "이어쓰기팝업": [".se-popup-button-cancel", "button:has-text('취소')"],
    }
    for fr in [page.main_frame, *page.frames]:
        tag = fr.name or fr.url[:60]
        hits = []
        for label, cands in probes.items():
            for css in cands:
                try:
                    c = fr.locator(css).count()
                except Exception:
                    c = 0
                if c > 0:
                    hits.append(f"{label}:{css}={c}")
        if hits:
            print(f"\n[프레임 '{tag}'] 에서 발견:")
            for h in hits:
                print("   ", h)
finally:
    ctx.close(); pw.stop()
