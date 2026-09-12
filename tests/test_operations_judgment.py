"""Phase 3-C-1 — 오늘의 운영 판단 엔진 (2026-09-12).

계약:
  · 규칙은 결정적이다(AI 없음). 같은 입력이면 같은 판단·같은 정체성(dedupe_key).
  · 업무 순위는 work_store.priority_of 를 그대로 쓴다 — rank 0 → urgent, 1 → today,
    5(담당 없음) → check, 4(오래됨) → watch. 2·3·6 은 판단에 안 올린다.
  · 기한 지난 업무의 열쇠는 알림과 같은 work.overdue:w:<id> — 알림을 새로 만들지 않는다.
  · 고객 요청은 3일 넘게 미전파일 때만 check(request.unshared:review:<id>).
  · 같은 원천은 한 칸에만. 끝난 업무·공유된 요청은 안 나온다.
  · 믿을 만한 지켜보기 신호가 없으면 watch 는 빈 목록.
DB·네트워크 불필요.
"""
import importlib
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tests", ROOT / "service"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from database import work_store as wk  # noqa: E402
from service import operations_judgment as oj  # noqa: E402

REF = date(2026, 9, 12)


def _task(raw_id, content, due=None, owner="", created=None, done=False, source="work"):
    """work_store.open_tasks 가 주는 모양 그대로 — 순위는 진짜 priority_of 로 계산."""
    r = {"id": raw_id, "content": content, "owner": owner, "done": done,
         "created_at": (created or REF - timedelta(days=1)).isoformat()}
    if due:
        r["due_date"] = due.isoformat()
    v = wk._view(r, source, REF)
    return v


def _req(rid, days_ago, topic="포장"):
    d = (REF - timedelta(days=days_ago)).isoformat()
    return {"id": rid, "topic": topic, "quote": "빵칼 하나만 넣어주세요", "date": d,
            "collected": d, "platform": "배민"}


def _by_cat(js, cat):
    return [j for j in js if j["category"] == cat]


# ── 1·3·4: 기한 지남 → urgent, 끝난 것 제외, today 에 중복 없음 ────────────────

def test_기한_지난_업무는_urgent_이고_알림_열쇠를_그대로_쓴다():
    js = oj.get_daily_judgments(tasks=[_task(39, "냉장고 수리", due=REF - timedelta(days=2), owner="민수")],
                                today=REF)
    assert len(js) == 1
    j = js[0]
    assert j["category"] == "urgent" and j["priority"] == 1
    assert j["dedupe_key"] == "work.overdue:w:39"          # 3-B-2 알림과 같은 정체성
    assert j["has_notification"] is True
    assert j["link"] == "/work#w-39"
    assert "2일 지났어요" in j["reason"] and "민수" in j["reason"]


def test_끝난_업무는_판단에_안_나온다():
    js = oj.get_daily_judgments(
        tasks=[_task(1, "끝낸 것", due=REF - timedelta(days=5), done=True),
               _task(2, "끝낸 오늘 것", due=REF, done=True)], today=REF)
    assert js == []


def test_기한_지난_업무가_today_에_또_들어가지_않는다():
    js = oj.get_daily_judgments(
        tasks=[_task(5, "늦은 것", due=REF - timedelta(days=1)),
               _task(6, "오늘 것", due=REF)], today=REF)
    assert [j["source_id"] for j in _by_cat(js, "urgent")] == ["w:5"]
    assert [j["source_id"] for j in _by_cat(js, "today")] == ["w:6"]
    assert len({(j["source_type"], j["source_id"]) for j in js}) == len(js)


# ── 2: 오늘 기한 → today (내일·이번 주·여유는 안 올린다) ───────────────────────

def test_오늘_기한은_today_내일부터는_안_올린다():
    js = oj.get_daily_judgments(
        tasks=[_task(7, "오늘", due=REF, owner="지은"),
               _task(8, "내일", due=REF + timedelta(days=1), owner="지은"),
               _task(9, "다음 주", due=REF + timedelta(days=6), owner="지은")], today=REF)
    assert [(j["category"], j["source_id"]) for j in js] == [("today", "w:7")]
    assert js[0]["dedupe_key"] == "work.today:w:7" and js[0]["has_notification"] is False


# ── 5·6: 고객 요청 ───────────────────────────────────────────────────────────

def test_3일_넘게_미전파_요청은_check():
    js = oj.get_daily_judgments(tasks=[], requests=[_req(1734, 4), _req(1800, 1)], today=REF)
    assert [(j["category"], j["source_id"]) for j in js] == [("check", 1734)]
    j = js[0]
    assert j["dedupe_key"] == "request.unshared:review:1734"     # Phase 2 알림과 같은 열쇠
    assert j["has_notification"] is True
    assert j["link"] == "/review#requests"
    assert "4일째" in j["reason"]


def test_공유_끝난_요청은_입력에_없으니_안_나온다():
    # 공유 완료는 _customer_requests 가 이미 걸러 준다 — 엔진은 받은 것만 본다.
    # 넘겨받은 목록이 비면 check 도 빈다(엔진이 몰래 DB 를 읽지 않는다).
    js = oj.get_daily_judgments(tasks=[], requests=[], today=REF)
    assert js == []


# ── 7: watch 는 보수적으로 ──────────────────────────────────────────────────

def test_믿을_신호가_없으면_watch_는_빈다():
    js = oj.get_daily_judgments(
        tasks=[_task(1, "오늘", due=REF), _task(2, "여유", due=REF + timedelta(days=20), owner="a")],
        requests=[], review_counts={}, today=REF)
    assert _by_cat(js, "watch") == []


def test_기한_없이_오래_묵은_업무만_watch():
    js = oj.get_daily_judgments(
        tasks=[_task(3, "묵은 것", owner="a", created=REF - timedelta(days=10))], today=REF)
    assert [(j["category"], j["dedupe_key"]) for j in js] == [("watch", "work.stale:w:3")]


# ── 8·10: 순서·정체성이 결정적 ────────────────────────────────────────────────

def test_순서가_결정적이다():
    tasks = [_task(1, "하루 늦음", due=REF - timedelta(days=1)),
             _task(2, "닷새 늦음", due=REF - timedelta(days=5)),
             _task(3, "오늘", due=REF, owner="a"),
             _task(4, "담당 없음"),
             _task(5, "회의 할 일 늦음", due=REF - timedelta(days=3), source="meeting")]
    reqs = [_req(10, 3), _req(11, 6)]
    counts = {"escalate": 1, "attention": 2}
    a = oj.get_daily_judgments(tasks=tasks, requests=reqs, review_counts=counts, today=REF)
    b = oj.get_daily_judgments(tasks=list(reversed(tasks)), requests=list(reversed(reqs)),
                               review_counts=counts, today=REF)
    keys = [j["dedupe_key"] for j in a]
    assert keys == [
        "work.overdue:w:2", "work.overdue:m:5", "work.overdue:w:1",   # 늦은 날수 많은 순
        "work.today:w:3",
        "review.escalate", "request.unshared:review:11", "request.unshared:review:10",
        "review.attention", "work.unassigned:w:4",
    ]
    assert [j["priority"] for j in a] == sorted(j["priority"] for j in a)
    assert keys == [j["dedupe_key"] for j in b]           # 입력 순서를 바꿔도 같다


def test_두_번_불러도_같은_정체성():
    tasks = [_task(39, "냉장고", due=REF - timedelta(days=2))]
    a = oj.get_daily_judgments(tasks=tasks, requests=[_req(7, 5)], today=REF)
    b = oj.get_daily_judgments(tasks=tasks, requests=[_req(7, 5)], today=REF)
    assert a == b


# ── 9: 행동 링크 ────────────────────────────────────────────────────────────

def test_모든_판단이_처리_화면으로_바로_간다():
    js = oj.get_daily_judgments(
        tasks=[_task(1, "늦음", due=REF - timedelta(days=1)), _task(2, "담당 없음"),
               _task(3, "회의", due=REF, source="meeting")],
        requests=[_req(5, 4)], review_counts={"escalate": 1, "attention": 1}, today=REF)
    links = {j["dedupe_key"]: j["link"] for j in js}
    assert links["work.overdue:w:1"] == "/work#w-1"
    assert links["work.unassigned:w:2"] == "/work#w-2"
    assert links["work.today:m:3"] == "/work#m-3"
    assert links["request.unshared:review:5"] == "/review#requests"
    assert links["review.escalate"] == "/todo"
    assert links["review.attention"] == "/care"
    for j in js:
        assert j["link"].startswith("/") and j["link"] != "/"    # 홈으로 되돌리지 않는다


# ── 11·12: 기존 알림 dedupe·해소가 그대로 ───────────────────────────────────

def test_엔진은_알림을_만들지도_닫지도_않는다(monkeypatch):
    from database import notification_store as ns
    for name in ("record", "resolve_by_key", "mark_resolved", "mark_read"):
        monkeypatch.setattr(ns, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError(name)))
    js = oj.get_daily_judgments(
        tasks=[_task(39, "냉장고", due=REF - timedelta(days=2))], requests=[_req(1, 4)],
        review_counts={"escalate": 1}, today=REF)
    assert len(js) == 3


def test_urgent_열쇠는_set_done_이_닫는_열쇠와_같다(monkeypatch):
    """업무를 끝내면 work_store 가 work.overdue:w:<id> 를 닫는다 — 판단의 열쇠와 일치해야
    알림 하나·판단 하나가 같은 문제를 가리킨다."""
    from database import notification_store as ns
    closed = []
    monkeypatch.setattr(ns, "resolve_by_key", lambda k, **kw: closed.append(k))
    wk._resolve_overdue_notice(39)
    j = oj.get_daily_judgments(tasks=[_task(39, "냉장고", due=REF - timedelta(days=2))], today=REF)[0]
    assert closed == [j["dedupe_key"]] == ["work.overdue:w:39"]


# ── 화면: 홈 '오늘의 운영 판단' ───────────────────────────────────────────────

def test_group_은_빈_칸을_빼고_넘치면_더보기():
    js = oj.get_daily_judgments(
        tasks=[_task(i, f"늦음 {i}", due=REF - timedelta(days=i)) for i in range(1, 7)],
        today=REF)
    gs = oj.group(js, per=4)
    assert [g["category"] for g in gs] == ["urgent"]
    assert len(gs[0]["items"]) == 4 and gs[0]["more"] == 2 and gs[0]["more_link"] == "/work"


KEY = "testkey"


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setenv("SERVICE_PATH", KEY)
    import service.app as m
    importlib.reload(m)
    m.app.testing = True
    monkeypatch.setattr(m, "gather", _gather_stub)
    monkeypatch.setattr(m, "_owner_alerts", lambda: [])
    monkeypatch.setattr(m, "_notif_alerts", lambda: [])
    for name in ("meet_tasks", "_work_top_cached", "_briefs_cached"):
        if hasattr(m, name):
            monkeypatch.setattr(m, name, lambda *a, **k: [])
    return m


def _gather_stub(**calls):
    out = {k: None for k in calls}
    for k in ("inbox", "meet_tasks", "work_top", "briefs", "judgments"):
        if k in calls:
            out[k] = calls[k]()
    return out


def test_홈에_오늘의_운영_판단이_칸별로_뜬다(svc, monkeypatch):
    m = svc
    monkeypatch.setattr(m, "_judgments_cached", lambda: oj.get_daily_judgments(
        tasks=[_task(39, "냉장고 수리 부르기", due=REF - timedelta(days=2), owner="민수"),
               _task(40, "포스 영수증지 주문", due=REF)],
        requests=[_req(1734, 4)], review_counts={"escalate": 0, "attention": 0}, today=REF))
    html = m.app.test_client().get(f"/{KEY}/").get_data(as_text=True)
    assert 'class="sec-lbl">오늘의 운영 판단' in html
    assert "가장 먼저 처리" in html and "오늘 처리" in html and "확인 필요" in html
    assert "⚪ 지켜보기" not in html                                # 빈 칸은 안 그린다
    assert f'href="/{KEY}/work#w-39"' in html and "냉장고 수리 부르기" in html
    assert f'href="/{KEY}/review#requests"' in html
    assert 'data-key="work.overdue:w:39"' in html
    assert html.index("가장 먼저 처리") < html.index("오늘 처리") < html.index("확인 필요")


def test_판단이_없으면_칸_자체가_없다(svc, monkeypatch):
    m = svc
    monkeypatch.setattr(m, "_judgments_cached", lambda: [])
    html = m.app.test_client().get(f"/{KEY}/").get_data(as_text=True)
    assert 'class="sec-lbl">오늘의 운영 판단' not in html and 'class="judge"' not in html


def test_판단_계산이_깨져도_홈은_뜬다(svc, monkeypatch):
    m = svc
    monkeypatch.setattr(m, "_judgments_cached", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    r = m.app.test_client().get(f"/{KEY}/")
    assert r.status_code == 200 and 'class="judge"' not in r.get_data(as_text=True)
