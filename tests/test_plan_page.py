"""콘텐츠 기획 화면(/mkt) 조립 규칙 — DB·브라우저 불필요."""
from service import plan_page as pp


def _c(i, status, **kw):
    return {"id": f"b17890000{i:02d}-주제{i}", "topic": f"주제{i}", "status": status, **kw}


def test_three_zones_and_dismissed_hidden():
    cards = [_c(1, "제안"), _c(2, "제안", dismissed=True), _c(3, "촬영중", folder="폴더3"),
             _c(4, "소재도착", keyword="송도 베이글"), _c(5, "발행", likes=31, rank=6),
             _c(6, "종료", dismissed=True)]
    v = pp.build_view(cards)
    assert [c["topic"] for c in v["props"]] == ["주제1"] and v["n_props"] == 1
    assert [c["topic"] for c in v["live"]] == ["주제4", "주제3"]      # 손이 필요한 소재도착 먼저
    assert [c["topic"] for c in v["done"]] == ["주제5"]               # 접은 건 끝난 것에도 안 나온다
    assert "주제4" in v["now"]["text"] and "릴스 만들기" in v["now"]["text"]


def test_next_actions_follow_status():
    v = pp.build_view([_c(1, "촬영중"), _c(2, "소재도착", keyword="k"),
                       _c(3, "제작중", keyword="k", post_id=41), _c(4, "제작중", keyword="k")])
    acts = {c["topic"]: [a for a, _, _ in c["acts"]] for c in v["live"]}
    assert acts == {"주제1": ["intake"], "주제2": ["reel", "blog"], "주제3": [], "주제4": ["blog"]}


def test_proposals_fold_after_three_newest_first():
    v = pp.build_view([_c(i, "제안") for i in range(1, 6)])
    assert [c["topic"] for c in v["props"]] == ["주제5", "주제4", "주제3"]
    assert len(v["props_more"]) == 2 and v["n_props"] == 5


def test_empty_and_busy():
    v = pp.build_view([], {"kind": "reel_ideas", "status": "running"})
    assert "새 제안 받기" in v["now"]["text"] and "짜는 중" in v["busy"]
    v = pp.build_view([], {"kind": "reel_ideas", "status": "error", "message": "AI 실패"})
    assert not v["busy"] and "AI 실패" in v["last"]
    assert not pp.build_view([], {"kind": "post", "status": "running"})["busy"]   # 남의 잡엔 반응 안 함
