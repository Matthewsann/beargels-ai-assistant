"""Meta Graph API 클라이언트 — 인스타그램 해시태그 리서치 + 내 계정 인사이트.

Supermetrics 무료 체험이 만료돼(2026-08-07) 대체로 붙인 직결 연동이다.
비용이 들지 않고, 이미 연결해 둔 **페이스북 페이지 + 인스타 비즈니스 계정**만
있으면 된다.

.env 에 필요한 값:
    META_ACCESS_TOKEN   페이스북 그래프 API 액세스 토큰 (장기 토큰 권장)
    IG_USER_ID          인스타 비즈니스 계정 ID (비워두면 토큰에서 자동으로 찾음)

토큰 발급 절차는 docs/meta_graph_setup.md 참고.

⚠️ 해시태그 검색 한도: **7일에 고유 해시태그 30개**까지. 그래서 조회한
해시태그 ID를 data/hashtag_ids.json 에 캐시해 재사용한다(ID 조회도 한도를 먹는다).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

API = "https://graph.facebook.com/v21.0"
_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_HASHTAG_CACHE = os.path.join(_CACHE_DIR, "hashtag_ids.json")


class MetaGraphError(RuntimeError):
    """그래프 API 호출 실패. 사용자에게 그대로 보여줄 수 있는 한글 메시지를 담는다."""


@dataclass
class MetaGraph:
    access_token: str
    ig_user_id: str = ""
    _hashtag_ids: dict = field(default_factory=dict, repr=False)

    # ── 기본 호출 ────────────────────────────────────────────
    def _get(self, path: str, **params) -> dict:
        params["access_token"] = self.access_token
        try:
            r = httpx.get(f"{API}/{path.lstrip('/')}", params=params, timeout=30)
        except httpx.HTTPError as e:
            raise MetaGraphError(f"메타 API 연결 실패: {e}") from e

        if r.status_code >= 400:
            try:
                err = r.json().get("error", {})
            except ValueError:
                err = {}
            msg = err.get("message", r.text[:300])
            code = err.get("code")
            if code == 190:
                raise MetaGraphError(
                    "액세스 토큰이 만료되었거나 유효하지 않습니다. "
                    "docs/meta_graph_setup.md 를 보고 새로 발급받으세요."
                )
            if code == 4 or "limit" in str(msg).lower():
                raise MetaGraphError(
                    f"메타 API 호출 한도에 걸렸습니다(해시태그는 7일에 30개). 원문: {msg}"
                )
            raise MetaGraphError(f"메타 API 오류({code}): {msg}")
        return r.json()

    # ── 계정 ────────────────────────────────────────────────
    def resolve_ig_user_id(self) -> str:
        """토큰이 접근 가능한 페이지에서 인스타 비즈니스 계정 ID를 찾는다."""
        if self.ig_user_id:
            return self.ig_user_id
        pages = self._get("me/accounts", fields="id,name").get("data", [])
        if not pages:
            raise MetaGraphError(
                "토큰으로 접근 가능한 페이스북 페이지가 없습니다. "
                "토큰 발급 시 페이지 권한(pages_show_list)을 체크했는지 확인하세요."
            )
        for p in pages:
            # 페이지 유형에 따라 필드 이름이 다르다. 둘 다 본다.
            for fld in ("instagram_business_account", "connected_instagram_account"):
                info = self._get(p["id"], fields=f"{fld}{{id,username}}")
                iba = info.get(fld)
                if iba and iba.get("id"):
                    self.ig_user_id = iba["id"]
                    logger.info("인스타 계정 발견: @%s (%s), 페이지 '%s'",
                                iba.get("username"), self.ig_user_id, p.get("name"))
                    return self.ig_user_id
        raise self._account_error(", ".join(p.get("name", "?") for p in pages))

    def _account_error(self, names: str) -> MetaGraphError:
        """계정을 못 찾았을 때, 진짜 원인을 짚어 준다.

        가장 흔한 원인은 계정 연결이 아니라 **권한 누락**이다. `instagram_basic`
        이 없으면 연결이 멀쩡해도 instagram_business_account 필드가 조용히
        빈 값으로 온다(에러도 안 난다).
        """
        missing = self.missing_scopes()
        if missing:
            return MetaGraphError(
                f"토큰에 권한이 빠져 있습니다: {', '.join(missing)}\n"
                f"    페이지({names})는 찾았지만 인스타 정보를 읽을 권한이 없습니다.\n"
                "    그래프 API 탐색기에서 위 권한을 체크하고 토큰을 다시 만드세요."
            )
        return MetaGraphError(
            f"페이지({names})에 연결된 인스타 비즈니스 계정을 찾지 못했습니다. "
            "인스타 앱 → 설정 → 계정 유형에서 '프로페셔널 계정'인지, "
            "페이스북 페이지와 연결돼 있는지 확인하세요."
        )

    #: 이 연동에 반드시 필요한 권한. 하나라도 빠지면 조용히 빈 값이 돌아온다.
    NEEDED_SCOPES = (
        "pages_show_list",
        "pages_read_engagement",
        "instagram_basic",
        "instagram_manage_insights",
    )

    def granted_scopes(self) -> list[str]:
        data = self._get("me/permissions").get("data", [])
        return [p["permission"] for p in data if p.get("status") == "granted"]

    def missing_scopes(self) -> list[str]:
        granted = set(self.granted_scopes())
        return [s for s in self.NEEDED_SCOPES if s not in granted]

    def token_info(self) -> dict:
        """토큰 유효기간 등. 실패해도 치명적이지 않다."""
        try:
            return self._get("debug_token", input_token=self.access_token).get("data", {})
        except MetaGraphError:
            return {}

    def me(self) -> dict:
        """내 인스타 계정 기본 정보."""
        uid = self.resolve_ig_user_id()
        return self._get(uid, fields="username,name,followers_count,follows_count,media_count")

    # ── 해시태그 리서치 ──────────────────────────────────────
    def _load_cache(self) -> dict:
        if self._hashtag_ids:
            return self._hashtag_ids
        try:
            with open(_HASHTAG_CACHE, encoding="utf-8") as f:
                self._hashtag_ids = json.load(f)
        except (OSError, ValueError):
            self._hashtag_ids = {}
        return self._hashtag_ids

    def _save_cache(self) -> None:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(_HASHTAG_CACHE, "w", encoding="utf-8") as f:
            json.dump(self._hashtag_ids, f, ensure_ascii=False, indent=2)

    def hashtag_id(self, name: str) -> str:
        """해시태그 이름 → ID. 7일 30개 한도가 있어 캐시를 우선 쓴다."""
        name = name.lstrip("#").strip()
        cache = self._load_cache()
        if name in cache:
            return cache[name]
        uid = self.resolve_ig_user_id()
        data = self._get("ig_hashtag_search", user_id=uid, q=name).get("data", [])
        if not data:
            raise MetaGraphError(f"해시태그 '#{name}' 를 찾지 못했습니다.")
        cache[name] = data[0]["id"]
        self._save_cache()
        return cache[name]

    MEDIA_FIELDS = "id,caption,media_type,media_url,permalink,like_count,comments_count,timestamp"

    def hashtag_media(self, name: str, *, top: bool = True, limit: int = 25) -> list[dict]:
        """해시태그의 인기(top_media) 또는 최신(recent_media) 게시물.

        ⚠️ 캡션·좋아요·댓글까지 주지만 **조회수·저장수는 남의 게시물이라 안 준다**
        (메타 정책). 그래서 인기 판정은 좋아요/댓글로만 한다.
        """
        hid = self.hashtag_id(name)
        edge = "top_media" if top else "recent_media"
        uid = self.resolve_ig_user_id()
        out = self._get(f"{hid}/{edge}", user_id=uid,
                        fields=self.MEDIA_FIELDS, limit=limit)
        return out.get("data", [])

    # ── 내 계정 성과 ─────────────────────────────────────────
    def my_media(self, limit: int = 25) -> list[dict]:
        uid = self.resolve_ig_user_id()
        return self._get(f"{uid}/media", fields=self.MEDIA_FIELDS, limit=limit).get("data", [])

    #: 릴스에서 의미 있는 지표. 도달·저장·공유가 알고리즘의 핵심.
    REEL_METRICS = ("reach", "saved", "shares", "comments", "likes", "total_interactions")

    def media_insights(self, media_id: str, metrics=REEL_METRICS) -> dict:
        """게시물별 인사이트. 지원 안 되는 지표는 빼고 재시도한다."""
        try:
            data = self._get(f"{media_id}/insights", metric=",".join(metrics)).get("data", [])
        except MetaGraphError as e:
            if "metric" not in str(e).lower():
                raise
            # 지표 구성은 게시물 종류(릴스/사진/캐러셀)마다 다르다 → 하나씩 시도
            data = []
            for m in metrics:
                try:
                    data += self._get(f"{media_id}/insights", metric=m).get("data", [])
                except MetaGraphError:
                    continue
        return {d["name"]: (d.get("values") or [{}])[0].get("value") for d in data}


def from_env() -> MetaGraph:
    """.env 에서 설정을 읽어 클라이언트를 만든다."""
    token = os.getenv("META_ACCESS_TOKEN", "").strip()
    if not token:
        raise MetaGraphError(
            "META_ACCESS_TOKEN 이 .env 에 없습니다. docs/meta_graph_setup.md 를 보고 발급하세요."
        )
    return MetaGraph(access_token=token, ig_user_id=os.getenv("IG_USER_ID", "").strip())


def _rate_pause(i: int) -> None:
    """연속 호출 사이 짧은 쉼 (한도·스로틀 회피)."""
    if i:
        time.sleep(0.6)
