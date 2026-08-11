"""
[에이전트 ⑧] 피드백 (노출/유입 분석) + 학습 루프.

네이버 블로그 통계(크리에이터 어드바이저)의 조회수·유입 검색어를 분석해
1) data/월간리포트.md  — 사람이 읽는 성과 리포트
2) data/keywords-learned.yaml — 잘 되는 키워드 (⓪시장조사·①기획에 자동 주입)
을 만듭니다.

통계 입력 방법 (둘 중 편한 것)
------------------------------
A. --open : 자동화 크롬 프로필로 크리에이터 어드바이저를 열어줌
   → 화면의 유입검색어/조회수를 data/stats-input.yaml 에 붙여넣기 (양식 자동 생성)
B. 직접 data/stats-input.yaml 편집

    python src/feedback.py --open      # 통계 화면 열기 + 입력 양식 생성
    python src/feedback.py             # 입력된 통계 분석 → 리포트 + 학습 키워드

※ 통계 화면 자동 수집(스크래핑)은 임시저장 자동화가 PC에서 안정화된 뒤
   셀렉터를 맞춰 추가하는 것을 권장합니다 (에이전트-설계.md 4장).
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATS_IN = DATA / "stats-input.yaml"
LEARNED = DATA / "keywords-learned.yaml"

TEMPLATE = """# 네이버 블로그 통계 입력 (크리에이터 어드바이저 보고 채우기)
# 채우는 곳: 글 제목 / 조회수 / 그 글로 들어온 유입 검색어
# 예시 형식을 지우고 실제 값으로 바꾸세요. 대략적인 값이어도 충분합니다.

period: "{period}"
posts:
  - title: "예시) 송도 베이글 맛집, 소금빵 후기"
    views: 120
    keywords: ["송도 소금빵", "송도 베이글"]
  - title: "예시) 베이글 데우는 법"
    views: 300
    keywords: ["베이글 데우는법", "베이글 에어프라이어"]
"""


def cmd_open() -> None:
    """자동화 크롬 프로필로 통계 화면을 열고, 입력 양식을 준비한다."""
    DATA.mkdir(exist_ok=True)
    if not STATS_IN.exists():
        STATS_IN.write_text(
            TEMPLATE.format(period=dt.date.today().strftime("%Y-%m")), encoding="utf-8")
        print(f"✓ 입력 양식 생성: {STATS_IN.relative_to(ROOT)}")

    try:
        import naver_autodraft as na
        cfg = na.load_config()
        pw, ctx, page = na.launch(cfg, headful=True)
        blog_id = cfg["naver"]["blog_id"]
        page.goto(f"https://creator-advisor.naver.com/naver_blog/{blog_id}/analytics")
        print("크리에이터 어드바이저를 열었습니다. 유입검색어·조회수를 보고")
        print(f"{STATS_IN.name} 을 채운 뒤, python src/feedback.py 를 실행하세요.")
        input("확인이 끝나면 Enter … ")
        ctx.close(); pw.stop()
    except Exception as e:
        print(f"브라우저 열기 실패({e}) — https://creator-advisor.naver.com 에 직접 접속해 확인하세요.")


def analyze() -> None:
    if not STATS_IN.exists():
        print("data/stats-input.yaml 이 없습니다. 먼저:  python src/feedback.py --open")
        return
    stats = yaml.safe_load(STATS_IN.read_text(encoding="utf-8")) or {}
    posts = [p for p in stats.get("posts", []) if "예시)" not in str(p.get("title", ""))]
    if not posts:
        print("입력된 통계가 없습니다(예시만 있음). stats-input.yaml 을 채워주세요.")
        return

    posts.sort(key=lambda p: p.get("views", 0), reverse=True)
    kw_score: dict[str, int] = {}
    for p in posts:
        for kw in p.get("keywords", []):
            kw = str(kw).strip()
            if kw:
                kw_score[kw] = kw_score.get(kw, 0) + int(p.get("views", 0))
    top_kw = sorted(kw_score.items(), key=lambda x: x[1], reverse=True)

    # 1) 사람용 리포트
    period = stats.get("period", dt.date.today().strftime("%Y-%m"))
    lines = [f"# 📊 블로그 성과 리포트 — {period}", "",
             "## 조회수 TOP 글", ""]
    for i, p in enumerate(posts[:5], 1):
        lines.append(f"{i}. **{p.get('title','')}** — {p.get('views',0)}회")
    lines += ["", "## 유입 검색어 TOP (조회수 가중)", ""]
    for kw, score in top_kw[:10]:
        lines.append(f"- {kw} ({score})")
    lines += ["", "## 다음 달 제안",
              "- TOP 유입 키워드는 자동으로 기획 재료에 반영되었습니다 (keywords-learned.yaml).",
              "- TOP 글과 같은 유형의 글을 늘리고, 조회 낮은 유형은 줄여보세요."]
    report = DATA / f"리포트_{period}.md"
    report.write_text("\n".join(lines), encoding="utf-8")

    # 2) 학습 루프: 잘 되는 키워드 → ⓪시장조사 시드·①기획에 주입
    learned = [kw for kw, _ in top_kw[:8]]
    LEARNED.write_text(yaml.safe_dump(
        {"updated": dt.date.today().isoformat(), "keywords": learned,
         "top_posts": [p.get("title", "") for p in posts[:3]]},
        allow_unicode=True, sort_keys=False), encoding="utf-8")

    print(f"✓ 리포트: {report.relative_to(ROOT)}")
    print(f"✓ 학습 키워드 {len(learned)}개 → 다음 시장조사·기획에 자동 반영")
    for kw in learned:
        print(f"    · {kw}")


def main() -> None:
    ap = argparse.ArgumentParser(description="피드백: 통계 분석 → 리포트 + 학습 키워드")
    ap.add_argument("--open", action="store_true", help="통계 화면 열기 + 입력 양식 생성")
    args = ap.parse_args()
    if args.open:
        cmd_open()
    else:
        analyze()


if __name__ == "__main__":
    main()
