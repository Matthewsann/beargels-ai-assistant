"""릴스 발행 기록·인스타 자동 감지 회귀 테스트 (DB·인스타·브라우저 불필요).

왜 이 테스트가 있나(설계 검토 2026-09-04):
    발행은 사람이 하는데 "올렸다"는 사실이 시스템에 안 남아 훅 라이브러리가
    전부 미발행·성과 빈칸이었다. 6단계(성과 → 다음 기획)가 한 번도 못 돌았다.

계약:
  · 캡션 비교는 해시태그·문장부호를 무시하고, 첫 줄이 같으면 같은 릴스로 본다
  · 완성본보다 하루 넘게 오래된 게시물은 후보에서 뺀다(예전에 올린 같은 메뉴)
  · mark_reel_published 는 프로젝트·훅 라이브러리·캘린더·카드 네 곳에 남기고 멱등이다
  · [올렸어요]만 누른 릴스(게시물 ID 없음)도 다음 동기화가 게시물을 찾아 붙여
    좋아요·댓글이 들어온다 — 버튼이 자동 수집을 막으면 안 된다(검토 2026-09-04 #1)
  · 같은 캡션의 프로젝트가 둘이면 게시 시각이 완성 시각에 가까운 쪽에 붙는다
  · 다시 만들면 새 판 — 발행 기록은 history 로 내려가고 옛 게시물은 새 판에 안 붙는다
  · 잘못 눌렀으면 네 곳에서 되돌린다
  · 릴스 id 는 `<epoch>-<슬러그>` 꼴만 받는다(경로 조작·태그 주입 차단)
  · [올렸어요] 잡은 같은 릴스·같은 방향이 대기 중이면 새로 만들지 않는다
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest.importorskip("fastapi")          # sns_automation.webapp 이 FastAPI 앱이다

from sns_automation import cloud_sync, planner  # noqa: E402
from sns_automation import publish_sync as ps  # noqa: E402
from sns_automation import webapp as wa  # noqa: E402

CAP = ("단면 자를 때 제일 긴장되는 메뉴예요. 제철 귤을 통째로 넣고, 크림에는 "
       "산딸기잼을 함께 섞었어요. 달기만 한 게 아니라 새콤한 끝맛이 남아서 "
       "커피랑 잘 어울립니다. 송도 타임스페이스 오시면 오늘 나온 산도부터 한 번 봐주세요 🥯")
# 사장님이 Edits 앱에서 단어 하나 고치고 해시태그를 붙여 올린 모습
IG_CAP = CAP.replace("산딸기잼", "자몽잼") + "\n\n#베어글스 #송도카페 #송도베이글"
OTHER = ("어제 아침, 샌드위치 40인분을 만들었어요.\n\n단체 주문을 주셔서 순서대로 "
         "하나씩 완성했습니다. 송도에서 워크숍 단체 주문 필요하시면 미리 말씀해주세요.")

T_MADE = 1_788_000_000          # 완성본 만든 시각(epoch)
IG_NEW = "2026-09-04T02:00:00+0000"   # 그 뒤에 올린 게시물 (= 1788487200)
IG_OLD = "2026-07-01T02:00:00+0000"   # 완성본보다 훨씬 전 게시물
T_NEW = 1_788_487_200


# ── 순수 함수 ──────────────────────────────────────────────────

def test_normalize_strips_hashtags_and_punctuation():
    assert ps.normalize_caption("귤을 통째로!! #송도카페 @beargels ~") == "귤을 통째로"
    assert ps.first_line("#태그만\n\n귤을 통째로 넣었어요\n둘째 줄") == "귤을 통째로 넣었어요"


def test_similarity_same_first_line_counts_as_match():
    a = "귤을 통째로 넣어봤어요 🍊\n긴 설명이 여기 붙고 내용이 꽤 달라도"
    b = "귤을 통째로 넣어봤어요\n#베어글스 #송도"
    assert ps.caption_similarity(a, b) >= 0.9
    assert ps.caption_similarity(CAP, OTHER) < 0.4


def test_similarity_survives_small_edit_and_hashtags():
    assert ps.caption_similarity(CAP, IG_CAP) >= ps.MIN_RATIO


def test_parse_ig_time_and_norm_url():
    assert ps.parse_ig_time("2026-09-04T02:00:00+0000") == T_NEW
    assert ps.parse_ig_time("") == 0
    assert ps.parse_ig_time("이상한값") == 0
    assert (ps.norm_url("https://www.instagram.com/reel/abc/?igsh=xyz")
            == ps.norm_url("instagram.com/reel/abc"))


def test_check_pid_accepts_project_ids_and_rejects_paths():
    assert ps.check_pid("1788061731-제철-과일산도-단면") == "1788061731-제철-과일산도-단면"
    for bad in ("", "..", "../x", "a/b", "C:\\x", "<img src=x>", "a b", "x" * 121):
        with pytest.raises(ps.PublishError):
            ps.check_pid(bad)


def test_match_post_picks_similar_and_ignores_old():
    p = {"id": "x", "created": T_MADE, "script_caption": CAP}
    posts = [
        {"id": "a", "caption": IG_CAP, "timestamp": IG_NEW},
        {"id": "b", "caption": OTHER, "timestamp": IG_NEW},
    ]
    post, ratio = ps.match_post(p, posts)
    assert post["id"] == "a" and ratio >= ps.MIN_RATIO
    # 같은 캡션이라도 완성본보다 두 달 전 게시물이면 이 릴스가 아니다
    assert ps.match_post(p, [{"id": "c", "caption": IG_CAP, "timestamp": IG_OLD}]) is None
    # 닮은 게 없으면 None
    assert ps.match_post(p, [posts[1]]) is None
    # 캡션이 없는 프로젝트는 맞출 수 없다
    assert ps.match_post({"id": "y", "created": T_MADE}, posts) is None


def test_match_post_respects_manual_mark_time_window():
    """[올렸어요]를 누른 시각보다 하루 넘게 뒤에 올라온 게시물은 그 릴스가 아니다."""
    p = {"id": "x", "created": T_MADE, "script_caption": CAP, "published_at": T_NEW}
    late = {"id": "z", "caption": IG_CAP, "timestamp": "2026-09-10T02:00:00+0000"}
    assert ps.match_post(p, [late]) is None
    assert ps.match_post(p, [{"id": "a", "caption": IG_CAP, "timestamp": IG_NEW}])[0]["id"] == "a"


def test_assign_posts_prefers_closest_in_time_when_captions_tie():
    """같은 메뉴를 두 번 만들어 캡션이 똑같을 때 — 옛 릴스의 게시물이 새 프로젝트에 붙으면 안 된다."""
    a = {"id": "A", "created": T_MADE, "script_caption": CAP}
    b = {"id": "B", "created": T_MADE + 5 * 86400, "script_caption": CAP}
    post = {"id": "y", "caption": IG_CAP, "timestamp": IG_NEW}   # A 완성 5.6일 뒤, B 완성 0.6일 뒤
    got = ps.assign_posts([b, a], [post])          # 새 것부터 넘겨도
    assert len(got) == 1 and got[0][0]["id"] == "B"  # 시각이 가까운 B
    early = {"id": "y2", "caption": IG_CAP, "timestamp": "2026-09-01T02:00:00+0000"}
    got = ps.assign_posts([b, a], [early])          # B 완성보다 2.4일 전 → B 는 창 밖, A 만 가능
    assert got[0][0]["id"] == "A"
    # 게시물이 둘이면 1:1 — 한 게시물이 두 프로젝트에 붙지 않는다
    two = ps.assign_posts([a, b], [post, early])
    assert {g[0]["id"] for g in two} == {"A", "B"} and {g[1]["id"] for g in two} == {"y", "y2"}


# ── 기록 (샌드박스: 로컬 파일만, 클라우드·DB 는 가짜) ───────────────

@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(wa, "PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setattr(planner, "HOOKS_FILE", str(tmp_path / "hooks.json"))
    calls = {"cloud": [], "cloud_undo": [], "calendar": [], "calendar_undo": []}
    monkeypatch.setattr(cloud_sync, "mark_published",
                        lambda pid, at, **kw: calls["cloud"].append((pid, at, kw)) or True)
    monkeypatch.setattr(cloud_sync, "unmark_published",
                        lambda pid, **kw: calls["cloud_undo"].append(pid) or True)
    from database import mkt_store

    def fake_auto_record(**kw):          # 진짜 auto_record 처럼 같은 source_ref 는 한 번만
        if any(c["source_ref"] == kw["source_ref"] for c in calls["calendar"]):
            return None
        calls["calendar"].append(kw)
        return 1
    monkeypatch.setattr(mkt_store, "auto_record", fake_auto_record)
    monkeypatch.setattr(mkt_store, "delete_auto_record",
                        lambda ref: calls["calendar_undo"].append(ref) or 1)
    return calls


def _project(pid, caption, created=T_MADE, versions=1):
    p = {"id": pid, "title": pid.split("-", 1)[1].replace("-", " "), "created": created,
         "status": wa.ST_DONE, "final_path": "완성본/" + pid, "script_caption": caption}
    wa._save_project(p)
    for n in range(versions):
        planner.record_hook(pid, p["title"], f"훅{n}", "reel.mp4" if n == 0 else f"reel_{n+1}.mp4")
    return p


def _hooks(pid):
    return [h for h in planner.get_hook_library() if h["project_id"] == pid]


def test_mark_reel_published_records_everywhere_and_is_idempotent(sandbox):
    pid = "1788000000-제철-과일산도-단면"
    _project(pid, CAP, versions=2)

    res = ps.mark_reel_published(pid, url="https://www.instagram.com/reel/abc/",
                                 at=T_NEW, source="manual")
    assert res["already"] is False and res["at"] == T_NEW

    p = wa._load_project(pid)
    assert p["published"] is True and p["published_at"] == T_NEW
    assert p["ig_permalink"].endswith("/reel/abc/") and p["published_source"] == "manual"

    hooks = _hooks(pid)
    assert len(hooks) == 2 and all(h["published"] for h in hooks)
    assert all(h["permalink"].endswith("/reel/abc/") for h in hooks)

    cal = sandbox["calendar"]
    assert len(cal) == 1 and cal[0]["source_ref"] == f"reel#{pid}"
    assert cal[0]["day"] == "2026-09-04"          # KST 날짜(UTC 02:00 → 11:00)
    assert "제철 과일산도 단면" in cal[0]["title"]
    assert sandbox["cloud"] and sandbox["cloud"][0][0] == pid

    # 두 번 눌러도 안전 — 발행 시각·출처는 처음 것을 지킨다
    res2 = ps.mark_reel_published(pid, at=1799999999, source="auto")
    assert res2["already"] is True
    p = wa._load_project(pid)
    assert p["published_at"] == T_NEW and p["published_source"] == "manual"
    assert all(h["published_at"] == T_NEW for h in _hooks(pid))


def test_mark_unknown_or_bad_project_raises_without_echoing_pid(sandbox):
    with pytest.raises(ps.PublishError) as ei:
        ps.mark_reel_published("1788000000-없는-프로젝트")
    assert "없는-프로젝트" not in str(ei.value)       # 오류 문구가 화면 innerHTML 로 간다
    with pytest.raises(ps.PublishError):
        ps.mark_reel_published("<img src=x onerror=alert(1)>")
    with pytest.raises(ps.PublishError):
        ps.mark_reel_published("../../다른곳")


def test_calendar_failure_does_not_block_record(sandbox, monkeypatch):
    from database import mkt_store

    def boom(**kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(mkt_store, "auto_record", boom)
    pid = "1788000000-잠봉뵈르-베이글"
    _project(pid, CAP)
    res = ps.mark_reel_published(pid)
    assert res["already"] is False and wa._load_project(pid)["published"] is True


def test_unmark_reverts_all_four_places(sandbox):
    pid = "1788000000-제철-과일산도-단면"
    _project(pid, CAP)
    ps.mark_reel_published(pid, at=T_NEW, media_id="a", url="https://www.instagram.com/reel/a/",
                           likes=12, comments=3)
    res = ps.unmark_reel_published(pid)
    assert res["was"] is True
    p = wa._load_project(pid)
    assert not p.get("published") and "ig_media_id" not in p and "published_at" not in p
    h = _hooks(pid)[0]
    assert h["published"] is False and h["likes"] is None and "media_id" not in h
    assert sandbox["calendar_undo"] == [f"reel#{pid}"] and sandbox["cloud_undo"] == [pid]
    # 떼어낸 게시물 a 는 history 에 남아 다음 동기화가 다시 붙이지 않는다(오매칭을 사람이 고치는 길)
    assert p["published_history"][0]["ig_media_id"] == "a"
    notes = ps.sync_published_reels(client=_FakeApi([_post_a()]), now=T_NEW + 7200)
    assert any("새로 감지된 발행 없음" in n for n in notes)
    assert not wa._load_project(pid).get("published")
    # 다른 게시물은 여전히 붙는다
    other = {**_post_a(), "id": "c", "permalink": "https://www.instagram.com/reel/c/"}
    ps.sync_published_reels(client=_FakeApi([other]), now=T_NEW + 7200)
    assert wa._load_project(pid)["ig_media_id"] == "c"


def test_new_version_keeps_old_hooks_and_calendar_intact(sandbox):
    """v1 발행·성과 → 다시 만들기 → v2 발행: 옛 판 훅·캘린더는 그대로, 새 판만 새로 기록."""
    pid = "1788000000-제철-과일산도-단면"
    _project(pid, CAP)
    ps.sync_published_reels(client=_FakeApi([_post_a()]), now=T_NEW + 3600)     # v1: 게시물 a, ♥12
    h0 = _hooks(pid)[0]
    assert h0["media_id"] == "a" and h0["likes"] == 12

    p = wa._load_project(pid)
    ps.start_new_version(p, now=T_NEW + 2 * 86400)                              # 다시 만들기
    wa._save_project(p)
    planner.record_hook(pid, p["title"], "새 훅", "reel_2.mp4")
    h0, h1 = _hooks(pid)
    assert h0["archived"] is True and not h1.get("archived")

    post_b = {**_post_a(likes=3), "id": "b", "timestamp": "2026-09-06T02:00:00+0000",
              "permalink": "https://www.instagram.com/reel/b/"}
    ps.sync_published_reels(client=_FakeApi([_post_a(likes=40), post_b]), now=T_NEW + 3 * 86400)
    h0, h1 = _hooks(pid)
    assert h0["media_id"] == "a" and h0["likes"] == 12       # 옛 판 그대로(성과도 안 덮임)
    assert h1["media_id"] == "b" and h1["likes"] == 3        # 새 판만 새 게시물
    refs = [c["source_ref"] for c in sandbox["calendar"]]
    assert len(refs) == 2 and refs[0] == f"reel#{pid}" and refs[1].startswith(f"reel#{pid}@")

    ps.unmark_reel_published(pid)                             # v2 취소 — v1 은 건드리지 않는다
    h0, h1 = _hooks(pid)
    assert h0["published"] and h0["likes"] == 12
    assert not h1["published"] and h1["likes"] is None
    assert sandbox["calendar_undo"] == [refs[1]]


def test_new_version_moves_publish_record_to_history():
    p = {"id": "x", "created": T_MADE, "published": True, "published_at": T_NEW,
         "published_source": "auto", "ig_media_id": "a", "ig_permalink": "u"}
    ps.new_version(p, now=T_NEW + 100)
    assert not p.get("published") and "ig_media_id" not in p and "published_at" not in p
    assert p["rendered_at"] == T_NEW + 100
    assert p["published_history"][0]["ig_media_id"] == "a"
    assert ps.anchor_time(p) == T_NEW + 100          # 시간 창 기준도 새 판 기준으로
    # 안 올린 판을 다시 만들면 history 는 생기지 않는다
    q = {"id": "y", "created": T_MADE}
    ps.new_version(q, now=T_NEW)
    assert "published_history" not in q and q["rendered_at"] == T_NEW


# ── 자동 감지 + 성과 갱신 ────────────────────────────────────────

class _FakeApi:
    def __init__(self, posts, insights=None, scopes_missing=("instagram_manage_insights",)):
        self.posts, self.insights = posts, insights or {}
        self.scopes_missing = list(scopes_missing)
        self.insight_calls = []

    def my_media(self, limit=25):
        return list(self.posts)[:limit]

    def missing_optional_scopes(self):
        return list(self.scopes_missing)

    def media_insights(self, media_id):
        self.insight_calls.append(media_id)
        return self.insights.get(media_id, {})


def _post_a(likes=12):
    return {"id": "a", "caption": IG_CAP, "timestamp": IG_NEW, "like_count": likes,
            "comments_count": 3, "permalink": "https://www.instagram.com/reel/a/"}


def test_sync_detects_publish_and_refreshes_likes(sandbox):
    hit = "1788000000-제철-과일산도-단면"
    miss = "1788000100-잠봉뵈르-베이글"
    _project(hit, CAP)
    _project(miss, "잠봉뵈르 베이글, 버터를 이렇게 두껍게 썰어요")
    api = _FakeApi([_post_a(),
                    {"id": "b", "caption": OTHER, "timestamp": IG_NEW, "like_count": 4, "comments_count": 0}])

    notes = ps.sync_published_reels(client=api, now=1788500000)
    assert any("발행 감지" in n for n in notes)

    p = wa._load_project(hit)
    assert p["published"] and p["ig_media_id"] == "a" and p["published_source"] == "auto"
    assert p["published_at"] == T_NEW                 # 게시물 시각을 발행 시각으로
    h = _hooks(hit)[0]
    assert h["published"] and h["likes"] == 12 and h["comments"] == 3 and h["media_id"] == "a"
    assert not wa._load_project(miss).get("published")     # 닮은 게시물이 없으면 그대로
    assert sandbox["calendar"][0]["source_ref"] == f"reel#{hit}"

    # 다음 날 좋아요가 늘었다 — 같은 게시물을 또 '감지'하지 않고 숫자만 갱신
    api.posts[0]["like_count"] = 20
    n_before = len(sandbox["calendar"])
    notes = ps.sync_published_reels(client=api, now=1788600000)
    assert any("새로 감지된 발행 없음" in n for n in notes)
    assert _hooks(hit)[0]["likes"] == 20
    assert len(sandbox["calendar"]) == n_before       # 성과 갱신은 캘린더에 다시 적지 않는다
    assert not api.insight_calls                       # 권한 없으면 인사이트를 부르지 않는다


def test_sync_links_post_to_manually_marked_reel(sandbox):
    """[올렸어요]만 누른 릴스(게시물 ID 없음) — 동기화가 게시물을 찾아 붙이고 좋아요를 채운다.

    검토(2026-09-04)에서 잡힌 결함: 버튼을 누르면 오히려 자동 수집에서 빠졌다.
    """
    pid = "1788000000-제철-과일산도-단면"
    _project(pid, CAP)
    ps.mark_reel_published(pid, at=T_NEW + 3600, source="manual")    # 올린 뒤 1시간 뒤 누름
    assert wa._load_project(pid).get("ig_media_id") is None

    notes = ps.sync_published_reels(client=_FakeApi([_post_a()]), now=T_NEW + 7200)
    assert any("게시물 연결" in n for n in notes)
    p = wa._load_project(pid)
    assert p["ig_media_id"] == "a" and p["ig_permalink"].endswith("/reel/a/")
    assert p["published_at"] == T_NEW + 3600 and p["published_source"] == "manual"   # 누른 기록 유지
    h = _hooks(pid)[0]
    assert h["likes"] == 12 and h["comments"] == 3 and h["media_id"] == "a"
    assert len(sandbox["calendar"]) == 1              # 캘린더 중복 기록 없음(auto_record 의 reel# 마커)


def test_sync_links_by_permalink_first(sandbox):
    """주소를 적어 둔 릴스는 캡션이 달라도 주소로 붙인다."""
    pid = "1788000000-제철-과일산도-단면"
    _project(pid, "완전히 다른 캡션이지만 주소를 적어 뒀다")
    ps.mark_reel_published(pid, at=T_NEW + 60, url="https://instagram.com/reel/a?igsh=1")
    notes = ps.sync_published_reels(client=_FakeApi([_post_a()]), now=T_NEW + 7200)
    assert any("주소 일치" in n for n in notes)
    assert wa._load_project(pid)["ig_media_id"] == "a" and _hooks(pid)[0]["likes"] == 12


def test_sync_does_not_attach_old_post_to_new_version(sandbox):
    """다시 만든 판(new_version)에 옛 판의 게시물이 붙지 않는다."""
    pid = "1788000000-제철-과일산도-단면"
    _project(pid, CAP)
    ps.mark_reel_published(pid, at=T_NEW, media_id="a", source="auto")
    p = wa._load_project(pid)
    ps.new_version(p, now=T_NEW + 86400 * 2)
    wa._save_project(p)
    notes = ps.sync_published_reels(client=_FakeApi([_post_a()]), now=T_NEW + 86400 * 3)
    assert any("새로 감지된 발행 없음" in n for n in notes)
    assert not wa._load_project(pid).get("published")


def test_sync_uses_insights_when_permitted(sandbox):
    pid = "1788000000-제철-과일산도-단면"
    _project(pid, CAP)
    api = _FakeApi([_post_a()], insights={"a": {"reach": 900, "saved": 31, "shares": 7}},
                   scopes_missing=())
    ps.sync_published_reels(client=api, now=1788500000)
    h = _hooks(pid)[0]
    assert (h["reach"], h["saves"], h["shares"], h["likes"]) == (900, 31, 7, 12)
    assert api.insight_calls == ["a"]


def test_sync_skips_stale_posts_for_refresh(sandbox):
    """발행 30일이 지난 릴스는 성과를 다시 묻지 않는다(호출 아낌)."""
    pid = "1780000000-옛-릴스"
    _project(pid, CAP, created=1_780_000_000)
    ps.mark_reel_published(pid, at=1_780_100_000, media_id="old")
    api = _FakeApi([{"id": "old", "caption": IG_CAP, "timestamp": "2026-05-29T00:00:00+0000",
                     "like_count": 99, "comments_count": 1}])
    ps.sync_published_reels(client=api, now=1_788_500_000)
    assert _hooks(pid)[0]["likes"] is None


# ── 잡 큐 (직원 웹 → 집 PC) ────────────────────────────────────

class _Query:
    def __init__(self, rows, log):
        self._rows, self._log = rows, log

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) == val]
        return self

    def in_(self, col, vals):
        self._rows = [r for r in self._rows if r.get(col) in vals]
        return self

    def order(self, col, desc=False):
        self._rows = sorted(self._rows, key=lambda r: r.get(col) or "", reverse=desc)
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def insert(self, row):
        self._log.append(("insert", row))
        self._rows = [{"id": 99, **row}]
        return self

    def execute(self):
        return type("R", (), {"data": list(self._rows)})()


class _Client:
    def __init__(self, rows, log):
        self._rows, self._log = rows, log

    def table(self, name):
        return _Query(list(self._rows), self._log)


def test_request_reel_published_reuses_pending_job(monkeypatch):
    from database import supabase_client as db
    log = []
    rows = [{"id": 5, "kind": "reel_published", "status": "pending",
             "message": json.dumps({"pid": "P1", "url": ""})}]
    monkeypatch.setattr(db, "get_client", lambda: _Client(rows, log))

    assert db.request_reel_published("P1")["id"] == 5 and not log     # 연타 → 재사용
    new = db.request_reel_published("P2", url="https://www.instagram.com/reel/z/")
    assert new["id"] == 99 and log[0][1]["kind"] == "reel_published"
    assert json.loads(log[0][1]["message"]) == {"pid": "P2", "url": "https://www.instagram.com/reel/z/"}
    # 같은 릴스라도 '되돌리기'는 다른 방향이라 새 잡
    undo = db.request_reel_published("P1", undo=True)
    assert undo["id"] == 99 and json.loads(log[1][1]["message"])["undo"] is True
    assert db.pending_reel_published() == {"P1": "pending"}


def test_pending_state_uses_latest_request(monkeypatch):
    """기록·취소 요청이 같은 릴스에 둘 다 살아 있으면 마지막 요청이 카드 상태다."""
    from database import supabase_client as db
    rows = [
        {"id": 2, "kind": "reel_published", "status": "pending", "requested_at": "2026-09-04T02:00",
         "message": json.dumps({"pid": "P1", "url": "", "undo": True})},
        {"id": 1, "kind": "reel_published", "status": "pending", "requested_at": "2026-09-04T01:00",
         "message": json.dumps({"pid": "P1", "url": ""})},
    ]
    monkeypatch.setattr(db, "get_client", lambda: _Client(rows, []))
    assert db.pending_reel_published() == {"P1": "undo"}


def test_publish_job_is_interactive_and_routed():
    from database import supabase_client as db
    assert "reel_published" in db.INTERACTIVE_JOB_KINDS   # 직원이 기다리는 잡 — 빠른 박자
    src = (ROOT / "worker" / "agent.py").read_text(encoding="utf-8")
    assert 'job.get("kind") == "reel_published"' in src   # run_job 분기(알 수 없는 잡 가드보다 앞)
    assert "maybe_reel_sync()" in src                      # 느린 박자 정기 점검에 등록
