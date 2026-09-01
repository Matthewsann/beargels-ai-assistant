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
    # 계산은 db.channel_diff() 정본 하나 — 웹 첫 화면·작업지시서와 같은 숫자를
    # 낸다(예전엔 이 파일이 세 번째 사본이라 서로 다른 답을 냈다). 여기서는
    # 그 결과를 마크다운으로 그리기만 한다.
    diff = db.channel_diff()

    lines = [f"# 채널별 메뉴 수정 지시서 — {datetime.now():%Y-%m-%d %H:%M}", ""]
    lines += ["정본(직원용 웹 /menu)과 각 채널에 실제 노출 중인 메뉴를 비교한 결과입니다.",
              "각 채널 포털에서 아래 표를 위에서부터 처리하고, 끝나면 다시 수집해",
              "이 지시서가 비는지 확인하세요. **일부러 다르게 둘 항목은 고치는 대신",
              "웹 /menu 의 '채널 예외'에 기록**하세요 — 다음 지시서부터 빠집니다.", ""]

    total_fix = 0
    for ch in ("baemin", "coupang", "naver"):
        d = diff.get(ch)
        lines.append(f"## {CH_LABEL[ch]}")
        if not d:
            lines += ["", "⚠ 수집된 스냅샷이 없습니다. 먼저 메뉴 수집을 실행하세요.", ""]
            continue
        when = (d.get("collected_at") or "")[:16].replace("T", " ")
        lines.append(f"(수집 시각: {when})\n")

        by_type = {}
        for t in d["items"]:
            by_type.setdefault(t["type"], []).append(t)
        n = d["counts"]["total"]
        total_fix += n
        if n == 0:
            lines += ["✅ 정본과 완전히 일치합니다.", ""]
            continue

        price_fix = by_type.get("price", [])
        if price_fix:
            lines += [f"### 1) 가격 수정 ({len(price_fix)}건)", "",
                      "| 채널 메뉴명 | 현재 | → 바꿀 가격 |", "|---|---:|---:|"]
            for t in sorted(price_fix, key=lambda x: -abs((x["cur"] or 0) - x["to"])):
                lines.append(f"| {t['name']} | {won(t['cur'])} | **{won(t['to'])}** |")
            lines.append("")
        name_fix = by_type.get("name", [])
        if name_fix:
            lines += [f"### 2) 이름 수정 ({len(name_fix)}건)", "",
                      "| 현재 채널 표기 | → 정본 표기 |", "|---|---|"]
            for t in name_fix:
                lines.append(f"| {t['name']} | **{t['to']}** |")
            lines.append("")
        maybe = by_type.get("maybe", [])
        if maybe:
            lines += [f"### 3) 같은 메뉴인지 확인 ({len(maybe)}건) — 사람 판단 필요", "",
                      "| 채널 메뉴명 | 채널가 | 정본 후보 | 후보 기준가 |", "|---|---:|---|---:|"]
            for t in maybe:
                lines.append(f"| {t['name']} | {won(t.get('cur'))} | {t['guess']} | {won(t.get('guessPrice'))} |")
            lines += ["", "> 같은 메뉴면 채널 이름·가격을 정본에 맞추고, 다른 메뉴면 정본에 추가하세요.", ""]
        extra = by_type.get("extra", [])
        if extra:
            lines += [f"### 4) 정본에 없는 메뉴 ({len(extra)}건) — 내리거나, 정본에 추가", "",
                      "| 채널 메뉴명 | 채널가 |", "|---|---:|"]
            for t in extra:
                lines.append(f"| {t['name']} | {won(t.get('cur'))} |")
            lines += ["", "> 옛 메뉴면 채널에서 내리고, 계속 팔 메뉴면 웹 /menu 정본에 추가하세요.", ""]
        add = by_type.get("add", [])
        if add:
            lines += [f"### 5) 채널에 없는 메뉴 ({len(add)}건) — 새로 등록", "",
                      "| 정본 메뉴명 | 등록 가격 |", "|---|---:|"]
            for t in add:
                lines.append(f"| {t['name']} | {won(t.get('to'))} |")
            lines += ["", "> 이 채널에서 안 팔 메뉴면 웹 /menu 에서 해당 채널 '판매' 체크를 끄세요.", ""]

    lines += ["---", f"**총 처리 항목: {total_fix}건.** 처리 후 `worker\\menu_report.bat` 로",
              "재수집하면 처리된 항목이 지시서에서 사라집니다.", ""]
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"지시서 저장: {REPORT} (총 {total_fix}건)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
