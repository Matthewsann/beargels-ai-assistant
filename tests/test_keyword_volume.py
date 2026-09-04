"""네이버 검색광고 키워드도구 클라이언트 (네트워크·키 불필요).

왜 이 테스트가 있나(2026-09-05):
    검색량을 몰라서 판정이 거꾸로 나가고 있었다. 실측: 「송도베이글산도」 월 45회를
    '지금 쓰면 이긴다'로 1순위 추천하고, 월 1,030회인 「송도베이글」은 '피하라'고
    했다. 이 클라이언트가 그 숫자를 가져온다.

계약:
  · 서명은 "타임스탬프.메서드.**경로**" — 쿼리스트링을 넣으면 403 이 난다
  · 응답 필드는 전부 문자열이고, 10 미만은 '< 10' 으로 마스킹돼 온다
  · 키워드는 공백을 뺀 형태로 보낸다 (키워드도구 규칙)
  · 키가 없으면 조용히 꺼진다 — 검색량 없이 예전처럼 동작해야 한다
"""

import base64
import hashlib
import hmac
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sns_automation import keyword_volume as kv  # noqa: E402


def test_signature_matches_official_sample():
    """공식 파이썬 샘플과 같은 값이 나와야 한다 — 여기서 틀리면 전부 403."""
    secret, ts, method, uri = "mysecret", "1457082455307", "GET", "/ncc/campaigns"
    expected = base64.b64encode(hmac.new(
        secret.encode(), f"{ts}.{method}.{uri}".encode(), hashlib.sha256).digest()).decode()
    assert kv.sign(secret, ts, method, uri) == expected
    # 경로만 서명한다 — 쿼리를 붙이면 다른 값이 나온다(=403 의 원인)
    assert kv.sign(secret, ts, method, uri) != kv.sign(secret, ts, method, uri + "?a=1")


def test_parse_count_handles_masking_and_commas():
    assert kv.parse_count("1,230") == 1230
    assert kv.parse_count("< 10") == kv.UNDER10      # 0 으로 치면 '수요 없음'과 구분 안 됨
    assert kv.parse_count("<10") == kv.UNDER10
    assert kv.parse_count(240) == 240
    assert kv.parse_count(None) == 0 and kv.parse_count("") == 0
    assert kv.parse_count("데이터없음") == 0


def test_clean_keyword_strips_spaces():
    assert kv.clean_keyword("송도 베이글 산도") == "송도베이글산도"
    assert kv.clean_keyword("  ") == ""


def test_to_rows_sums_pc_and_mobile_and_sorts():
    payload = {"keywordList": [
        {"relKeyword": "송도베이글", "monthlyPcQcCnt": "60",
         "monthlyMobileQcCnt": "970", "compIdx": "중간"},
        {"relKeyword": "베이글산도", "monthlyPcQcCnt": "240",
         "monthlyMobileQcCnt": "2,280", "compIdx": "높음"},
        {"relKeyword": "송도베이글산도", "monthlyPcQcCnt": "< 10",
         "monthlyMobileQcCnt": "40", "compIdx": "낮음"},
    ]}
    rows = kv.to_rows(payload)
    assert [r["keyword"] for r in rows] == ["베이글산도", "송도베이글", "송도베이글산도"]
    assert rows[0]["total"] == 2520 and rows[1]["total"] == 1030
    assert rows[2]["total"] == kv.UNDER10 + 40       # 마스킹된 값도 더해진다
    assert kv.to_rows({}) == []


def test_lookup_batches_by_five_and_filters(monkeypatch):
    """한 번에 5개까지 — 8개를 넣으면 두 번 부른다. related=False 면 물어본 것만."""
    calls = []

    def fake_call(params):
        calls.append(params["hintKeywords"].split(","))
        return {"keyword_list_placeholder": True, "keywordList": [
            {"relKeyword": k, "monthlyPcQcCnt": "10", "monthlyMobileQcCnt": "10"}
            for k in params["hintKeywords"].split(",")
        ] + [{"relKeyword": "덤으로온연관어", "monthlyPcQcCnt": "999",
              "monthlyMobileQcCnt": "0"}]}

    monkeypatch.setattr(kv, "_call", fake_call)
    monkeypatch.setattr(kv, "PAUSE", 0)
    got = kv.lookup([f"키워드{i}" for i in range(8)])
    assert len(calls) == 2 and len(calls[0]) == 5 and len(calls[1]) == 3
    assert "덤으로온연관어" not in [r["keyword"] for r in got]   # 안 물어본 건 뺀다
    assert len(got) == 8
    # related=True 면 연관어까지 준다
    assert "덤으로온연관어" in [r["keyword"] for r in kv.lookup(["키워드0"], related=True)]


def test_volume_map_keys_have_no_spaces(monkeypatch):
    monkeypatch.setattr(kv, "_call", lambda p: {"keywordList": [
        {"relKeyword": "송도베이글", "monthlyPcQcCnt": "60", "monthlyMobileQcCnt": "970"}]})
    monkeypatch.setattr(kv, "PAUSE", 0)
    assert kv.volume_map(["송도 베이글"]) == {"송도베이글": 1030}


def test_configured_is_false_without_keys(monkeypatch):
    for k in ("NAVER_AD_API_KEY", "NAVER_AD_SECRET_KEY", "NAVER_AD_CUSTOMER_ID"):
        monkeypatch.delenv(k, raising=False)
    assert kv.configured() is False
    monkeypatch.setenv("NAVER_AD_API_KEY", "a")
    monkeypatch.setenv("NAVER_AD_SECRET_KEY", "b")
    assert kv.configured() is False          # 셋 다 있어야 한다
    monkeypatch.setenv("NAVER_AD_CUSTOMER_ID", "123")
    assert kv.configured() is True


def test_research_survives_volume_failure(monkeypatch):
    """검색량 조회가 실패해도 경쟁 조사 결과는 살아 있어야 한다."""
    from sns_automation import naver_search as ns
    monkeypatch.setattr(ns, "PAUSE", 0)
    monkeypatch.setattr(ns, "autocomplete", lambda k: [k])
    monkeypatch.setattr(ns, "blog_top", lambda k, limit=30: [
        {"blog_id": f"b{i}", "log_no": "1", "title": "다른 글"} for i in range(30)])
    monkeypatch.setattr(kv, "configured", lambda: True)

    def boom(keywords):
        raise kv.VolumeError("429")
    monkeypatch.setattr(kv, "volume_map", boom)
    monkeypatch.setattr(ns, "save", lambda d, path=None: None)
    data = ns.research(seeds=["송도 베이글"], per_seed=0, max_keywords=1)
    assert len(data["rows"]) == 1 and data["has_volume"] is False
    assert data["rows"][0]["verdict"]["tier"]        # 판정은 그대로 나온다


def test_research_studies_every_seed_before_branches(monkeypatch):
    """씨앗이 상한에 밀려 조사조차 안 되던 문제(2026-09-05)."""
    from sns_automation import naver_search as ns
    monkeypatch.setattr(ns, "PAUSE", 0)
    monkeypatch.setattr(ns, "autocomplete",
                        lambda k: [k, k + " 송도 추천", k + " 송도 후기"])
    monkeypatch.setattr(ns, "blog_top", lambda k, limit=30: [])
    monkeypatch.setattr(kv, "configured", lambda: False)
    monkeypatch.setattr(ns, "save", lambda d, path=None: None)
    seeds = ["씨앗하나", "씨앗둘", "씨앗셋", "씨앗넷"]
    data = ns.research(seeds=seeds, per_seed=3, max_keywords=5)
    studied = [r["keyword"] for r in data["rows"]]
    for s in seeds:
        assert s in studied, f"{s} 가 조사되지 않았다"
