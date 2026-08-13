"""엠즈푸드 발주 사이트(ok.msfs.co.kr)의 발주품목을 긁어 자재 후보로 만든다.

집 PC 전용이다. 클라우드 컨테이너에서는 이 사이트에 접속 자체가 막혀 있고,
로그인 세션도 집 PC 크롬 프로필에만 있다.

동작 방식 — 화면 구조를 미리 알 수 없는 SPA 라서 DOM 을 파싱하지 않고,
**크롬 창에서 오가는 응답을 통째로 받아 적는다.** 사장님은 평소처럼 발주
화면을 넘겨보기만 하면 된다.

파일 두 개가 나온다:
    data/msfs_items.json   품목처럼 보이는 줄만 추린 것(바로 쓸 수 있으면 이것)
    data/msfs_raw.json     받은 응답 원본(필드 이름을 모를 때 이걸 보고 맞춘다)

쓰는 법 (집 PC):
    1. scripts/launch_chrome.bat 으로 크롤링용 크롬을 켠다
    2. python -m worker.msfs_items      ← 먼저 실행해 두고
    3. 그 크롬에서 ok.msfs.co.kr 로그인 → 발주 품목 목록을 카테고리별로 넘겨본다
       (터미널에 '기록 N건' 이 올라가면 잡히고 있는 것)
    4. 터미널에서 엔터 → 저장
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

OUT = ROOT / "data" / "msfs_items.json"
RAW = ROOT / "data" / "msfs_raw.json"
START_URL = os.getenv("MSFS_URL", "https://ok.msfs.co.kr/#/home")
CDP = f"http://127.0.0.1:{os.getenv('CDP_PORT', '9222')}"
HOST_HINT = "msfs"          # 이 글자가 URL 에 든 응답만 본다(다른 탭 소음 제외)

NAME_KEYS = ("itemNm", "goodsNm", "prodNm", "productName", "itemName", "goodsName",
             "prdNm", "prdtNm", "matNm", "title", "name", "품목명", "상품명", "제품명")
PRICE_KEYS = ("price", "unitPrice", "salePrice", "sellPrice", "amt", "unitAmt",
              "cost", "단가", "판매가", "공급가")
SPEC_KEYS = ("spec", "standard", "std", "unit", "packing", "capacity", "size",
             "규격", "단위", "포장")
CODE_KEYS = ("itemCd", "goodsCd", "prodCd", "prdCd", "matCd", "code", "품목코드")


def _pick(d: dict, keys):
    low = {re.sub(r"[_\s]", "", str(k)).lower(): v for k, v in d.items()}
    for k in keys:
        v = d.get(k)
        if v in (None, ""):
            v = low.get(re.sub(r"[_\s]", "", k).lower())
        if v not in (None, ""):
            return v
    return None


def _rows(obj, out, depth=0):
    """응답 어디에 목록이 있든 훑어서 '이름처럼 보이는 값'이 있는 dict 를 모은다."""
    if depth > 8:
        return
    if isinstance(obj, list):
        for x in obj:
            _rows(x, out, depth + 1)
        return
    if not isinstance(obj, dict):
        return
    name = _pick(obj, NAME_KEYS)
    if isinstance(name, str) and 1 < len(name.strip()) < 120:
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

    found, seen, raw = [], set(), []
    lock = threading.Lock()

    def note(resp):
        url = resp.url
        if HOST_HINT not in url:
            return
        if re.search(r"\.(js|css|png|jpe?g|gif|svg|woff2?|ico|map)(\?|$)", url, re.I):
            return
        try:
            body = resp.text()
        except Exception:  # noqa: BLE001
            return
        if not body or len(body) > 4_000_000:
            return
        stripped = body.lstrip()
        if not stripped[:1] in ("{", "["):
            return
        try:
            data = json.loads(body)
        except Exception:  # noqa: BLE001
            return
        with lock:
            # 원본은 무조건 남긴다 — 필드 이름을 모를 때 이걸 보고 맞춘다.
            raw.append({"url": url.split("?")[0], "query": url.split("?", 1)[1]
                        if "?" in url else "", "body": data})
            rows = []
            _rows(data, rows)
            new = 0
            for r in rows:
                key = (r["name"], str(r.get("code") or ""), str(r.get("spec") or ""))
                if key in seen:
                    continue
                seen.add(key)
                found.append(r)
                new += 1
            if new:
                print(f"  … 품목 후보 +{new} (누적 {len(found)}) ← {url.split('?')[0][-60:]}",
                      flush=True)
            elif rows == []:
                print(f"  … 응답 기록 {len(raw)}건 ← {url.split('?')[0][-60:]}", flush=True)

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(CDP)
        except Exception as e:  # noqa: BLE001
            print(f"크롤링용 크롬에 붙지 못했습니다({CDP}): {e}\n"
                  f"scripts/launch_chrome.bat 을 먼저 실행해 주세요.")
            return 1
        if not browser.contexts:
            print("크롬에 열린 창이 없습니다. 크롬에서 아무 탭이나 연 뒤 다시 실행해 주세요.")
            return 1

        # 창(context) 단위로 건다 — 어느 탭에서 보시든, 새 탭을 여시든 다 잡힌다.
        # (전에는 스크립트가 연 탭 하나만 봤다.)
        for ctx in browser.contexts:
            ctx.on("response", note)
            for pg in ctx.pages:
                pg.on("response", note)
            ctx.on("page", lambda pg: pg.on("response", note))

        tabs = sum(len(c.pages) for c in browser.contexts)
        print(f"\n크롬 창 {len(browser.contexts)}개 · 탭 {tabs}개를 감시합니다.")
        print(f"크롬에서 {START_URL} 로 가서 로그인하고,")
        print("발주 품목 목록을 카테고리별로 끝까지 넘겨보세요.")
        print("(아래에 '품목 후보 +N' 이 올라가면 잡히고 있는 겁니다)")
        print("\n다 보셨으면 여기서 엔터 — 그때까지 계속 모읍니다.\n")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(found, ensure_ascii=False, indent=1), encoding="utf-8")
    RAW.write_text(json.dumps(raw, ensure_ascii=False, indent=1)[:8_000_000],
                   encoding="utf-8")
    priced = [f for f in found if f.get("price")]
    print(f"\n품목 후보 {len(found)}개 (단가까지 잡힌 것 {len(priced)}개) → {OUT}")
    print(f"응답 원본 {len(raw)}건 → {RAW}")
    for f in found[:15]:
        print(f"  · {f['name']}  규격={f.get('spec')}  단가={f.get('price')}")
    if len(found) < 10:
        print("\n거의 못 잡았습니다. data/msfs_raw.json 을 채팅에 올려주시면")
        print("실제 응답을 보고 맞춰드리겠습니다.")
        if not raw:
            print("⚠ 응답 자체가 하나도 안 잡혔습니다 —")
            print("  스크립트를 켜 둔 채로 크롬에서 화면을 '새로' 넘겨보셨는지 확인해 주세요")
            print("  (이미 떠 있는 화면은 통신이 끝나 있어 잡히지 않습니다).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
