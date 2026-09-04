"""집 PC 일꾼의 블로그 담당 — 클라우드 웹이 요청한 블로그 작업을 실제로 실행한다.

리뷰 수집과 똑같은 구조다:
    [클라우드 웹] jobs 에 요청  →  [집 PC 일꾼] 이 파일이 실행  →  Supabase 에 결과 기록

처리하는 작업(job kind):
    blog_recommend  금고를 읽고 글감 10개 추천        (AI)
    blog_draft      기획 주제로 초안 작성 → 창고 저장   (AI)
    blog_publish    글을 네이버에 임시저장(초안) 넣기    (브라우저)
    blog_rank       타겟 키워드 네이버 순위 확인        (브라우저)
    blog_media      사진함에 새로 올린 사진 살펴보기     (AI)

사진은 드라이브 '베어글스_블로그_사진함' 에서 가져온다(blog_media.py).
초안을 쓸 때 이미 사진을 골라 본문에 박아 두고, 네이버 초안 넣기에서
그 사진들을 실제로 올린다 — 사장님이 에디터에서 사진을 찾아 넣을 일이 없다.

무거운 일(AI 호출·크롬 조작)은 전부 여기서만 한다. 클라우드 웹은 버튼과 결과 표시만.
실제 '발행' 버튼은 사장님이 네이버에서 직접 누른다(자동 발행하지 않는다).
"""

from __future__ import annotations

import logging
import re
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "worker", ROOT / "webapp", ROOT / "automation" / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from database import blog_store as store  # noqa: E402

logger = logging.getLogger(__name__)

BLOG_KINDS = ("blog_recommend", "blog_draft", "blog_publish", "blog_rank",
              "blog_media", "blog_learn", "blog_react", "blog_plan")

# 순위 추적 기본 키워드(창고 글의 대표 키워드에 더해 항상 확인)
DEFAULT_KEYWORDS = ("송도 베이글", "송도 카페")


def handles(kind: str) -> bool:
    return (kind or "").startswith("blog_")


# ---------------------------------------------------------------------------
# 개별 작업
# ---------------------------------------------------------------------------

def _brief(brief_id) -> dict | None:
    """콘텐츠 브리프 하나 — 없거나 모듈이 없으면 None(예전 흐름 그대로)."""
    if not brief_id:
        return None
    try:
        from sns_automation import briefs
        return briefs.get(str(brief_id))
    except Exception as e:  # noqa: BLE001 — 브리프가 없어도 초안은 써야 한다
        logger.warning("브리프 읽기 실패(%s): %s", brief_id, str(e)[:120])
        return None


def _brief_link(brief_id: str, post_id: int, title: str) -> None:
    """초안이 나왔다 → 브리프에 글 번호를 붙이고 '제작중'으로."""
    try:
        from sns_automation import briefs
        briefs.patch(brief_id, blog={"post_id": post_id, "title": title})
        briefs.set_status(brief_id, briefs.MAKING)
        briefs.push()
    except Exception as e:  # noqa: BLE001
        logger.warning("브리프 연결 실패(%s): %s", brief_id, str(e)[:120])


def do_recommend() -> tuple[int, str]:
    """금고 기반 글감 추천 → blog_recommendations 테이블 교체."""
    import planner
    items = planner.make_recommendations()
    store.replace_recommendations(items)
    return len(items), f"글감 {len(items)}개 추천"


def do_media() -> tuple[int, str]:
    """사진함을 다시 훑어 새로 올라온 사진을 AI 가 살펴본다."""
    import blog_media
    before = len(blog_media.load_index())
    idx = blog_media.build_index()
    photos = sum(1 for v in idx.values() if v.get("kind") == "photo")
    videos = len(idx) - photos
    added = len(idx) - before
    grew = f"새 사진 {added}장 · " if added > 0 else ""
    return len(idx), f"{grew}사진함 사진 {photos}장 · 영상 {videos}개"


def do_draft(payload: dict) -> tuple[int, str]:
    """기획 주제로 초안 작성 → blog_posts 에 저장.

    AI 에게 사진함 목록을 먼저 보여주고 그 사진으로 글을 짜게 한 다음,
    본문의 사진 번호([📷 P07])를 **파일 경로로 굳혀서** 저장한다.
    사진함이 나중에 바뀌어도 이 글이 쓰던 사진은 그대로 남는다.
    """
    import planner
    topic = (payload.get("topic") or payload.get("title") or "").strip()
    main_keyword = (payload.get("main_keyword") or "").strip()

    # ── 콘텐츠 브리프에서 왔으면 그 지시를 따른다(설계 2026-09-04) ──
    #    같은 촬영으로 릴스와 블로그를 만들되, 글의 각도와 대표 키워드는
    #    네이버 실측 위에서 정해진 것을 쓴다. 브리프가 없으면 예전 그대로.
    brief = _brief(payload.get("brief_id"))
    if brief:
        b_blog = brief.get("blog") or {}
        topic = topic or brief.get("topic", "")
        main_keyword = main_keyword or (b_blog.get("keyword") or "")
        if b_blog.get("angle"):
            topic = f"{topic} — {b_blog['angle']}"
    if not topic:
        raise ValueError("주제가 비어 있습니다.")
    post_type = payload.get("post_type") or "정보성"
    data = planner.make_draft_data(
        topic=topic,
        post_type=post_type,
        title=payload.get("title") or topic,
        main_keyword=main_keyword,
        sub_keywords=payload.get("sub_keywords") or [],
        only_rels=payload.get("photos") or None,   # 승인된 배분안의 블로그 몫
    )
    body = data.get("body") or ""
    photo_note = ""
    try:
        import blog_media
        body = blog_media.freeze_marks(body)
        media = blog_media.used_media(body)
        if media:
            photo_note = f" · 사진 {len(media)}장"
        else:
            photo_note = " · ⚠ 사진 0장 — 사진함을 확인해 주세요"
    except Exception as e:  # noqa: BLE001 — 사진을 못 붙여도 글은 저장한다
        logger.warning("사진 붙이기 실패: %s", str(e)[:120])

    # ★ 품질 게이트 — 점수를 매기고, 기준 미달이면 개선점을 먹여 1회 자동 퇴고.
    #   낮아도 저장은 한다(점수가 메시지에 붙어 사장님이 걸러 볼 수 있게).
    q_note = ""
    quality = None
    try:
        import blog_quality
        body, quality = blog_quality.gate(
            body, data.get("title") or topic, data.get("main_keyword") or "")
        q_note = f" · 품질 {quality['score']}점"
        if quality.get("revised"):
            q_note += f"(퇴고로 {quality.get('before_score')}→{quality['score']})"
    except Exception as e:  # noqa: BLE001 — 평가 실패가 저장을 막으면 안 된다
        logger.warning("품질 평가 실패: %s", str(e)[:120])

    # ★ 해시태그를 본문 맨 끝에 문단으로 넣는다(사장님 지적 2026-08-28 —
    #   태그가 DB에만 있고 네이버엔 안 들어가고 있었다). 네이버 공식 태그칸은
    #   발행(예약) 레이어에만 있는데 그건 이제 사람이 직접 다루므로, 태그
    #   노출은 본문 해시태그로 잡는다. 태그칸은 사람이 발행할 때 직접 채운다.
    tags = [t.strip().lstrip("#").replace(" ", "") for t in (data.get("tags") or [])]
    tags = [t for t in tags if t][:10]
    if tags and "#" + tags[0] not in body:
        body = body.rstrip() + "\n\n" + " ".join("#" + t for t in tags)

    post_id = store.save_post(
        title=data.get("title"), body=body, post_type=post_type,
        main_keyword=data.get("main_keyword"), sub_keywords=data.get("sub_keywords"),
        tags=tags,
    )
    if quality is not None:
        try:
            import blog_quality
            blog_quality.record(post_id, data.get("title") or topic, quality)
        except Exception:  # noqa: BLE001
            pass
    if brief:
        _brief_link(brief["id"], post_id, data.get("title") or topic)
    return 1, (f"초안 저장 완료 (#{post_id}){q_note}{photo_note}"
               f" — {data.get('title', '')[:40]}")


def build_blocks(body: str) -> tuple[list[dict], int]:
    """본문을 '글 토막 + 올릴 사진 파일' 순서로 바꾼다.

    사진은 여기서 미리 업로드용으로 손질한다(세로사진 회전·HEIC 변환·1600px 축소).
    사진함을 못 읽으면 글자만 넣는 예전 방식으로 조용히 되돌아간다.
    """
    try:
        import blog_media
    except Exception:  # noqa: BLE001
        return [], 0
    raw, _media = blog_media.resolve_body(body)
    blocks, n = [], 0
    for b in raw:
        if b.get("type") == "text":
            # 마크다운을 베어글스 서식 블록으로 푼다:
            #   "## 소제목"  → heading 블록(에디터에서 19크기+굵게)
            #   "---"       → divider 블록(구분선)
            #   나머지 문단  → 일반 텍스트(가운데 정렬은 에디터에서 일괄)
            for chunk in re.split(r"\n(?=#{1,4}\s|-{3,}\s*$)",
                                  b.get("text", ""), flags=re.MULTILINE):
                chunk = chunk.strip("\n")
                if not chunk.strip():
                    continue
                m = re.match(r"^#{1,4}\s*(.+)$", chunk.split("\n")[0])
                if m:
                    blocks.append({"type": "text", "style": "heading",
                                   "text": m.group(1).strip()})
                    rest = "\n".join(chunk.split("\n")[1:]).strip("\n")
                    if rest.strip():
                        blocks.append({"type": "text", "text": rest})
                    continue
                if re.match(r"^-{3,}\s*$", chunk.split("\n")[0]):
                    blocks.append({"type": "divider"})
                    rest = "\n".join(chunk.split("\n")[1:]).strip("\n")
                    if rest.strip():
                        blocks.append({"type": "text", "text": rest})
                    continue
                blocks.append({"type": "text", "text": chunk})
            continue
        try:
            path = blog_media.prepare(b["rel"]) if b["type"] == "photo" \
                else blog_media.full_path(b["rel"])
            blocks.append({"type": b["type"], "path": str(path),
                           "rel": b["rel"], "caption": b.get("caption", "")})
            n += 1
        except Exception as e:  # noqa: BLE001 — 사진 한 장 때문에 글 전체를 막지 않는다
            logger.warning("사진 준비 실패(%s): %s", b.get("rel"), str(e)[:100])
    return blocks, n


def do_publish(payload: dict) -> tuple[int, str]:
    """글 하나를 네이버 임시저장(초안)으로 넣는다. 발행 예약은 사장님이 직접.

    본문에 박아 둔 사진도 이때 같이 올라간다 — 사장님이 에디터에서
    사진을 찾아 넣을 일이 없다는 게 이 기능의 핵심이다. 여기서 하는 건
    딱 임시저장까지 — 실제 '예약 발행' 버튼을 누르는 최종 행위는 사람이
    네이버에서 직접 한다(사장님 확정 2026-08-29: 자동 예약은 하지 않는다).
    """
    import naver_autodraft as na
    post_id = payload.get("post_id")
    post = store.get_post(post_id) if post_id else None
    if not post:
        raise ValueError(f"글 #{post_id} 를 찾을 수 없습니다.")

    body = post.get("body") or ""
    blocks, _prepared = build_blocks(body)

    cfg = na.load_config()
    headful = bool(cfg.get("naver", {}).get("headful", True))
    pw, ctx, page = na.launch(cfg, headful=headful)
    try:
        doc = {"title": post.get("title"), "body": body, "blocks": blocks or None,
               "tags": post.get("tags") or []}
        ok = na.draft_one(page, cfg, doc)
    finally:
        try:
            ctx.close()
            pw.stop()
        except Exception:  # noqa: BLE001
            pass
    if not ok:
        # 실패 원인을 사장님이 읽을 수 있는 말로 (2026-08-30 감사: 로그인
        # 만료가 "화면 구조가 바뀌었을 수 있어요"로 둔갑해 원인을 못 찾았다)
        reason = getattr(na, "LAST_ERROR", "")
        if reason == "login_expired":
            raise RuntimeError(
                "네이버 로그인이 만료됐어요 — 집 PC에서 automation 폴더의 "
                "로그인(login_helper.py)을 다시 실행해 주세요.")
        raise RuntimeError("네이버 에디터 입력 실패 (화면 구조가 바뀌었을 수 있어요)")
    store.update_post(post_id, prepared_at=store._now())

    # ★ **실제로 에디터에 들어간** 사진·클립만 원장에 기록한다.
    #   (insert 함수들이 True/False 를 정직하게 돌려주게 고침 — 08-30)
    inserted = [b["rel"] for b in blocks if b.get("rel") and b.get("inserted")]
    failed = [b["rel"] for b in blocks if b.get("rel") and not b.get("inserted")]
    moved = 0
    try:
        import blog_media
        moved = blog_media.mark_used(inserted, label=f"글 #{post_id}")
    except Exception as e:  # noqa: BLE001 — 기록 실패가 발행 성공을 덮으면 안 된다
        logger.warning("원장 기록 실패: %s", str(e)[:120])

    # '사진 N장 포함'은 준비한 개수가 아니라 **실제 들어간 개수**를 말한다
    with_photo = f" (사진·영상 {len(inserted)}개 들어감)" if inserted else " (⚠ 미디어 0개)"
    fail_note = f" · ⚠ {len(failed)}개는 업로드 실패" if failed else ""
    return 1, (f"네이버 임시저장 완료{with_photo}{fail_note}"
               f" — {post.get('title', '')[:40]}")


LEARN_FILE = ROOT / "knowledge" / "블로그-배운점.md"

LEARN_PROMPT = """너는 베어글스 송도점 블로그의 SEO·브랜드 편집장이다.
AI 가 쓴 블로그 글을 사장님이 직접 고쳤다. 아래에 '고치기 전'과 '고친 후'가 있다.

⚠️ 사장님의 수정이 항상 정답은 아니다(사장님 본인이 확인해 준 사실이다).
너는 편집장으로서 **비판적으로** 골라내라:
- **사실 교정(메뉴 이름·재료·가격·주소·영업 정보)** → 사장님이 가게의 사실을
  제일 잘 안다. 무조건 채택(type "사실").
- **말투·표현·구성 수정** → SEO(키워드·분량·구조)와 브랜드 톤 기준으로 판단해서
  ①따를 가치가 있으면 type "표현"으로 채택
  ②오히려 상위노출·가독성을 해치면(키워드 삭제, 분량 대폭 축소, 정보 삭제 등)
    type "주의"로 기록 — 다음 글에 따라하지 말고, 사장님과 상의할 거리다.
- 사진 표시([📷 …], [🎬 …]) 이동/삭제와 오탈자 수준은 무시.
- 교훈이 없으면 빈 배열.

[고치기 전]
{before}

[고친 후]
{after}

JSON 배열만 출력(설명·코드블록 금지):
[{{"type":"사실|표현|주의","wrong":"","right":"","lesson":"다음부터 이렇게 (주의면: 왜 따르면 안 되는지)"}}]"""


def do_learn(payload: dict) -> tuple[int, str]:
    """사장님의 본문 수정에서 교훈을 뽑아 knowledge/블로그-배운점.md 에 쌓는다.

    이 파일은 금고(knowledge/)라 다음 초안·글감 추천 프롬프트에 자동 포함된다
    — 같은 실수를 두 번 하지 않게 하는 학습 루프의 저장소.
    """
    import json as _json
    import re as _re
    from datetime import date

    import llm
    before = (payload.get("before") or "").strip()
    after = (payload.get("after") or "").strip()
    if not before or not after:
        return 0, "비교할 내용이 없습니다."

    raw = llm.complete(user=LEARN_PROMPT.format(before=before[:6000],
                                                after=after[:6000]),
                       max_tokens=1200, prefer="gemini")
    m = _re.search(r"\[.*\]", raw, _re.DOTALL)
    lessons = _json.loads(m.group(0)) if m else []
    lessons = [l for l in lessons if l.get("lesson") or l.get("right")]
    if not lessons:
        return 0, "특별히 배울 수정이 아니었어요."

    if not LEARN_FILE.exists():
        LEARN_FILE.write_text(
            "# 블로그 배운점 — 사장님 수정에서 자동으로 배운 것\n\n"
            "> 사장님이 비서 페이지에서 본문을 고치면, 그 차이에서 뽑은 교훈이\n"
            "> 여기 자동으로 쌓입니다. 이 파일은 다음 글을 쓸 때 항상 함께 읽힙니다.\n"
            "> ❗사실 교정이 반복되면 금고 본체(매장정보.md 등)로 옮겨 확정하세요.\n\n",
            encoding="utf-8")

    today = date.today().isoformat()
    post_id = payload.get("post_id")
    lines = []
    for l in lessons:
        t = l.get("type")
        if t == "사실":
            lines.append(f"- ❗사실({today}, 글#{post_id}): "
                         f"'{l.get('wrong', '')}' 는 틀림 → **{l.get('right', '')}**. "
                         f"{l.get('lesson', '')}")
        elif t == "주의":
            # 사장님 수정이지만 SEO·가독성엔 손해 — 따라하지 말고 상의 거리로 남긴다
            lines.append(f"- ⚠️주의({today}, 글#{post_id}): {l.get('lesson', '')} "
                         f"(사장님 수정이지만 다음 글에 그대로 따르지 말 것)")
        else:
            lines.append(f"- 표현({today}, 글#{post_id}): {l.get('lesson', '')}")
    with LEARN_FILE.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    facts = sum(1 for l in lessons if l.get("type") == "사실")
    return len(lessons), (f"배운 것 {len(lessons)}개 기록"
                          + (f" (잘못된 정보 교정 {facts}건 ❗)" if facts else ""))


PLAN_PROMPT = """너는 베어글스 송도점의 멀티채널 콘텐츠 디렉터다.
아래는 주제 「{topic}」 폴더에 있는 실제 소재 목록이다(번호|종류|내용|키워드).

{materials}

이 소재들을 채널별로 배분하라. 채널마다 목적이 다르다:
- blog: 네이버 검색 상위노출 — 과정·정보 사진 4~6장 + 짧은 클립 1개(있으면)
- insta: 릴스 — 가장 임팩트 있는 순간(자르기·단면·크림) 중심의 릴스 컨셉 한 줄 + 커버 사진 1장
- danggeun: 당근 동네생활 — 친근한 사진 1~2장(사람 냄새 나는 컷 우선)
- place: 네이버 플레이스 소식 — 완성품이 잘 보이는 대표컷 1~2장

배분 원칙:
1. **채널끼리 같은 사진·영상을 써도 된다**(보는 사람이 다르다 — 사장님 확정).
   각 채널의 목적에 가장 잘 맞는 컷을 자유롭게 골라라. 제일 좋은 컷은 여러
   채널이 같이 쓰는 게 정상이다.
2. 대신 **이 주제는 이 배분 한 번으로 소진**된다 — 아껴두지 말고 이번에
   제대로 써라. 같은 주제를 나중에 또 우려먹지 않는 것이 규칙이다.
3. 흐린 사진(quality bad)은 쓰지 않는다.
3. 이 주제를 관통하는 한 줄 각도(angle)를 먼저 정한다 — 모든 채널이 같은 이야기를 다른 문법으로.
4. 소재 번호(P01, V01)로만 가리킨다. 없는 번호를 지어내지 마라.

JSON 하나만 순수 출력(설명·코드블록 금지):
{{"angle": "이 주제의 한 줄 각도",
  "channels": {{
    "blog":     {{"photos": ["P01"], "clip": "V01 또는 null", "title_hint": "제목 힌트"}},
    "insta":    {{"reel": "릴스 컨셉 한 줄", "cover": "P02"}},
    "danggeun": {{"photos": ["P03"], "copy_hint": "당근 글 힌트 한 줄"}},
    "place":    {{"photos": ["P04"], "copy_hint": "플레이스 소식 한 줄"}}
  }},
  "note": "배분 이유·주의 한 줄"}}"""


def do_plan(payload: dict) -> tuple[int, str]:
    """주제 하나의 소재를 채널별로 배분하는 안을 만들어 웹 승인 대기열에 올린다."""
    import json as _json
    import re as _re

    import blog_media
    import llm

    idx = blog_media.load_index()
    topic = (payload.get("topic") or "").strip()
    if not topic:
        # 상시(_)가 아닌 주제 중 소재가 가장 많은 것
        from collections import Counter
        counts = Counter(v.get("slot") for v in idx.values()
                         if not (v.get("slot") or "_").startswith("_"))
        if not counts:
            raise ValueError("배분할 주제 폴더가 없습니다. 원본소재에 주제 폴더를 만들어 주세요.")
        topic = counts.most_common(1)[0][0]

    # 이 주제의 소재만 번호표를 붙여 보여준다 (어느 채널도 안 쓴 것 위주)
    sub = {rel: v for rel, v in idx.items() if v.get("slot") == topic}
    if not sub:
        raise ValueError(f"주제 「{topic}」 에 소재가 없습니다.")
    cat = blog_media.catalog(index=sub, channel="__plan__")  # 원장 필터 없이 전부
    materials = blog_media.catalog_text(cat, limit=80)

    raw = llm.complete(user=PLAN_PROMPT.format(topic=topic, materials=materials),
                       max_tokens=1500, prefer="gemini")
    m = _re.search(r"\{.*\}", raw, _re.DOTALL)
    plan = _json.loads(m.group(0)) if m else {}
    if not plan.get("channels"):
        raise RuntimeError("배분안 생성 실패 — 다시 시도해 주세요.")

    # 번호(P01)를 실제 파일 경로로 굳혀 저장한다(번호는 다음 스캔에 밀린다)
    def to_rel(pid):
        item = cat.get((pid or "").strip().upper())
        return item["rel"] if item else None

    for ch, c in (plan.get("channels") or {}).items():
        if not isinstance(c, dict):
            continue
        if c.get("photos"):
            c["photos"] = [r for r in (to_rel(p) for p in c["photos"]) if r]
        for key in ("clip", "cover"):
            if c.get(key):
                c[key] = to_rel(c[key])

    plan_id = store.save_plan(topic, plan)
    n = sum(len(c.get("photos") or []) + (1 if c.get("cover") else 0)
            + (1 if c.get("clip") else 0)
            for c in plan["channels"].values() if isinstance(c, dict))
    return 1, f"배분안 #{plan_id} — 「{topic}」 소재 {n}개를 4개 채널에 배분 (웹에서 승인해 주세요)"


def do_react() -> tuple[int, str]:
    """발행 감지(RSS→URL 연결) + 공감·댓글 수집 + 발행본에서 배우기."""
    import blog_perf
    freed = blog_perf.release_trashed()
    if freed:
        logger.info("휴지통 글의 소재 %d건을 원장에서 해제", freed)
    linked = blog_perf.sync_published()
    n, likes, comments = blog_perf.collect()
    learned = 0
    try:
        learned = blog_perf.learn_from_published()
    except Exception as e:  # noqa: BLE001 — 학습 실패가 수집을 막으면 안 된다
        logger.warning("발행본 학습 실패: %s", str(e)[:120])
    link_note = f"새 발행 연결 {linked}건 · " if linked else ""
    learn_note = f" · 발행본 학습 {learned}건" if learned else ""
    return n, (f"{link_note}글 {n}개 반응 수집 "
               f"(공감 {likes} · 댓글 {comments}){learn_note}")


def do_rank(payload: dict) -> tuple[int, str]:
    """타겟 키워드들의 네이버 순위를 확인해 blog_ranks 에 기록."""
    import rank_checker as rc
    blog_id = rc.get_blog_id()
    if not blog_id:
        raise ValueError("config.yaml 에서 blog_id 를 찾지 못했습니다.")

    keywords = payload.get("keywords") or []
    if not keywords:
        for p in store.list_posts(limit=100):
            k = (p.get("main_keyword") or "").strip()
            if k and k not in keywords:
                keywords.append(k)
        # 브리프가 고른 키워드도 확인 대상 — 그래야 '블로그에선 됐나'를 판정한다
        try:
            from sns_automation import briefs
            for b in briefs.load():
                k = ((b.get("blog") or {}).get("keyword") or "").strip()
                if k and k not in keywords:
                    keywords.append(k)
        except Exception as e:  # noqa: BLE001
            logger.debug("브리프 키워드 없음: %s", e)
        for k in DEFAULT_KEYWORDS:
            if k not in keywords:
                keywords.append(k)

    results = []
    for kw in keywords:
        try:
            results.append(rc.check_keyword(kw, blog_id))
        except Exception as e:  # noqa: BLE001 — 한 키워드 실패로 전체를 멈추지 않는다
            logger.warning("순위 확인 실패(%s): %s", kw, str(e)[:120])
    store.save_ranks(results)
    try:
        import blog_perf
        blog_perf.brief_ranks(results)          # 브리프 판정에 순위를 먹인다
    except Exception as e:  # noqa: BLE001
        logger.debug("브리프 순위 반영 실패: %s", e)
    found = sum(1 for r in results if r.get("found"))
    return len(results), f"키워드 {len(results)}개 확인 (노출 {found}개)"


# ---------------------------------------------------------------------------
# 진입점 — agent.py 가 부른다
# ---------------------------------------------------------------------------

_HANDLERS = {
    "blog_recommend": lambda p: do_recommend(),
    "blog_draft": do_draft,
    "blog_publish": do_publish,
    "blog_rank": do_rank,
    "blog_media": lambda p: do_media(),
    "blog_learn": do_learn,
    "blog_react": lambda p: do_react(),
    "blog_plan": do_plan,
}


def run(job: dict) -> tuple[int, str]:
    """블로그 잡 1건 처리. (처리 건수, 메시지) 반환. 실패 시 예외를 올린다."""
    kind = job.get("kind")
    handler = _HANDLERS.get(kind)
    if handler is None:
        raise ValueError(f"알 수 없는 블로그 작업: {kind}")
    payload = job.get("payload") or {}
    if isinstance(payload, str):
        import json
        try:
            payload = json.loads(payload)
        except Exception:  # noqa: BLE001
            payload = {}
    return handler(payload)
