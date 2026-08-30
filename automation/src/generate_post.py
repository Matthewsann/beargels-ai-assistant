"""
글 초안 자동 생성기 (네이버 SEO 전문가 모드).

- 상위(레포) 폴더의 '네이버-SEO-지식.md' 를 학습 자료로 읽어, C-Rank/D.I.A. 기준을
  지키는 상위 노출용 글을 생성합니다.
- 생성 결과는 콘텐츠 라이브러리(library/)에 status=ready 로 '재고'처럼 쌓입니다.

사용법
------
  python src/generate_post.py --plan          # content-plan.yaml 의 지정 글(신메뉴/이벤트 등) 생성
  python src/generate_post.py --stockpile 5   # 상시 소재(정보성/일상 등) 5개를 새로 쌓기
  python src/generate_post.py --images        # 생성과 함께 이미지 계획/생성까지 (generate_image 연동)
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re

import yaml

import library

# ⚠️ 퇴역(2026-08-30 비용 감사): 이 모듈의 글 생성은 유료 Claude API 를
# 직접 불렀다 — 사장님 원칙("유료 API 에 의지하지 않는다")에 따라 생성
# 기능은 막아 둔다. 글 생성은 웹 기획실(무료 Gemini, llm.complete 경유)로.
# load_config() 등 설정 함수는 generate_image.py 가 계속 쓰므로 남긴다.
Anthropic = None  # 유료 클라이언트 제거

ROOT = pathlib.Path(__file__).resolve().parent.parent
SEO_DOC = ROOT.parent / "네이버-SEO-지식.md"

TYPE_GUIDE = {
    "신메뉴": "신메뉴/대표메뉴 소개. 소개 계기 스토리 → 재료·과정을 감각적으로 → 추천 먹는 법 → 방문 유도.",
    "이벤트": "이벤트/프로모션 홍보. 혜택·기간·참여법이 한눈에. 서두르게 만드는 마무리. 과장 문구 금지.",
    "일상": "일상/비하인드 에세이. 사람 냄새로 단골과 소통. 마지막에만 자연스럽게 매장 연결.",
    "후기": "고객후기 재구성. 실제 손님 반응을 인용하듯 담백하게, 자화자찬 금지. 초대 마무리.",
    "정보성": "정보성 글(베이글/빵/커피 상식). 소제목·번호로 정리, 구체적 숫자 포함. 끝에 매장 연결.",
}

# --stockpile 모드에서 상시로 돌려 쌓을 수 있는 '에버그린' 소재 풀
EVERGREEN_TOPICS = [
    ("정보성", "베이글 가장 맛있게 데우는 법 (에어프라이어/오븐/팬)"),
    ("정보성", "크림치즈 스프레드 종류와 어울리는 베이글 조합"),
    ("정보성", "베이글 vs 일반 빵, 칼로리와 포만감 비교"),
    ("정보성", "베이글 신선하게 보관하는 법과 냉동 후 데우기"),
    ("정보성", "아침 대용으로 베이글이 좋은 이유"),
    ("일상", "새벽 배송부터 첫 손님까지, 베어글스의 아침"),
    ("일상", "주문받고 굽는 이유 — 토스팅 한 번의 차이"),
    ("일상", "곰돌이 캐릭터와 베어글스라는 이름의 뒷이야기"),
    ("후기", "요즘 손님들이 가장 많이 찾는 시그니처 베이글"),
    ("정보성", "베이글에 커피, 어떤 조합이 잘 어울릴까"),
]


def load_config() -> dict:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    cfg_path = ROOT / "config.yaml"
    if not cfg_path.exists():
        raise SystemExit("config.yaml 이 없습니다. config.example.yaml 을 복사해 만들어주세요.")
    return yaml.safe_load(cfg_path.read_text(encoding="utf-8"))


def seo_knowledge() -> str:
    if SEO_DOC.exists():
        return SEO_DOC.read_text(encoding="utf-8")
    return "(네이버-SEO-지식.md 를 찾지 못했습니다. 기본 SEO 원칙으로 작성합니다.)"


def build_prompt(brand: dict, post_type: str, variables: dict, avoid_titles: list[str]) -> str:
    region = brand.get("region", "")
    guide = TYPE_GUIDE.get(post_type, TYPE_GUIDE["정보성"])
    var_lines = "\n".join(f"- {k}: {v}" for k, v in (variables or {}).items())
    avoid = "\n".join(f"- {t}" for t in avoid_titles[-30:]) or "(없음)"

    footer = (
        f"📍 베어글스 {region}점 / {brand.get('address','')} / "
        f"🕒 {brand.get('hours','')} / ☎️ {brand.get('phone','')} / "
        f"📷 인스타그램 {brand.get('instagram','')}"
    )

    return f"""당신은 네이버 블로그 상위 노출을 전문으로 하는 SEO 카피라이터이자,
베어글스 베이커리 카페(Bear+Bagels, 곰 캐릭터 베이글 카페, 지역: {region})의
다정한 사장님 목소리를 완벽히 구사하는 전문가입니다.

🔴 [절대 사실 — 어기면 허위광고]
베이글은 본사에서 새벽 냉동 배송으로 납품받습니다. 매장에서 반죽·발효·오븐 베이킹을
하지 않고, 주문 시 **그릴에 토스팅**해 냅니다. 크림치즈·샌드위치·음료는 매장에서 만듭니다.
- 절대 금지: "직접 반죽", "수제 베이글", "매장에서 굽는", "갓 구운", "새벽부터 반죽",
  "오븐에서 막 나온", "당일 생산", "우리가 쓰는 밀가루" 등 제빵을 암시하는 모든 표현.
- 대신 사용: "주문 즉시 그릴에 구워 겉바속쫀", "매일 새벽 들어오는 신선한 베이글",
  "직접 만든 크림치즈".

아래 '네이버 상위 노출 지식'을 반드시 지켜 글을 작성하세요.

===== 네이버 상위 노출 지식 (필독·준수) =====
{seo_knowledge()}
===== 지식 끝 =====

[톤] 다정하고 따뜻하게, 과장 없이 솔직하게. 1인칭 경험 서술. 이모지 🐻🥯☕️ 문단당 1~2개.

[이번 글 유형] {post_type} — {guide}

[반영할 정보]
{var_lines or "(별도 변수 없음 — 유형과 브랜드에 맞게 사실 기반으로 자연스럽게 작성)"}

[제목·본문 필수 규칙 — 위 지식 기반]
1) 대표 키워드 1개(예: "{region} 베이글")와 세부 키워드 2~3개를 먼저 정한다.
2) 제목: 대표 키워드를 앞쪽에, 25자 내외, 클릭 유도. 낚시 금지.
3) 본문 첫 문단(2~3줄)에 대표 키워드를 자연스럽게 1회 포함.
4) 글자 수 1,500자 이상. 소제목 2~4개로 구조화.
5) 키워드는 자연스럽게 3~5회만(도배 금지). 구체적 숫자(가격·시간·온도) 포함.
6) 사진 위치를 본문에 5~8군데 [📷 사진: 무엇을 어떻게 찍을지]로 안내(실물 촬영용).
7) 본문 맨 아래에 이 정보 블록을 그대로 넣는다:
{footer}

[출력 형식] 아래 JSON 하나만, 순수 JSON 으로 출력(코드블록/설명 금지).
{{
  "main_keyword": "대표 키워드",
  "sub_keywords": ["세부1", "세부2", "세부3"],
  "title": "제목",
  "body": "네이버에 그대로 붙여넣을 본문 전체(사진 위치·정보 블록 포함). 줄바꿈은 \\n.",
  "tags": ["태그10개내외", "..."]
}}

[이미 창고에 있는 제목 — 주제/제목 중복 금지]
{avoid}"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        if brace:
            text = brace.group(0)
    return json.loads(text)


def generate_one(client, cfg: dict, post_type: str, variables: dict) -> dict:
    raise RuntimeError(
        "퇴역한 기능입니다 — 글 생성은 웹 기획실(무료 Gemini)을 쓰세요. "
        "(유료 Claude 직접 호출 차단, 2026-08-30)")
    gen = cfg.get("generate", {})
    msg = client.messages.create(
        model=gen.get("model", "claude-sonnet-5"),
        max_tokens=gen.get("max_tokens", 5000),
        messages=[{
            "role": "user",
            "content": build_prompt(
                cfg.get("brand", {}), post_type, variables, library.existing_titles()
            ),
        }],
    )
    raw = "".join(b.text for b in msg.content if b.type == "text")
    data = _extract_json(raw)
    data.setdefault("tags", [])
    data.setdefault("sub_keywords", [])
    data.setdefault("main_keyword", "")
    return data


def run_plan(client, cfg) -> list[int]:
    plan = yaml.safe_load((ROOT / "content-plan.yaml").read_text(encoding="utf-8"))
    made = []
    for item in plan.get("posts", []):
        ptype = item.get("type", "신메뉴")
        print(f"  · '{ptype}' 생성 중…")
        data = generate_one(client, cfg, ptype, item.get("vars", {}))
        item_id = library.create_item(ptype, data)
        made.append(item_id)
        print(f"    ✓ 라이브러리 #{item_id:04d}  | {data.get('title','')}")
    return made


def run_stockpile(client, cfg, count: int) -> list[int]:
    made = []
    for i in range(count):
        ptype, topic = EVERGREEN_TOPICS[(library.next_id() + i) % len(EVERGREEN_TOPICS)]
        print(f"  · [{i+1}/{count}] '{ptype}' 생성 중… ({topic})")
        data = generate_one(client, cfg, ptype, {"주제": topic})
        item_id = library.create_item(ptype, data)
        made.append(item_id)
        print(f"    ✓ 라이브러리 #{item_id:04d}  | {data.get('title','')}")
    return made


def main() -> None:
    ap = argparse.ArgumentParser(description="네이버 SEO 전문가 모드 글 생성")
    ap.add_argument("--plan", action="store_true", help="content-plan.yaml 의 지정 글 생성")
    ap.add_argument("--stockpile", type=int, metavar="N", help="상시 소재 N개 쌓기")
    ap.add_argument("--images", action="store_true", help="생성 후 이미지 계획/생성까지 실행")
    args = ap.parse_args()

    raise SystemExit(
        "퇴역한 스크립트입니다 — 글 생성은 웹 기획실(무료 Gemini)을 쓰세요. "
        "(유료 Claude 직접 호출 차단, 2026-08-30)")
    cfg = load_config()
    client = Anthropic()
    made: list[int] = []

    if args.plan:
        print("[content-plan.yaml 기반 생성]")
        made += run_plan(client, cfg)
    if args.stockpile:
        print(f"[상시 소재 {args.stockpile}개 쌓기]")
        made += run_stockpile(client, cfg, args.stockpile)
    if not (args.plan or args.stockpile):
        print("[기본: 상시 소재 3개 쌓기]  (옵션: --plan, --stockpile N)")
        made += run_stockpile(client, cfg, 3)

    if args.images and made:
        try:
            import generate_image
            print("\n[이미지 생성/계획]")
            for item_id in made:
                generate_image.process_item(cfg, item_id)
        except Exception as e:
            print(f"  이미지 단계 건너뜀: {e}")

    print(f"\n완료! {len(made)}개를 라이브러리에 적재(status=ready). 창고: automation/library/")


if __name__ == "__main__":
    main()
