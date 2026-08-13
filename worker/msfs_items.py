"""엠즈푸드 발주 사이트(ok.msfs.co.kr)의 발주품목을 긁어 자재 후보로 만든다.

집 PC 전용이다. 클라우드 컨테이너에서는 이 사이트에 접속 자체가 막혀 있고,
로그인 세션도 집 PC 크롬 프로필에만 있다.

동작 방식 — 화면 구조를 미리 알 수 없는 SPA 라서, DOM 을 파싱하지 않고
**사이트가 스스로 받아오는 JSON 응답을 가로채서** 모은다. 사장님은 평소처럼
발주 화면을 열고 품목 목록을 넘겨보기만 하면 되고, 그 사이 오간 응답에서
'이름·규격·단가' 처럼 보이는 필드를 찾아 자재 후보로 뽑는다.

쓰는 법 (집 PC):
    1. scripts/launch_chrome.bat 으로 크롤링용 크롬을 켜고 ok.msfs.co.kr 로그인
    2. python -m worker.msfs_items
    3. 열린 크롬에서 발주 품목 목록을 끝까지 넘겨본다(카테고리별로)
    4. 터미널에서 엔터 → data/msfs_items.json 저장
    5. 결과를 확인한 뒤 web 자재 목록에 반영(일괄편집 화면에서 붙여넣기)
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

OUT = ROOT / "data" / "msfs_items.json"
START_URL = os.getenv("MSFS_URL", "https://ok.msfs.co.kr/#/home")
CDP = f"http://127.0.0.1:{os.getenv('CDP_PORT', '9222')}"

# 품목 한 줄로 볼 수 있는 최소 조건 — 이름처럼 보이는 값이 있어야 한다.
NAME_KEYS = ("itemNm", "goodsNm", "prodNm", "productName", "itemName",
             "name", "품목명", "상품명")
PRICE_KEYS = ("price", "unitPrice", "salePrice", "amt", "unitAmt", "단가", "판매가")
SPEC_KEYS = ("spec", "standard", "std", "unit", "규격", "단위", "packing")
CODE_KEYS = ("itemCd", "goodsCd", "prodCd", "code", "품목코드")


def _pick(d: dict, keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    # 대소문자·언더스코어 차이를 무시하고 한 번 더
    low = {re.sub(r"[_\s]", "", str(k)).lower(): v for k, v in d.items()}
    for k in keys:
        v = low.get(re.sub(r"[_\s]", "", k).lower())
        if v not in (None, ""):
            return v
    return None


def _rows(obj, out, depth=0):
    """응답 JSON 어디에 목록이 있든 찾아낸다(구조를 모르니 전부 훑는다)."""
    if depth > 6:
        return
    if isinstance(obj, list):
        for x in obj:
            _rows(x, out, depth + 1)
        return
    if not isinstance(obj, dict):
        return
    name = _pick(obj, NAME_KEYS)
    if isinstance(name, str) and name.strip() and len(name) < 120:
        out.append({
            "name": name.strip(),
            "code": _pick(obj, CODE_KEYS),
            "spec": _pick(obj, SPEC_KEYS),
            "price": _pick(obj, PRICE_KEYS),
            "raw": obj,
        })
        return
    for v in obj.values():
        _rows(v, out, depth + 1)


def main():
    from playwright.sync_api import sync_playwright

    found, seen = [], set()

    def on_response(resp):
        ct = (resp.headers.get("content-type") or "").lower()
        if "json" not in ct:
            return
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            return
        rows = []
        _rows(data, rows)
        for r in rows:
            key = (r["name"], str(r.get("code") or ""), str(r.get("spec") or ""))
            if key in seen:
                continue
            seen.add(key)
            found.append(r)

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(CDP)
        except Exception as e:  # noqa: BLE001
            print(f"크롤링용 크롬에 붙지 못했습니다({CDP}): {e}\n"
                  f"scripts/launch_chrome.bat 을 먼저 실행해 주세요.")
            return 1
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()
        page.on("response", on_response)
        page.goto(START_URL)

        print("\n크롬에서 발주 품목 목록을 카테고리별로 끝까지 넘겨보세요.")
        print("(로그인이 안 돼 있으면 먼저 로그인)")
        print("다 보셨으면 여기서 엔터를 누르세요 — 그때까지 계속 모읍니다.\n")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(found, ensure_ascii=False, indent=1), encoding="utf-8")
    named = [f for f in found if f.get("price")]
    print(f"품목 후보 {len(found)}개 (단가까지 잡힌 것 {len(named)}개) → {OUT}")
    for f in found[:15]:
        print(f"  · {f['name']}  규격={f.get('spec')}  단가={f.get('price')}")
    if not found:
        print("아무것도 못 잡았습니다 — 품목 목록 화면을 실제로 넘겨보셨는지 확인해 주세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
