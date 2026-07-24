"""저장된 프로필의 네이버 로그인 세션이 살아있는지 창 없이(headless) 확인."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import naver_autodraft as na
import login_helper as lh

cfg = na.load_config()
pw, ctx, page = na.launch(cfg, headful=False)
try:
    page.goto("https://www.naver.com", wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    names = {c["name"] for c in ctx.cookies("https://www.naver.com")}
    logged = {"NID_AUT", "NID_SES"}.issubset(names)
    print("로그인 세션:", "살아있음 ✅" if logged else "없음 ❌")
    print("네이버 쿠키 목록:", sorted(n for n in names if n.startswith("NID")))
finally:
    ctx.close(); pw.stop()
