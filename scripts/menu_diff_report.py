r"""채널별 메뉴 수정 지시서 생성 — 집 PC 실행용.

정본(menu_items)과 채널 스냅샷(menu_channel_snapshots)을 비교해
"어느 채널에서 무엇을 어떻게 고칠지"를 reports/menu-diff.md 로 남긴다.
사장님·직원은 이 파일만 보고 각 채널 포털에서 그대로 반영하면 된다.

Supabase 접속이 필요해 집 PC 에서 실행한다 (worker\menu_diff.bat).

리포트에는 메뉴명·가격만 담는다 — 어차피 채널에 공개된 정보다.
원가·마진은 담지 않는다 (저장소가 공개라 외부에 보이면 안 됨).
"""
from __future__ import annotations

import pathlib
import re
import sys
from datetime import datetime
from difflib import SequenceMatcher

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database import supabase_client as db  # noqa: E402

REPORT = ROOT / "reports" / "menu-diff.md"

CH_LABEL = {"baemin": "배민 (self.baemin.com)",
            "coupang": "쿠팡이츠 (store.coupangeats.com)",
            "naver": "네이버 스마트플레이스"}
# 채널별 기준가 — 네이버는 매장 메뉴 기준(사장님 확인), 배달앱은 배달가.
PRICE_BASE = {"baemin": "delivery", "coupang": "delivery",
              "naver": "store", "store": "store"}

# 쿠팡 메뉴 화면에서 카테고리 제목이 메뉴처럼 잡히는 노이즈(실조사 2026-08).
NOISE = re.compile(
    r"^\[B\]|^\[신메뉴\]$|^\[1~2인 세트\]$|^커피$|^보틀\(1L\)$|^단체주문 10인$"
    r"|^든든한 샌드위치 세트$|^BEARGLS HEALTHY|^Bear Cream Cheese")


def expected_price(item, override, channel):
    # 가격이 갈리는 곳은 배달앱뿐 — 매장·네이버는 매장가 그대로다(사장님 확인
    # 2026-08). 그래서 이 둘은 채널 오버라이드도 보지 않는다.
    if PRICE_BASE.get(channel) == "store":
        return item.get("store_price")
    if override and override.get("price_override") is not None:
        return override["price_override"]
    return item.get("delivery_price") or item.get("store_price")


def expected_name(item, override):
    if override and override.get("name_override"):
        return override["name_override"]
    return item["name"]


def active_for(item, channel):
    """정본 기준으로 이 채널에 노출돼야 하는 메뉴인가."""
    if PRICE_BASE.get(channel) == "store":
        return bool(item.get("store_active"))
    return bool(item.get("delivery_active"))


def won(v):
    return f"{v:,}원" if isinstance(v, (int, float)) else "?"


def main() -> int:
    items = db.menu_all()
    overrides = {(o["sku"], o["channel"]): o for o in db.menu_channels_all()}
    snaps = db.menu_snapshots_all()
    by_sku = {i["sku"]: i for i in items}
    by_norm = {db.normalize_menu_name(i["name"]): i["sku"] for i in items}

    lines = [f"# 채널별 메뉴 수정 지시서 — {datetime.now():%Y-%m-%d %H:%M}", ""]
    lines += ["정본(직원용 웹 /menu)과 각 채널에 실제 노출 중인 메뉴를 비교한 결과입니다.",
              "각 채널 포털에서 아래 표를 위에서부터 처리하고, 끝나면 다시 수집해",
              "이 지시서가 비는지 확인하세요. **일부러 다르게 둘 항목은 고치는 대신",
              "웹 /menu 의 '채널 예외'에 기록**하세요 — 다음 지시서부터 빠집니다.", ""]

    total_fix = 0
    for ch in ("baemin", "coupang", "naver"):
        rows = [s for s in snaps if s["channel"] == ch]
        lines.append(f"## {CH_LABEL[ch]} — 노출 {len(rows)}개")
        if not rows:
            lines += ["", "⚠ 수집된 스냅샷이 없습니다. 먼저 메뉴 수집을 실행하세요.", ""]
            continue
        when = (rows[0].get("collected_at") or "")[:16].replace("T", " ")
        lines.append(f"(수집 시각: {when})\n")

        price_fix, name_fix, extra, missing = [], [], [], []
        matched_skus = set()
        rows = [s for s in rows if not NOISE.search(s["menu_name"])]
        for s in rows:
            sku = s.get("matched_sku") or by_norm.get(
                db.normalize_menu_name(s["menu_name"]))
            item = by_sku.get(sku)
            if not item:
                extra.append(s)
                continue
            matched_skus.add(sku)
            ov = overrides.get((sku, ch))
            if ov and ov.get("active") is False:
                continue  # 일부러 이 채널에서 다르게 운영 — 예외 등록됨
            ep, en = expected_price(item, ov, ch), expected_name(item, ov)
            if s.get("price") is not None and ep is not None and s["price"] != ep:
                price_fix.append((s, item, ep))
            if db.normalize_menu_name(s["menu_name"]) != db.normalize_menu_name(en):
                name_fix.append((s, en))

        for item in items:
            if not active_for(item, ch) or item["sku"] in matched_skus:
                continue
            ov = overrides.get((item["sku"], ch))
            if ov and ov.get("active") is False:
                continue
            missing.append(item)

        # '정본에 없음'과 '채널에 없음'이 사실은 같은 메뉴인 경우(오타·표기 차이)를
        # 짝지어 '이름 수정'으로 승격 — 예: 채널 '슈패너' ↔ 정본 '슈페너'.
        # 확신 높은 것(유사도 0.8 이상)만 승격, 나머지는 그대로 둔다.
        still_extra, used = [], set()
        for s in extra:
            en = db.normalize_menu_name(s["menu_name"])
            best, score = None, 0.0
            for i, item in enumerate(missing):
                if i in used:
                    continue
                r = SequenceMatcher(None, en,
                                    db.normalize_menu_name(item["name"])).ratio()
                if r > score:
                    best, score = i, r
            if best is not None and score >= 0.8:
                item = missing[best]
                used.add(best)
                name_fix.append((s, expected_name(item, overrides.get((item["sku"], ch)))))
                ep = expected_price(item, overrides.get((item["sku"], ch)), ch)
                if s.get("price") is not None and ep is not None and s["price"] != ep:
                    price_fix.append((s, item, ep))
            else:
                still_extra.append(s)
        extra = still_extra
        missing = [m for i, m in enumerate(missing) if i not in used]

        n = len(price_fix) + len(name_fix) + len(extra) + len(missing)
        total_fix += n
        if n == 0:
            lines += ["✅ 정본과 완전히 일치합니다.", ""]
            continue

        if price_fix:
            lines += [f"### 1) 가격 수정 ({len(price_fix)}건)", "",
                      "| 채널 메뉴명 | 현재 | → 바꿀 가격 |", "|---|---:|---:|"]
            for s, item, ep in sorted(price_fix, key=lambda x: -abs((x[0]["price"] or 0) - x[2])):
                lines.append(f"| {s['menu_name']} | {won(s['price'])} | **{won(ep)}** |")
            lines.append("")
        if name_fix:
            lines += [f"### 2) 이름 수정 ({len(name_fix)}건)", "",
                      "| 현재 채널 표기 | → 정본 표기 |", "|---|---|"]
            for s, en in name_fix:
                lines.append(f"| {s['menu_name']} | **{en}** |")
            lines.append("")
        if extra:
            lines += [f"### 3) 정본에 없는 메뉴 ({len(extra)}건) — 내리거나, 정본에 추가", "",
                      "| 채널 메뉴명 | 채널가 |", "|---|---:|"]
            for s in extra:
                lines.append(f"| {s['menu_name']} | {won(s.get('price'))} |")
            lines += ["", "> 옛 메뉴면 채널에서 내리고, 계속 팔 메뉴면 웹 /menu 정본에 추가하세요.", ""]
        if missing:
            lines += [f"### 4) 채널에 없는 메뉴 ({len(missing)}건) — 새로 등록", "",
                      "| 정본 메뉴명 | 등록 가격 |", "|---|---:|"]
            for item in missing:
                lines.append(f"| {item['name']} | {won(expected_price(item, overrides.get((item['sku'], ch)), ch))} |")
            lines += ["", "> 이 채널에서 안 팔 메뉴면 웹 /menu 에서 해당 채널 '판매' 체크를 끄세요.", ""]

    lines += ["---", f"**총 처리 항목: {total_fix}건.** 처리 후 `worker\\menu_report.bat` 로",
              "다시 수집하고 `worker\\menu_diff.bat` 를 실행하면 지시서가 갱신됩니다.", ""]

    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"지시서 작성 완료: {REPORT} (총 {total_fix}건)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
