"""
[에이전트 ⓪] 시장조사 (트렌드서처).

시드 키워드(config: trend.seeds)로 네이버 자동완성 검색어를 수집해
data/trends.yaml 에 저장합니다. 기획 에이전트(propose_topics.py)의 재료가 됩니다.

- 네이버 자동완성은 '사람들이 실제로 치는 검색어'라 롱테일 키워드 발굴에 유용합니다.
- 공개 엔드포인트라 로그인이 필요 없지만, 형식이 바뀔 수 있어 방어적으로 파싱합니다.
- 수집 실패해도 파이프라인은 계속됩니다(기획이 트렌드 없이도 동작).

    python src/trend_search.py
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import urllib.parse
import urllib.request

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

DEFAULT_SEEDS = ["송도 베이글", "송도 카페", "송도 빵집", "베이글", "송도 브런치"]

AC_URL = "https://ac.search.naver.com/nx/ac?q={q}&con=1&frm=nv&ans=2&r_format=json&r_enc=UTF-8&r_unicode=0&t_koreng=1&run=2&rev=4&q_enc=UTF-8&st=100"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


def load_seeds() -> list[str]:
    try:
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        return (cfg.get("trend", {}) or {}).get("seeds") or DEFAULT_SEEDS
    except Exception:
        return DEFAULT_SEEDS


def fetch_suggestions(query: str) -> list[str]:
    """네이버 자동완성 검색어 수집 (실패 시 빈 목록)."""
    url = AC_URL.format(q=urllib.parse.quote(query))
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.naver.com/"})
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            data = json.loads(res.read().decode("utf-8"))
    except Exception as e:
        print(f"  ⚠ '{query}' 수집 실패: {e}")
        return []
    out: list[str] = []
    # 응답 형식: {"items": [[["검색어", ...], ...], ...]} — 중첩 배열을 방어적으로 순회
    def walk(node):
        if isinstance(node, str):
            s = node.strip()
            if s and s not in out and not s.startswith("http"):
                out.append(s)
        elif isinstance(node, list):
            # 첫 원소가 검색어인 패턴이 일반적이므로 전부 훑되 문자열만 채집
            for x in node:
                walk(x)
    walk(data.get("items", []))
    return out[:15]


def season_hints(today: dt.date) -> list[str]:
    """계절/시기 소재 힌트 — 트렌드 수집이 실패해도 기획 재료가 되도록."""
    m = today.month
    table = {
        (3, 4, 5): ["봄 피크닉 베이글", "딸기 시즌 메뉴", "새학기 아침"],
        (6, 7, 8): ["여름 아이스 음료", "시원한 브런치", "휴가철 간식"],
        (9, 10, 11): ["가을 소풍", "따뜻한 라떼 시작", "수능 선물"],
        (12, 1, 2): ["크리스마스 선물세트", "연말 모임", "따뜻한 수프와 베이글"],
    }
    for months, hints in table.items():
        if m in months:
            return hints
    return []


def main() -> None:
    seeds = load_seeds()
    today = dt.date.today()
    print(f"시드 {len(seeds)}개로 트렌드 수집…")

    collected: dict[str, list[str]] = {}
    total = 0
    for seed in seeds:
        sug = fetch_suggestions(seed)
        collected[seed] = sug
        total += len(sug)
        print(f"  · {seed}: {len(sug)}건")

    DATA.mkdir(exist_ok=True)
    out = {
        "updated": today.isoformat(),
        "seeds": seeds,
        "suggestions": collected,       # 시드별 자동완성(실검색어)
        "season_hints": season_hints(today),
    }
    (DATA / "trends.yaml").write_text(
        yaml.safe_dump(out, allow_unicode=True, sort_keys=False), encoding="utf-8")

    if total == 0:
        print("\n수집 0건(네트워크/형식 문제 가능) — 계절 힌트만 저장했습니다. 기획은 계속 동작합니다.")
    else:
        print(f"\n완료: 총 {total}건 → data/trends.yaml (기획 에이전트가 사용)")


if __name__ == "__main__":
    main()
