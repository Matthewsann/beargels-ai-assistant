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
              "blog_media", "blog_learn", "blog_react", "blog_plan", "blog_score")

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
    blog_media.pull_uploads()               # 폰에서 올린 사진부터 소재함에
    before = len(blog_media.load_index())
    idx = blog_media.build_index()
    photos = sum(1 for v in idx.values() if v.get("kind") == "photo")
    videos = len(idx) - photos
    added = len(idx) - before
    grew = f"새 사진 {added}장 · " if added > 0 else ""
    try:
        blog_media.publish_catalog()      # 웹의 '사진 선택'이 새 사진을 보게
    except Exception as e:  # noqa: BLE001
        logger.warning("사진 목록 발행 실패: %s", str(e)[:100])
    return len(idx), f"{grew}사진함 사진 {photos}장 · 영상 {videos}개"


# 다시 뽑기 전의 본문을 어디에 두나 — 새 표(마이그레이션)는 사장님 블로커라
# 만들지 않는다. 이미 범용 key-value 창고로 쓰고 있는 menu_settings 를 쓴다
# (place_keywords·sales_goals 와 같은 방식). 글 하나당 **직전 1개**만, 최근
# 글 10개까지만 남긴다 — 되돌리기는 "방금 것과 그 전 것" 사이를 오가는 기능이지
# 판본 보관함이 아니다.
VERSIONS_KEY = "blog_draft_versions"


def keep_version(post_id, post: dict) -> None:
    """다시 쓰기 직전의 본문을 보관한다(웹의 '이전 초안으로' 버튼이 읽는다)."""
    try:
        from database import supabase_client as sdb
        store_all = sdb.get_setting(VERSIONS_KEY) or {}
        store_all[str(post_id)] = {
            "title": post.get("title") or "",
            "body": post.get("body") or "",
            "at": store._now(),
        }
        if len(store_all) > 10:            # 오래된 글부터 버린다
            for k in sorted(store_all, key=lambda k: store_all[k].get("at") or "")[:-10]:
                store_all.pop(k, None)
        sdb.menu_set_setting(VERSIONS_KEY, store_all)
    except Exception as e:  # noqa: BLE001 — 보관 실패가 다시 뽑기를 막으면 안 된다
        logger.warning("이전 초안 보관 실패(%s): %s", post_id, str(e)[:120])


# ── 매장 정보 블록 ──────────────────────────────────────────────────────
# 글 끝의 [매장 정보]는 AI 가 쓰게 두면 안 된다. 금고의 영업시간 칸이 `[예: …]`
# 예시인 채라 AI 가 그걸 보고 시간을 지어냈다(2026-09-15 실측: 글#2 08:30~21:30,
# 글#3 08:00~21:00 — 둘 다 거짓). 사장님이 웹에서 적은 값(menu_settings.store_info)
# 하나를 원천으로, 초안 때 찍고 임시저장 때 다시 찍는다 — 값이 바뀌면 옛 초안도
# 네이버에 넣는 순간 최신이 된다. 빈 칸은 줄 자체를 안 쓴다(지어내지 않는다).
STORE_INFO_KEY = "store_info"
STORE_FIELDS = (            # (키, 라벨) — 화면·블록 순서
    ("name", "상호"), ("address", "주소"), ("hours", "영업시간"),
    ("closed", "휴무"), ("phone", "전화"), ("parking", "주차"), ("delivery", "배달"),
)
_STORE_FALLBACK = {         # 웹에 아직 아무것도 안 적었을 때 — 금고 매장정보.md 의 확정값만
    "name": "베어글스 송도 타임스페이스점",
    "address": "인천광역시 연수구 하모니로 158 C동 108호",
    "delivery": "배달의민족 · 쿠팡이츠",
}


def store_info() -> dict:
    """사장님이 웹에 적은 매장 정보. 없으면 금고 확정값만."""
    try:
        from database import supabase_client as sdb
        v = sdb.get_setting(STORE_INFO_KEY) or {}
    except Exception:  # noqa: BLE001
        v = {}
    out = dict(_STORE_FALLBACK)
    out.update({k: (v.get(k) or "").strip() for k, _ in STORE_FIELDS if (v.get(k) or "").strip()})
    return out


def store_block(info: dict | None = None) -> str:
    """[매장 정보] 고정 블록. 비어 있는 항목은 줄을 만들지 않는다."""
    info = info or store_info()
    lines = ["[매장 정보]"]
    for k, label in STORE_FIELDS:
        if info.get(k):
            lines.append(f"- {label}: {info[k]}")
    return "\n".join(lines)


_STORE_RE = re.compile(r"\[매장 정보\][^\n]*(?:\n(?![ \t]*\n)[^\n]*)*")


def stamp_store_block(body: str, info: dict | None = None) -> str:
    """본문의 [매장 정보] 블록을 고정 블록으로 바꿔 넣는다(없으면 해시태그 앞에 붙인다)."""
    block = store_block(info)
    body = body or ""
    if _STORE_RE.search(body):
        return _STORE_RE.sub(lambda _m: block, body, count=1)
    m = re.search(r"\n(?:#\S+\s*)+$", body)          # 맨 끝 해시태그 문단 앞에
    if m:
        return body[:m.start()].rstrip() + "\n\n" + block + "\n" + body[m.start():]
    return body.rstrip() + "\n\n" + block + "\n"


# 메뉴 관리 DB 의 매장 판매가 — 채점기가 늘 "구체적 가격"을 요구하는데(2026-09-15
# 실측) 금고엔 가격이 없어 AI 가 못 쓰거나 지어낼 위험이 있었다. 블로그가 인용할
# 카테고리만 골라 확정 사실로 준다(배달가 아님 — 매장가).
MENU_FACT_CATEGORIES = ("베이커리", "샌드위치", "샐러드", "산도", "케이크", "세트",
                        "시그니처", "커피", "논커피", "크림치즈")
MENU_FACT_MAX = 70


def menu_facts_text() -> str:
    """확정 메뉴·매장가 목록(프롬프트용). DB 가 안 닿으면 빈 문자열."""
    try:
        from database import supabase_client as sdb
        items = sdb.menu_all() or []
    except Exception as e:  # noqa: BLE001
        logger.warning("메뉴 사실 읽기 실패(무시): %s", str(e)[:100])
        return ""
    by_cat: dict = {}
    for it in items:
        if not it.get("store_active", True) or not it.get("store_price"):
            continue
        cat = it.get("category") or ""
        if cat not in MENU_FACT_CATEGORIES:
            continue
        by_cat.setdefault(cat, []).append((it.get("name") or "").strip() + f" {int(it['store_price']):,}원")
    lines, n = [], 0
    for cat in MENU_FACT_CATEGORIES:
        names = by_cat.get(cat) or []
        if not names:
            continue
        take = names[: max(2, (MENU_FACT_MAX - n) // max(1, len(MENU_FACT_CATEGORIES)))]
        lines.append(f"- {cat}: " + " · ".join(take))
        n += len(take)
        if n >= MENU_FACT_MAX:
            break
    return "\n".join(lines)


def store_facts_text() -> str:
    """AI 프롬프트용 '확정 매장 사실' — 이것만 쓰고, 없는 건 언급하지 말라고 못박는다."""
    info = store_info()
    lines = [f"- {label}: {info[k]}" for k, label in STORE_FIELDS if info.get(k)]
    missing = [label for k, label in STORE_FIELDS if not info.get(k)]
    txt = "\n".join(lines)
    if missing:
        txt += "\n- (미정 — 본문에서 언급하지 말 것: " + ", ".join(missing) + ")"
    menu = menu_facts_text()
    if menu:
        txt += ("\n[확정 메뉴·매장가 — 글에서 가격을 말할 땐 이 값만 그대로(다른 숫자 금지)]\n" + menu)
    return txt


def do_draft(payload: dict) -> tuple[int, str]:
    """기획 주제로 초안 작성 → blog_posts 에 저장.

    AI 에게 사진함 목록을 먼저 보여주고 그 사진으로 글을 짜게 한 다음,
    본문의 사진 번호([📷 P07])를 **파일 경로로 굳혀서** 저장한다.
    사진함이 나중에 바뀌어도 이 글이 쓰던 사진은 그대로 남는다.
    """
    import planner
    # post_id 가 있으면 '다시 뽑기' — 새 글을 만들지 않고 그 글을 새로 쓴다.
    # 직전 본문은 보관해서 웹에서 되돌릴 수 있게 한다(사장님 요청 2026-09-06:
    # 초안이 별로일 때 다시 뽑되, 앞의 것과 비교해 고를 수 있어야 한다).
    post_id = payload.get("post_id")
    reason = (payload.get("reason") or "").strip()
    old = store.get_post(post_id) if post_id else None
    topic = (payload.get("topic") or payload.get("title") or "").strip()
    main_keyword = (payload.get("main_keyword") or "").strip()
    if old:                       # 빠진 값은 원래 글에서 가져온다
        topic = topic or old.get("title") or ""
        main_keyword = main_keyword or (old.get("main_keyword") or "")

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
    subs = payload.get("sub_keywords") or (old or {}).get("sub_keywords") or []
    data = planner.make_draft_data(
        topic=topic,
        post_type=post_type,
        title=payload.get("title") or topic,
        main_keyword=main_keyword,
        sub_keywords=subs,
        only_rels=payload.get("photos") or None,   # 승인된 배분안의 블로그 몫
        retry_reason=reason,
        facts=store_facts_text(),                  # 영업시간 등은 이 값만 — 지어내지 않게
        except_post_id=post_id if old else None,   # 다시 뽑기는 자기 사진을 다시 써도 된다
    )
    body = data.get("body") or ""
    photo_note = ""
    try:
        import blog_media
        # 번호 표시([📷 P07])를 경로로 굳혀 품질 게이트가 깨끗한 표시를 보게 한다.
        # 개수 세기·미리보기는 게이트 **뒤에** 한 번만 한다(아래) — 퇴고가 표시를
        # 복제하거나 지어낼 수 있어서(2026-09-15 실측: 같은 사진이 두 번, 메시지는
        # '0장'이라고 거짓말 — 예전 코드의 try/else 가 꼬여 있었다).
        body = blog_media.freeze_marks(body)
        body, _ = blog_media.dedupe_marks(body)
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
        kwn = body.count(data.get("main_keyword") or "") if data.get("main_keyword") else None
        if kwn is not None:
            q_note += f" · 키워드 {kwn}회" + ("" if kwn >= 3 else " ⚠")
    except Exception as e:  # noqa: BLE001 — 평가 실패가 저장을 막으면 안 된다
        logger.warning("품질 평가 실패: %s", str(e)[:120])

    # 퇴고가 사진 표시를 새로 지어내는 일이 있다(2026-09-14 글#3 실측: `[📷 사진:
    # 이른 아침 햇살이…]` 같은 설명형 표시 6개). 표시 굳히기를 퇴고 **뒤에** 한 번
    # 더 돌려 사진함에 없는 표시는 지우고, 파일명만 적힌 것은 경로로 굳힌다.
    # AI 가 제목을 본문 첫 줄에 한 번 더 쓴다(2026-09-15 글#20 실측) — 네이버는 제목 칸이
    # 따로 있어 그대로 두면 제목이 두 번 보인다. 맨 앞(사진 표시 뒤)의 제목 줄을 걷어낸다.
    _title = (data.get("title") or topic or "").strip()
    if _title:
        _lines = body.split("\n")
        for _k, _ln in enumerate(_lines[:4]):
            if _ln.strip() and not _ln.lstrip().startswith("[") and \
               re.sub(r"\W", "", _ln) == re.sub(r"\W", "", _title):
                del _lines[_k]
                body = "\n".join(_lines)
                break
    try:
        import blog_media
        body = blog_media.freeze_marks(body)
        body, dropped = blog_media.dedupe_marks(body)      # 퇴고가 복제한 표시도 여기서 잡는다
        # 무료 모델은 사진을 3~4장만 놓는다 — 절 내용에 맞는 안 쓴 사진으로 7장까지 채운다
        body, filled = blog_media.fill_photos(body, except_post_id=post_id if old else None)
        media = blog_media.used_media(body)
        # ⚠ 아래 if/else 는 한 덩어리다 — 두 번이나(9-14, 9-15) 사이에 줄을 끼워 넣다가
        #   else 가 엉뚱한 if 에 붙어 "사진 0장" 거짓 메시지가 났다. 사이에 아무것도 넣지 말 것.
        if media:
            photo_note = (f" · 사진 {len(media)}장"
                          + (f"(자동 채움 {filled})" if filled else "")
                          + (f"(겹친 {dropped}장 뺌)" if dropped else ""))
            blog_media.ensure_thumbs(media)                # 웹 미리보기
        else:
            photo_note = " · ⚠ 사진 0장 — 사진함을 확인해 주세요"
        nw = len(blog_media.wishes(body))
        if nw:
            photo_note += f" · 📸 사진 부탁 {nw}개(글 화면에서 확인)"
        try:
            pool = len(blog_media.catalog(except_post_id=post_id if old else None))
            if pool < blog_media.THIN_POOL:
                photo_note += f" · ⚠ 아직 안 쓴 사진이 {pool}장뿐 — 소재함에 사진을 더 올려주세요"
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001
        logger.warning("퇴고 뒤 사진 표시 정리 실패: %s", str(e)[:100])

    # ★ 해시태그를 본문 맨 끝에 문단으로 넣는다(사장님 지적 2026-08-28 —
    #   태그가 DB에만 있고 네이버엔 안 들어가고 있었다). 네이버 공식 태그칸은
    #   발행(예약) 레이어에만 있는데 그건 이제 사람이 직접 다루므로, 태그
    #   노출은 본문 해시태그로 잡는다. 태그칸은 사람이 발행할 때 직접 채운다.
    # AI 가 프롬프트의 강조를 흉내 내 본문에 **굵게** 를 쓴다(2026-09-15 실측) — 네이버는
    # 별표를 그대로 찍는다. 저장 전에 걷어낸다(퇴고·키워드 보강 뒤라 여기서 한 번).
    body = re.sub(r"\*\*(.+?)\*\*", r"\1", body)
    body = re.sub(r"(?<![*\w])\*(?!\*)([^*\n]+?)\*(?![*\w])", r"\1", body)
    body = stamp_store_block(body)      # AI 가 뭐라고 썼든 [매장 정보]는 확정값으로

    tags = [t.strip().lstrip("#").replace(" ", "") for t in (data.get("tags") or [])]
    tags = [t for t in tags if t][:10]
    if tags and "#" + tags[0] not in body:
        body = body.rstrip() + "\n\n" + " ".join("#" + t for t in tags)

    if old:
        keep_version(post_id, old)          # 되돌리기용으로 직전 본문 보관
        store.update_post(
            post_id, title=data.get("title") or old.get("title"), body=body,
            main_keyword=data.get("main_keyword") or old.get("main_keyword"),
            sub_keywords=data.get("sub_keywords"), tags=tags,
            # 네이버에 넣어둔 임시저장본은 이제 이 글과 다르다 — 다시 넣어야 한다
            prepared_at=None,
        )
    else:
        post_id = store.save_post(
            title=data.get("title"), body=body, post_type=post_type,
            main_keyword=data.get("main_keyword"),
            sub_keywords=data.get("sub_keywords"), tags=tags,
        )
    if quality is not None:
        try:
            import blog_quality
            blog_quality.record(post_id, data.get("title") or topic, quality)
        except Exception:  # noqa: BLE001
            pass
    if brief and not old:
        _brief_link(brief["id"], post_id, data.get("title") or topic)
    try:
        blog_media.publish_catalog()        # 이 글이 쥔 사진은 ③ 고르기 목록에서 빠진다
    except Exception as e:  # noqa: BLE001
        logger.warning("사진 목록 발행 실패: %s", str(e)[:100])
    head = "초안 다시 뽑기 완료" if old else "초안 저장 완료"
    return 1, (f"{head} (#{post_id}){q_note}{photo_note}"
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


# ④ 품질 확인 — 초안 때 매긴 점수는 사장님이 사진을 바꾸고 문장을 고치면 낡는다.
# 임시저장 전에 **지금 글 그대로**를 다시 채점하고, 원하면 개선점대로 다듬는다.
# 결과는 웹이 읽는 menu_settings(범용 key-value)에 둔다 — 새 표를 만들지 않는다.
SCORES_KEY = "blog_quality_scores"


def _body_hash(body: str) -> str:
    import hashlib
    return hashlib.sha1((body or "").encode("utf-8")).hexdigest()[:16]


def do_score(payload: dict) -> tuple[int, str]:
    """글 하나를 채점(mode=score)하거나, 직전 채점의 개선점대로 다듬고 재채점(mode=polish)."""
    import blog_media
    import blog_quality
    import evaluator
    from database import supabase_client as sdb

    post_id = payload.get("post_id")
    post = store.get_post(post_id) if post_id else None
    if not post:
        raise ValueError(f"글 #{post_id} 를 찾을 수 없습니다.")
    title = post.get("title") or ""
    kw = post.get("main_keyword") or ""
    body = post.get("body") or ""
    all_scores = sdb.get_setting(SCORES_KEY) or {}
    polished = False

    if payload.get("mode") == "polish":
        prev = all_scores.get(str(post_id)) or {}
        imps = list(prev.get("improvements") or []) + list(prev.get("warns") or [])
        if not imps or prev.get("body_hash") != _body_hash(body):
            imps = (blog_quality.score(body, title, kw).get("improvements") or [])
        better = blog_quality.improve(body, title, kw, imps[:6]) if imps else None
        if better:
            better = blog_media.freeze_marks(better)     # 사진 표시가 깨졌으면 안 쓴다
            if not blog_media.used_media(better):
                better = None
        # 대표 키워드는 다듬기 뒤에도 **반드시** 3~5회·첫 문단(사장님 2026-09-15: "왜 계속
        # 1회냐"). 퇴고 프롬프트에 부탁만 해서는 무료 모델이 흘린다 — 키워드만 끼워 넣는
        # 짧은 호출(ensure_keyword)을 한 번 더 돌린다. 오늘 이전에 만든 초안도 이걸로 고쳐진다.
        cand = better or body
        cand, _kw_n = blog_quality.ensure_keyword(cand, kw)
        if cand.strip() != body.strip():
            keep_version(post_id, post)                   # 되돌릴 수 있게
            store.update_post(post_id, body=cand, prepared_at=None)
            body = cand
            polished = True

    q = blog_quality.score(body, title, kw)
    checks = evaluator.mechanical_check(body, title, kw)
    plain = blog_quality._plain_len(body)
    entry = {
        "score": q.get("score"), "ai_score": q.get("ai_score"),
        "one_line": q.get("one_line", ""), "brand_fit": q.get("brand_fit", ""),
        "improvements": q.get("improvements") or [], "warns": q.get("warns") or [],
        "checks": [{"label": c.get("label"), "value": c.get("value"),
                    "status": c.get("status"), "hint": c.get("hint")} for c in checks],
        "chars": plain, "photos": len(blog_media.used_media(body)),
        "body_hash": _body_hash(body), "at": store._now(), "polished": polished,
    }
    all_scores[str(post_id)] = entry
    if len(all_scores) > 30:
        for k in sorted(all_scores, key=lambda k: all_scores[k].get("at") or "")[:-30]:
            all_scores.pop(k, None)
    sdb.menu_set_setting(SCORES_KEY, all_scores)
    try:
        blog_quality.record(post_id, title, {**q, "revised": polished})
    except Exception:  # noqa: BLE001
        pass
    head = "다듬고 다시 채점" if polished else ("다듬을 게 없어 채점만" if payload.get("mode") == "polish" else "품질 채점")
    return 1, f"{head} — {entry['score']}점 · {plain:,}자 · 경고 {len(entry['warns'])}개 — {title[:30]}"


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
    try:
        import blog_media
        blog_media.pull_uploads()           # 폰에서 올린 사진이 있으면 먼저 가져온다
    except Exception as e:  # noqa: BLE001
        logger.warning("업로드 사진 가져오기 실패: %s", str(e)[:100])
    # 사진 부탁 메모는 사장님용 — 네이버에는 절대 안 나간다(본문엔 남겨 둔다)
    body = blog_media.strip_wishes(body)
    # 매장 정보는 넣는 순간의 최신값으로 — 영업시간이 바뀌었으면 옛 초안도 새 값으로 나간다
    stamped = stamp_store_block(body)
    if stamped != body:
        body = stamped
        # 웹에는 부탁 메모를 남긴 채 매장 정보만 갱신해 둔다
        store.update_post(post_id, body=stamp_store_block(post.get("body") or ""))
    blocks, _prepared = build_blocks(body)

    # 예약 발행(사장님 2026-09-15 — 8/29 의 '임시저장까지만'을 번복). 글마다 사람이
    # 시각을 보고 [예약 발행]을 눌러야만 온다. 초안 생성이 스스로 예약을 걸진 않는다.
    reserve_at = payload.get("reserve_at")
    when = None
    if reserve_at:
        from datetime import datetime, timedelta, timezone
        kst = timezone(timedelta(hours=9))
        when = datetime.fromisoformat(str(reserve_at).replace("Z", "+00:00"))
        when = (when if when.tzinfo else when.replace(tzinfo=kst)).astimezone(kst)
        floor = datetime.now(kst) + timedelta(minutes=15)
        if when < floor:                   # 과거·임박이면 15분 뒤로 밀어 예약
            when = floor
    dry = bool(payload.get("dry_run"))

    cfg = na.load_config()
    headful = bool(cfg.get("naver", {}).get("headful", True))
    pw, ctx, page = na.launch(cfg, headful=headful)
    reserve_note, reserved = "", False
    try:
        doc = {"title": post.get("title"), "body": body, "blocks": blocks or None,
               "tags": post.get("tags") or []}
        if when is not None:
            r_ok, msg = na.reserve_one(page, cfg, doc, when, dry_run=dry)
            reserved = r_ok and not dry
            reserve_note = f" · {msg}" if r_ok else f" · ⚠ 예약 실패({msg}) — 임시저장으로 남김"
            ok = True                      # 폴백 임시저장까지 됐으면 글은 들어간 것
        else:
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
    if reserved:
        store.set_status(post_id, "scheduled", scheduled_at=when.isoformat())

    # ★ **실제로 에디터에 들어간** 사진·클립만 원장에 기록한다.
    #   (insert 함수들이 True/False 를 정직하게 돌려주게 고침 — 08-30)
    inserted = [b["rel"] for b in blocks if b.get("rel") and b.get("inserted")]
    failed = [b["rel"] for b in blocks if b.get("rel") and not b.get("inserted")]
    moved = 0
    try:
        import blog_media
        moved = blog_media.mark_used(inserted, label=f"글 #{post_id}")
        blog_media.publish_catalog()      # 방금 쓴 사진은 고르기 목록에서 빠진다
    except Exception as e:  # noqa: BLE001 — 기록 실패가 발행 성공을 덮으면 안 된다
        logger.warning("원장 기록 실패: %s", str(e)[:120])

    # '사진 N장 포함'은 준비한 개수가 아니라 **실제 들어간 개수**를 말한다
    with_photo = f" (사진·영상 {len(inserted)}개 들어감)" if inserted else " (⚠ 미디어 0개)"
    fail_note = f" · ⚠ {len(failed)}개는 업로드 실패" if failed else ""
    head = "네이버 예약 발행 설정" if reserved else "네이버 임시저장 완료"
    return 1, (f"{head}{with_photo}{fail_note}{reserve_note}"
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
        # 브리프가 고른 키워드도 확인 대상 — 단 **글을 낸 브리프만**.
        # 아직 안 쓴 제안의 키워드까지 확인하면 옛 글이 만든 순위가 그 브리프의
        # 성과로 잘못 붙는다(2026-09-04 검토).
        try:
            from sns_automation import briefs
            for b in briefs.load():
                blog = b.get("blog") or {}
                if not (blog.get("post_id") or blog.get("published_at")):
                    continue
                k = (blog.get("keyword") or "").strip()
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
    "blog_score": do_score,
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
