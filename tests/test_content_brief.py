"""콘텐츠 브리프·네이버 실측·입고 검수 회귀 테스트 (네트워크·AI·ffmpeg 불필요).

왜 이 테스트가 있나(설계 2026-09-04):
    주제 하나가 촬영→릴스→블로그→성과를 관통해야 "이 주제는 블로그에선 됐고
    릴스에선 안 됐다"를 말할 수 있다. 예전엔 채널마다 저장소가 달라서 그 판단
    자체가 불가능했다.

계약:
  · 브리프 상태는 앞으로만 간다(늦게 온 잡이 진행을 되돌리지 않는다)
  · 폴더 이름·프로젝트 id·글 번호 어느 쪽으로도 같은 브리프를 찾는다
  · 판정은 규칙이다 — 계정 평균 대비로 채널을 갈라 한 문장
  · 네이버 실측: 자동완성=수요, 블로그탭 정면 경쟁글=경쟁, 등급은 규칙
  · 입고 검수: 짧은 클립·어두움·흔들림을 등급으로 가른다(밝기를 먼저 본다)
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sns_automation import briefs, intake_qc  # noqa: E402
from sns_automation import naver_search as ns  # noqa: E402


# ── 브리프 저장·상태 ──────────────────────────────────────────

@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(briefs, "PATH", str(tmp_path / "briefs.json"))
    pushed = []
    monkeypatch.setattr(briefs, "push", lambda items=None: pushed.append(1))
    return pushed


def test_create_and_find(store):
    b = briefs.create("구운 대파 크림치즈", why="9월 초 선선",
                      insta={"hook_angle": "구워서 넣었습니다", "shots": [{"what": "단면", "secs": 3}]},
                      blog={"keyword": "송도 베이글 크림치즈", "angle": "조합 추천"})
    assert b["status"] == briefs.PROPOSED and b["id"].startswith("b")
    assert briefs.get(b["id"])["topic"] == "구운 대파 크림치즈"
    # 폴더 이름으로 찾기 — 폴더칸이 비어 있으면 주제명으로 맞춘다
    assert briefs.by_folder("구운 대파 크림치즈")["id"] == b["id"]
    assert briefs.by_folder(r"C:\소재\구운 대파 크림치즈")["id"] == b["id"]
    assert briefs.by_folder("없는 폴더") is None
    briefs.patch(b["id"], folder="구운대파", insta={"project_id": "p1"})
    assert briefs.by_folder("구운대파")["id"] == b["id"]
    assert briefs.by_project("p1")["id"] == b["id"]
    briefs.patch(b["id"], blog={"post_id": 7})
    assert briefs.by_post(7)["id"] == b["id"]


def test_patch_merges_nested_and_keeps_others(store):
    b = briefs.create("주제", insta={"hook_angle": "훅", "shots": [1]})
    briefs.patch(b["id"], insta={"project_id": "p9"})
    got = briefs.get(b["id"])
    assert got["insta"] == {"hook_angle": "훅", "shots": [1], "project_id": "p9"}


def test_status_moves_forward_only(store):
    b = briefs.create("주제")
    briefs.set_status(b["id"], briefs.MAKING)
    assert briefs.get(b["id"])["status"] == briefs.MAKING
    briefs.set_status(b["id"], briefs.SHOOTING)          # 늦게 온 잡
    assert briefs.get(b["id"])["status"] == briefs.MAKING
    briefs.set_status(b["id"], briefs.PUBLISHED)
    assert briefs.get(b["id"])["status"] == briefs.PUBLISHED
    briefs.set_status(b["id"], "이상한상태")
    assert briefs.get(b["id"])["status"] == briefs.PUBLISHED


def test_live_excludes_proposed_and_closed(store):
    a = briefs.create("제안만")
    b = briefs.create("진행중")
    briefs.set_status(b["id"], briefs.ARRIVED)
    c = briefs.create("끝난것")
    briefs.set_status(c["id"], briefs.CLOSED)
    ids = [x["id"] for x in briefs.live()]
    assert ids == [b["id"]] and a["id"] not in ids


# ── 판정 규칙(6단계) ─────────────────────────────────────────

def test_verdict_splits_channels():
    b = {"topic": "과일 산도", "insta": {"published_at": 1, "likes": 30, "saves": 12,
                                     "hook_angle": "귤을 통째로"},
         "blog": {"keyword": "송도 과일 산도", "rank": 5, "published_at": 1}}
    v = briefs.verdict_line(b, avg_likes=10)
    assert "릴스는 잘 됐다" in v["line"] and "블로그는 됐다" in v["line"]
    assert "저장 12" in v["line"]
    assert any("한 번 더" in a for a in v["next"])

    bad = {"topic": "x", "insta": {"published_at": 1, "likes": 5},
           "blog": {"keyword": "송도 베이글", "published_at": 1, "rank": None}}
    v2 = briefs.verdict_line(bad, avg_likes=20)
    assert "릴스는 안 됐다" in v2["line"] and "순위권 밖" in v2["line"]
    assert any("다른 훅" in a for a in v2["next"])

    # 성과가 아직 없으면 판정하지 않는다(빈 dict)
    assert briefs.verdict_line({"topic": "x", "insta": {}, "blog": {}}, avg_likes=10) == {}


def test_record_insta_sets_published_and_verdict(store, monkeypatch):
    monkeypatch.setattr(briefs, "_account_avg_likes", lambda: 10.0)
    b = briefs.create("주제", insta={"hook_angle": "훅"})
    briefs.record_insta(b["id"], project_id="p1", published_at=1788500000, likes=40)
    got = briefs.get(b["id"])
    assert got["status"] == briefs.PUBLISHED
    assert "릴스는 잘 됐다" in got["verdict"]["line"]


def test_prompt_context_uses_verdicts(store, monkeypatch):
    monkeypatch.setattr(briefs, "_account_avg_likes", lambda: 10.0)
    b = briefs.create("과일 산도")
    briefs.record_insta(b["id"], published_at=1, likes=40)
    text = briefs.as_prompt_context()
    assert "과일 산도" in text and "릴스는 잘 됐다" in text
    assert briefs.as_prompt_context([]) == ""


def test_card_drops_heavy_fields(store):
    b = briefs.create("주제", insta={"hook_angle": "훅", "shots": [{"what": "a", "secs": 2}]},
                      blog={"keyword": "kw"})
    card = briefs.to_card(briefs.get(b["id"]))
    assert card["keyword"] == "kw" and card["hook_angle"] == "훅"
    assert "why" in card and "insta" not in card       # 중첩 원본은 안 올린다


# ── 네이버 실측 ──────────────────────────────────────────────

AC_JSON = ('{"items":[[["송도 베이글","0"],["송도 베이글 맛집","0"],'
           '["부산 송도 베이글","0"]]]}')

BLOG_HTML = """
<a href="https://blog.naver.com/aaa/224389668150" x>송도 베이글 맛집 후기<i>새 창 열림</i></a>
<a href="https://blog.naver.com/aaa/224389668150">중복 링크</a>
<a href="https://blog.naver.com/bbb/224391350228">부산송도카페 로로베이글</a>
<a href="https://blog.naver.com/beargelssongdo/224000000001">베어글스 송도 베이글 신메뉴</a>
<a href="https://blog.naver.com/ccc">프로필 링크(글 아님)</a>
"""


def test_parse_autocomplete_and_blog_results():
    assert ns.parse_autocomplete(AC_JSON)[:2] == ["송도 베이글", "송도 베이글 맛집"]
    assert ns.parse_autocomplete("깨진 json") == []
    posts = ns.parse_blog_results(BLOG_HTML)
    assert [p["blog_id"] for p in posts] == ["aaa", "bbb", "beargelssongdo"]  # 중복·프로필 제외
    assert posts[0]["title"] == "송도 베이글 맛집 후기"      # '새 창 열림' 제거


def test_exact_hits_and_our_rank():
    posts = ns.parse_blog_results(BLOG_HTML)
    assert ns.exact_hits("송도 베이글", posts) == 2        # 띄어쓰기 무시 매칭
    assert ns.exact_hits("무화과", posts) == 0
    assert ns.our_rank(posts, "beargelssongdo") == 3
    assert ns.our_rank(posts, "없는블로그") is None


def test_our_blog_id_comes_from_config_not_a_guess(monkeypatch):
    """손으로 적어 둔 아이디는 틀린다 — 순위 확인과 같은 출처를 쓴다."""
    ns.our_blog_id.cache_clear()
    monkeypatch.setenv("NAVER_BLOG_ID", "beargels_songdo")
    assert ns.our_blog_id() == "beargels_songdo"
    ns.our_blog_id.cache_clear()


def test_rank_of_matches_rank_checker_shape(monkeypatch):
    """브라우저 없이 순위를 낸다 — 저장·화면이 쓰는 모양 그대로."""
    monkeypatch.setattr(ns, "blog_top", lambda kw, limit=30: ns.parse_blog_results(BLOG_HTML))
    got = ns.rank_of("송도 베이글", "beargelssongdo")
    assert got == {"keyword": "송도 베이글", "found": True, "rank": 3,
                   "page": 1, "pos_in_page": 3, "scanned": 3}
    miss = ns.rank_of("송도 베이글", "없는블로그")
    assert miss["found"] is False and miss["rank"] is None and miss["scanned"] == 3


def test_rank_checker_prefers_http_path(monkeypatch):
    """rank_checker 가 크로미움 없이도 답을 낸다(집 PC 에 브라우저가 없다)."""
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "webapp"))
    import rank_checker as rc
    monkeypatch.setattr(ns, "blog_top", lambda kw, limit=30: ns.parse_blog_results(BLOG_HTML))
    got = rc.check_keyword("송도 베이글", "beargelssongdo")
    assert got["rank"] == 3 and got["scanned"] == 3


def test_verdict_rules():
    assert ns.verdict({"in_autocomplete": True, "exact_hits": 1})["tier"] == "green"
    assert ns.verdict({"in_autocomplete": True, "exact_hits": 5})["tier"] == "yellow"
    assert ns.verdict({"in_autocomplete": True, "exact_hits": 12})["tier"] == "red"
    assert ns.verdict({"in_autocomplete": False, "exact_hits": 0})["tier"] == "red"
    # 이미 우리 글이 상위면 새로 쓰지 말고 보강하라고 한다
    v = ns.verdict({"in_autocomplete": True, "exact_hits": 12, "our_rank": 4})
    assert v["tier"] == "mine" and "보강" in v["why"]


def test_pick_winnable_orders_by_tier_then_competition():
    rows = [
        {"keyword": "a", "exact_hits": 5, "verdict": {"tier": "yellow"}},
        {"keyword": "b", "exact_hits": 2, "verdict": {"tier": "green"}},
        {"keyword": "c", "exact_hits": 0, "verdict": {"tier": "red"}},
        {"keyword": "d", "exact_hits": 1, "verdict": {"tier": "green"}},
    ]
    assert [r["keyword"] for r in ns.pick_winnable(rows)] == ["d", "b", "a"]


def test_branch_filter_drops_other_towns_and_rivals(monkeypatch):
    assert ns.is_useful("송도 베이글 맛집") and ns.is_useful("인천 베이글")
    # '송도'라는 이름의 다른 동네 — 부산·포항에도 송도가 있다
    assert not ns.is_useful("부산 송도 베이글")
    assert not ns.is_useful("포항 송도 카페")
    # 경쟁 가게 상호 — 이길 수 있어도 쓰지 않는다(그 손님은 그 가게를 찾는 사람)
    assert not ns.is_useful("송도 베이글리스트")
    assert not ns.is_useful("송도 베이글로그 브런치")
    assert not ns.is_useful("베이글 만들기")          # 지역이 없다
    monkeypatch.setenv("NAVER_EXCLUDE", "새로생긴빵집")
    assert not ns.is_useful("송도 새로생긴빵집")


def test_prompt_context_marks_tiers():
    data = {"rows": [{"keyword": "송도 크림치즈",
                      "verdict": {"tier": "green", "why": "정면 경쟁글 1개"}}],
            "winnable": ["송도 크림치즈"]}
    text = ns.as_prompt_context(data)
    assert "✅" in text and "송도 크림치즈" in text
    assert ns.as_prompt_context({"rows": []}) == ""


# ── 입고 검수 ────────────────────────────────────────────────

def test_intake_judge_rules():
    good = {"sharp": 30.0, "bright": 120.0, "seconds": 5.0}
    assert intake_qc.judge(good) == (intake_qc.Verdict.OK, "")
    short = intake_qc.judge({"sharp": 30.0, "bright": 120.0, "seconds": 1.2})
    assert short[0] == intake_qc.Verdict.BAD and "짧아요" in short[1]
    blur = intake_qc.judge({"sharp": 8.0, "bright": 120.0, "seconds": 5.0})
    assert blur[0] == intake_qc.Verdict.BAD and "흔들" in blur[1]
    weak = intake_qc.judge({"sharp": 14.0, "bright": 120.0, "seconds": 5.0})
    assert weak[0] == intake_qc.Verdict.WARN
    # 어두우면 선명도도 같이 떨어진다 — 안내는 '어둡다'가 나가야 한다
    dark = intake_qc.judge({"sharp": 8.0, "bright": 40.0, "seconds": 5.0})
    assert dark[0] == intake_qc.Verdict.BAD and "어두워요" in dark[1]
    silent = intake_qc.judge({"sharp": 30.0, "bright": 120.0, "seconds": 5.0, "silent": True})
    assert silent[0] == intake_qc.Verdict.WARN and "소리" in silent[1]


def test_summary_line_speaks_plainly():
    assert "아직 파일이 없어요" in intake_qc.summary_line({"files": 0})
    clean = intake_qc.summary_line({"files": 5, "ok": 5, "bad": [], "missing": []})
    assert "5개 바로 쓸 수 있어요" in clean and "다시 찍을 건 없어요" in clean
    dirty = intake_qc.summary_line({
        "files": 3, "ok": 1,
        "bad": [{"file": "IMG_1.MOV", "why": "흔들렸거나 초점이 안 맞았어요 — 다시 찍는 게 좋아요"}],
        "missing": ["단면 정면 클로즈업"]})
    assert "못 쓰는 1개" in dirty and "IMG_1.MOV" in dirty
    # 어느 샷이 빠졌는지는 알 수 없다 — 단정하지 말고 '모자라다'로만 말한다
    assert "모자라요" in dirty and "안 찍은" not in dirty


#: start_shoot 이 실제로 쓰는 촬영가이드 모양(번호 목록) — 왕복이 되어야 한다.
GUIDE = """■ 구운 대파 크림치즈

왜 지금: 9월 초 선선한 아침

훅(첫 3초): 구워서 넣었습니다

[찍을 샷]
1. 단면 정면 클로즈업, 칼이 다 내려간 뒤 3초 유지 (3초)
2. 크림 늘어나는 순간 (2초)
3. 완성 접시 정면 (2초)

[같은 촬영으로 블로그도 씁니다]
검색 키워드: 송도 베이글 크림치즈
"""


def test_wanted_shots_reads_our_own_guide(tmp_path):
    """우리가 만든 가이드를 우리가 읽는다 — 번호 목록과 불릿 둘 다."""
    folder = tmp_path / "주제"
    folder.mkdir()
    (folder / "촬영가이드.txt").write_text(GUIDE, encoding="utf-8")
    got = intake_qc.wanted_shots(str(folder))
    assert len(got) == 3 and got[1] == "크림 늘어나는 순간 (2초)"

    (folder / "촬영가이드.txt").write_text(
        "[찍을 샷]\n- 단면 클로즈업\n· 크림 늘어남\n📹 접시 정면\n\n짧\n", encoding="utf-8")
    assert intake_qc.wanted_shots(str(folder)) == ["단면 클로즈업", "크림 늘어남", "접시 정면"]


def test_check_folder_counts_missing(tmp_path, monkeypatch):
    folder = tmp_path / "주제"
    folder.mkdir()
    (folder / "촬영가이드.txt").write_text(GUIDE, encoding="utf-8")
    (folder / "a.mp4").write_bytes(b"x")
    (folder / "b.mp4").write_bytes(b"x")
    monkeypatch.setattr(intake_qc, "check_video",
                        lambda p: {"file": Path(p).name, "grade": intake_qc.Verdict.OK})
    res = intake_qc.check_folder(str(folder))
    assert res["files"] == 2 and res["ok"] == 2
    assert res["missing"] == ["완성 접시 정면 (2초)"]     # 3개 계획 중 2개만 도착


# ── 잡 큐 ────────────────────────────────────────────────────

def test_new_jobs_are_registered_and_routed():
    from database import supabase_client as db
    for kind in ("reel_shoot", "content_intake"):
        assert kind in db.INTERACTIVE_JOB_KINDS
    src = (ROOT / "worker" / "agent.py").read_text(encoding="utf-8")
    assert 'job.get("kind") == "reel_shoot"' in src
    assert 'job.get("kind") == "content_intake"' in src
    assert "maybe_intake_qc()" in src and "maybe_naver_research()" in src


def test_pa_can_import_briefs_without_local_files():
    """직원 웹(PA)은 로컬 파일 없이 버킷 사본만 읽는다 — 무거운 import 금지."""
    import ast
    src = (ROOT / "sns_automation" / "briefs.py").read_text(encoding="utf-8")
    top = {n.module or "" for n in ast.parse(src).body if isinstance(n, ast.ImportFrom)}
    top |= {a.name for n in ast.parse(src).body if isinstance(n, ast.Import)
            for a in n.names}
    assert top <= {"json", "logging", "os", "re", "time", "__future__"}, top
    deploy = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    assert "upload sns_automation/briefs.py" in deploy
