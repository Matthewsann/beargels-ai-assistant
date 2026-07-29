r"""채널 메뉴 수집 + 결과 리포트 — 집 PC 무인 실행용.

왜 필요한가:
    개발은 클라우드 세션에서, 실행은 집 PC 에서 한다. 그런데 logs/ 와 debug/ 는
    .gitignore 로 빠져 있어 **PC 에서 무슨 일이 있었는지 클라우드가 볼 수 없다**.
    이 스크립트는 실행 결과를 reports/ 에 요약 파일로 남긴다. reports/ 는 커밋
    대상이라, PC 가 push 하면 클라우드에서 git pull 로 그대로 읽을 수 있다.

    Claude 가 필요 없다. 파이썬만 돌면 된다.

무엇을 남기나:
    · 채널별 수집 건수와 상태(정상 / 0건 / 실패)
    · 실패 원인 분류 — 크롬 미실행, 로그인 만료, 화면 구조 변경 등
    · 다음에 사람이 할 일(로그인 다시 하기 등)
    · debug/ 덤프 파일 이름(민감정보라 내용은 안 올리고 이름만)

    메뉴 이름·가격 같은 실제 데이터는 리포트에 넣지 않는다. 요약만 남긴다.

실행:
    worker\menu_report.bat        (수집 → 리포트 → 자동 commit+push)
    python scripts/menu_report.py (리포트만, push 안 함)
"""
from __future__ import annotations

import logging
import pathlib
import socket
import sys
import traceback
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPORTS_DIR = ROOT / "reports"
DEBUG_DIR = ROOT / "debug"

logger = logging.getLogger("menu_report")

# 채널별로 "0건일 때 사람이 뭘 해야 하는지" — 리포트에 그대로 적는다.
CHANNEL_HINTS = {
    "baemin": "scripts/launch_chrome.bat 으로 띄운 크롬에서 self.baemin.com 로그인 확인",
    "coupang": "같은 크롬에서 store.coupangeats.com 로그인 확인",
    "naver": "같은 크롬에서 new.smartplace.naver.com 네이버 로그인 확인",
}


def _cdp_alive(port: int = 9222) -> bool:
    """디버그 크롬(원격 디버깅 포트)이 떠 있는지 확인."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def _classify(channel: str, rows, error: BaseException | None) -> dict:
    """수집 결과를 상태·원인·조치로 분류한다."""
    if error is not None:
        msg = str(error)
        if "CDP attach" in msg or "connect_over_cdp" in msg:
            cause = "디버그 크롬이 꺼져 있음"
            action = "scripts/launch_chrome.bat 실행 후 각 사이트 로그인"
        else:
            cause = f"{type(error).__name__}: {msg[:160]}"
            action = "리포트 하단 상세 오류를 클라우드 세션에 전달"
        return {"status": "실패", "count": 0, "cause": cause, "action": action}

    count = len(rows or [])
    if count == 0:
        return {"status": "0건", "count": 0,
                "cause": "로그인 만료 또는 화면 구조 변경 의심",
                "action": CHANNEL_HINTS.get(channel, "로그인 상태 확인")}
    return {"status": "정상", "count": count, "cause": "", "action": ""}


def _recent_dumps(since: datetime, limit: int = 10) -> list[str]:
    """이번 실행 중 새로 생긴 debug 덤프 파일 이름만 모은다(내용은 안 읽음)."""
    if not DEBUG_DIR.exists():
        return []
    cutoff = since.timestamp()
    names = [p.name for p in DEBUG_DIR.glob("menu_*.txt")
             if p.stat().st_mtime >= cutoff]
    return sorted(names)[:limit]


def collect() -> tuple[list[dict], list[str]]:
    """세 채널을 순서대로 수집하고 채널별 결과와 상세 오류를 돌려준다."""
    from crawler import menu_scrape

    fetchers = (
        ("baemin", "배민", menu_scrape.fetch_baemin_menus),
        ("coupang", "쿠팡이츠", menu_scrape.fetch_coupang_menus),
        ("naver", "네이버", menu_scrape.fetch_naver_menus),
    )
    results, details = [], []
    for key, label, fetch in fetchers:
        rows, error = None, None
        try:
            rows = fetch()
        except Exception as e:  # noqa: BLE001 — 한 채널이 죽어도 나머지는 계속
            error = e
            details.append(f"### {label}\n\n```\n{traceback.format_exc()[-1500:]}\n```")
            logger.error("%s 수집 실패: %s", label, e)
        info = _classify(key, rows, error)
        info.update(channel=key, label=label, rows=rows or [])
        results.append(info)
        logger.info("%s — %s (%d건)", label, info["status"], info["count"])
    return results, details


def save_snapshots(results: list[dict]) -> str:
    """수집분을 Supabase 에 저장(설정이 없으면 조용히 건너뛴다)."""
    ok = [r for r in results if r["count"] > 0]
    if not ok:
        return "저장할 데이터 없음"
    try:
        from database import supabase_client as db
    except Exception as e:  # noqa: BLE001 — DB 미설정 PC 에서도 리포트는 나와야 한다
        return f"건너뜀(DB 사용 불가: {type(e).__name__})"
    saved = 0
    for r in ok:
        try:
            saved += db.save_menu_snapshots(r["channel"], r["rows"])
        except Exception as e:  # noqa: BLE001
            return f"실패({type(e).__name__}: {str(e)[:80]})"
    return f"{saved}건 저장"


def render(results: list[dict], details: list[str], started: datetime,
           dumps: list[str], chrome_ok: bool, saved: str) -> str:
    """사람이 읽고, 클라우드 세션도 그대로 읽을 수 있는 마크다운 리포트."""
    stamp = started.strftime("%Y-%m-%d %H:%M")
    total = sum(r["count"] for r in results)
    bad = [r for r in results if r["status"] != "정상"]
    headline = "정상" if not bad else f"확인 필요 ({len(bad)}개 채널)"

    lines = [
        f"# 채널 메뉴 수집 리포트 — {stamp}",
        "",
        f"- **결과**: {headline}",
        f"- **총 수집**: {total}건",
        f"- **디버그 크롬**: {'실행 중' if chrome_ok else '꺼져 있음 ⚠️'}",
        f"- **DB 저장**: {saved}",
        "",
        "## 채널별",
        "",
        "| 채널 | 상태 | 건수 | 원인 |",
        "| --- | --- | ---: | --- |",
    ]
    for r in results:
        mark = {"정상": "✅", "0건": "⚠️", "실패": "❌"}[r["status"]]
        lines.append(f"| {r['label']} | {mark} {r['status']} | {r['count']} "
                     f"| {r['cause'] or '-'} |")

    todo = [r for r in results if r["action"]]
    lines += ["", "## 사람이 할 일", ""]
    if todo:
        lines += [f"- **{r['label']}**: {r['action']}" for r in todo]
    else:
        lines.append("- 없음. 모든 채널 정상 수집.")

    if dumps:
        lines += ["", "## 디버그 덤프 (PC 의 debug/ 폴더, 내용은 커밋 안 함)", ""]
        lines += [f"- `{n}`" for n in dumps]

    if details:
        lines += ["", "## 상세 오류", ""] + details

    lines += ["", "---", "",
              "이 파일은 `scripts/menu_report.py` 가 자동 생성합니다. "
              "직접 고치지 마세요.", ""]
    return "\n".join(lines)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S")

    started = datetime.now()
    chrome_ok = _cdp_alive()
    if not chrome_ok:
        logger.warning("디버그 크롬(9222)이 꺼져 있음 — 수집이 대부분 실패할 수 있음")

    results, details = collect()
    saved = save_snapshots(results)
    logger.info("DB 저장 — %s", saved)
    dumps = _recent_dumps(started)

    REPORTS_DIR.mkdir(exist_ok=True)
    body = render(results, details, started, dumps, chrome_ok, saved)
    dated = REPORTS_DIR / f"menu-{started:%Y%m%d-%H%M}.md"
    dated.write_text(body, encoding="utf-8")
    (REPORTS_DIR / "latest-menu.md").write_text(body, encoding="utf-8")
    logger.info("리포트 저장: %s", dated.relative_to(ROOT))

    # 채널이 하나라도 정상이면 성공으로 본다(전부 실패해야 실패 코드).
    return 0 if any(r["status"] == "정상" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
