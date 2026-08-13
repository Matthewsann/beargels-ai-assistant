"""엠즈푸드 오더링(ok.msfs.co.kr)의 발주품목을 API 로 직접 받아 적는다.

앞서 쓰던 worker/msfs_items.py 는 크롬을 띄워 놓고 오가는 응답을 엿듣는
방식이라, 화면을 넘겨보지 않으면 아무것도 못 잡았다. 이 스크립트는 그 화면이
실제로 부르는 REST API 를 그대로 부른다. 크롬도 로그인도 필요 없다.

    PUT https://orderlink.msfs.co.kr/order/{USER_ID}/products?<params>

인증 헤더가 없다 — 매장 ID(USER_ID)와 프랜차이즈 코드(FRCU_CODE)로만 식별한다.
쿼리스트링과 **똑같은 값을 JSON 바디에도** 실어야 400 이 안 난다(서버가 바디를
검증한다). jobsSabn/jobsName 은 비면 거부당하므로 매장 정보를 넣는다.

품목 목록은 dataGubn(마감구분) × jumuGubn(주문구분) 조합마다 다른 묶음이
나온다. 한 번만 부르면 D-1 상품만 잡히므로 조합을 다 돌아 합집합을 만든다.

쓰는 법:
    python -m worker.msfs_products
    python -m worker.msfs_products --date 20260818   # 특정 납품일 기준

나오는 파일:
    data/msfs_items.json    품목 원본(필드 그대로) — 다른 코드가 참조
    data/orderlink_items.csv  원가계산용 한글 헤더 CSV
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import pathlib
import ssl
import sys
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE = os.getenv("MSFS_API", "https://orderlink.msfs.co.kr")
USER_ID = os.getenv("MSFS_USER_ID", "63515")        # 베어글스 송도타임스페이스점
FRCU_CODE = os.getenv("MSFS_FRCU_CODE", "10552")    # 프랜차이즈 코드
CENT_CODE = os.getenv("MSFS_CENT_CODE", "1400")     # 물류센터
JOBS_SABN = os.getenv("MSFS_JOBS_SABN", USER_ID)
JOBS_NAME = os.getenv("MSFS_JOBS_NAME", "베어글스")

OUT_JSON = ROOT / "data" / "msfs_items.json"
OUT_CSV = ROOT / "data" / "orderlink_items.csv"

# partCod1 → 카테고리. 앱 필터 화면의 분류와 1:1 로 맞춘 것.
CATEGORY = {
    "10": "가공식품",
    "11": "가루류",
    "20": "농산물",
    "31": "유제품",
    "41": "제과 냉동",
    "50": "비식품",
    "61": "제빵 냉동",
    "81": "초콜릿",
}

# (응답 필드, CSV 헤더)
COLUMNS = [
    ("itemCode", "품목코드"),
    ("itemName", "품목명"),
    ("itemSize", "규격"),
    ("dawiName", "단위"),
    ("eachQnty", "입수량"),
    ("dngaQnty", "단가기준수량"),
    ("miniQnty", "최소주문수량"),
    ("maxxQnty", "최대주문수량"),
    ("mechDnpr", "공급가"),
    ("mechDnvt", "부가세"),
    ("mechDnga", "단가_합계"),
    ("taxxYsno", "과세여부"),
    ("magmText", "마감"),
    ("magmTime", "마감시간"),
    ("partCod1", "대분류코드"),
    ("partCod2", "중분류코드"),
    ("partCod3", "소분류코드"),
    ("jegoGubn", "재고구분"),
    ("itemStat", "상품상태"),
    ("centCode", "센터코드"),
    ("thumImge", "이미지URL"),
]

# orderlink 서버가 중간 인증서를 같이 내려주지 않는다. 브라우저는 빠진 조각을
# 알아서 받아와 메꾸지만 파이썬은 그러지 않아 검증이 깨진다(certifi 로도 동일).
# 서버 쪽 설정 문제라 우리가 고칠 수 없어, 이 호스트에 한해 검증을 끈다.
# 오가는 값은 공개 품목표뿐이고 로그인 정보는 싣지 않는다.
# 서버가 체인을 고치면 MSFS_VERIFY_TLS=1 로 되돌릴 수 있다.
if os.getenv("MSFS_VERIFY_TLS") == "1":
    _CTX = ssl.create_default_context()
else:
    _CTX = ssl._create_unverified_context()


def _call(method: str, path: str, params: dict) -> dict:
    url = f"{BASE}{path}?" + urllib.parse.urlencode(params)
    body = None
    if method == "PUT":
        # 서버가 쿼리와 별개로 바디를 검증한다 — 같은 값을 그대로 실어 보낸다.
        body = json.dumps(params, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, context=_CTX, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def delivery_dates() -> list[dict]:
    """주문 가능한 납품일 목록. 첫 항목이 가장 이른 날짜다."""
    res = _call("GET", f"/order/{USER_ID}/deliveryDate",
                {"centCode": CENT_CODE, "deliYsno": "1"})
    return res.get("result") or []


def products(jumu_date: str, cust_deli: str, data_gubn: str, jumu_gubn: str) -> list[dict]:
    params = {
        "frcuCode": FRCU_CODE,
        "dataGubn": data_gubn,
        "jumuGubn": jumu_gubn,
        "custDeli": cust_deli,
        "jumuDate": jumu_date,
        "itemCode": "",
        "jobsSabn": JOBS_SABN,
        "jobsName": JOBS_NAME,
        "srchItem": "",
        "partCod1": "",
        "partCod2": "",
        "partCod3": "",
        "itemStat": "00",
        "jumuYsno": "Y",
        "rcmdItem": "0",
        "sortFild": "1",
    }
    return _call("PUT", f"/order/{USER_ID}/products", params).get("result") or []


def collect(jumu_date: str, cust_deli: str) -> list[dict]:
    """마감/주문 구분 조합을 모두 돌아 품목 합집합을 만든다."""
    items: dict[str, dict] = {}
    for data_gubn in ("1", "2", "3"):
        for jumu_gubn in ("1", "2"):
            rows = products(jumu_date, cust_deli, data_gubn, jumu_gubn)
            new = 0
            for row in rows:
                code = row.get("itemCode")
                if code and code not in items:
                    items[code] = row
                    new += 1
            print(f"  dataGubn={data_gubn} jumuGubn={jumu_gubn}: "
                  f"{len(rows):>4}건 (신규 {new})", flush=True)
    return [r for r in items.values() if r.get("itemName")]


def write_csv(rows: list[dict], jumu_date: str) -> None:
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    # 엑셀에서 바로 열리도록 BOM 을 붙인다.
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([head for _, head in COLUMNS] + ["카테고리", "기준납품일"])
        for row in rows:
            writer.writerow(
                [row.get(key) for key, _ in COLUMNS]
                + [CATEGORY.get(row.get("partCod1"), ""), jumu_date]
            )


def main() -> int:
    ap = argparse.ArgumentParser(description="오더링 발주품목 수집")
    ap.add_argument("--date", help="납품일(YYYYMMDD). 없으면 가장 이른 주문 가능일")
    args = ap.parse_args()

    dates = delivery_dates()
    if not dates:
        print("주문 가능한 납품일이 없습니다. 센터코드/매장ID를 확인해 주세요.")
        return 1
    print("주문 가능 납품일:", ", ".join(d["dataDate"] for d in dates))

    chosen = next((d for d in dates if d["dataDate"] == args.date), None) if args.date else dates[0]
    if chosen is None:
        print(f"{args.date} 는 주문 가능일이 아닙니다.")
        return 1
    jumu_date = chosen["dataDate"]
    print(f"기준 납품일: {jumu_date} ({chosen.get('dataYoil', '')})")

    rows = collect(jumu_date, chosen.get("custDeli", "1"))
    rows.sort(key=lambda r: (r.get("partCod1") or "", r.get("itemName") or ""))

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    write_csv(rows, jumu_date)

    print(f"\n품목 {len(rows)}개")
    print(f"  → {OUT_JSON}")
    print(f"  → {OUT_CSV}")
    by_cat: dict[str, int] = {}
    for r in rows:
        by_cat[CATEGORY.get(r.get("partCod1"), "미분류")] = \
            by_cat.get(CATEGORY.get(r.get("partCod1"), "미분류"), 0) + 1
    for name, cnt in sorted(by_cat.items(), key=lambda x: -x[1]):
        print(f"    {name}: {cnt}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
