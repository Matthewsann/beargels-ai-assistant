"""네이버 검색광고 API 공통 클라이언트 — 서명·헤더·재시도를 한 곳에서.

인증 규칙(공식 python-sample/examples/signaturehelper.py, 2026-09-20 대조):
    서명 원문  = "{timestamp}.{METHOD}.{path}"   (path 는 쿼리스트링 제외)
    서명       = base64( HMAC-SHA256(비밀키, 원문) )
    timestamp  = 밀리초 epoch 문자열
    헤더       = X-Timestamp / X-API-KEY / X-Customer / X-Signature /
                 Content-Type: application/json; charset=UTF-8

키는 리포 루트 .env 에서만 읽는다. 어떤 경로로도 키 값을 출력하지 않는다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

BASE_URL = "https://api.searchad.naver.com"
TIMEOUT = 20.0
MAX_RETRY_429 = 3          # SPEC 1: 429 는 지수 백오프, 최대 3회
_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
_ENV_KEYS = ("NAVER_AD_API_KEY", "NAVER_AD_SECRET_KEY", "NAVER_AD_CUSTOMER_ID")


class NaverAdsError(RuntimeError):
    """사람이 읽고 조치할 수 있는 실패. 키 값은 절대 담지 않는다."""

    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


def sign(secret: str, timestamp: str, method: str, path: str) -> str:
    """X-Signature. 순수 함수라 테스트 가능. path 에 쿼리스트링을 넣으면 403."""
    message = f"{timestamp}.{method}.{path}"
    digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def load_credentials() -> dict[str, str]:
    """.env → 키 3개. 하나라도 비면 어떤 게 비었는지 이름만 말한다."""
    load_dotenv(_ENV_PATH, override=False)
    creds = {k: os.getenv(k, "").strip() for k in _ENV_KEYS}
    missing = [k for k, v in creds.items() if not v]
    if missing:
        raise NaverAdsError(f".env 에 없는 값: {', '.join(missing)} ({_ENV_PATH})")
    return creds


class NaverAdsClient:
    def __init__(self, creds: dict[str, str] | None = None):
        c = creds or load_credentials()
        self._api_key = c["NAVER_AD_API_KEY"]
        self._secret = c["NAVER_AD_SECRET_KEY"]
        self._customer = c["NAVER_AD_CUSTOMER_ID"]
        self._http = httpx.Client(base_url=BASE_URL, timeout=TIMEOUT)

    def _headers(self, method: str, path: str) -> tuple[dict[str, str], str]:
        ts = str(int(time.time() * 1000))
        return {
            "X-Timestamp": ts,
            "X-API-KEY": self._api_key,
            "X-Customer": self._customer,
            "X-Signature": sign(self._secret, ts, method, path),
            "Content-Type": "application/json; charset=UTF-8",
        }, ts

    def request(self, method: str, path: str, *, params: dict | None = None,
                body: Any = None) -> Any:
        """JSON 응답을 돌려준다. path 는 '/ncc/campaigns' 처럼 경로만."""
        if "?" in path:
            raise NaverAdsError("path 에 쿼리스트링을 넣지 말고 params= 로 넘긴다 (서명이 깨진다)")
        method = method.upper()
        delay = 1.0
        for attempt in range(MAX_RETRY_429 + 1):
            headers, ts = self._headers(method, path)
            resp = self._http.request(method, path, params=params, headers=headers,
                                      content=json.dumps(body) if body is not None else None)
            if resp.status_code == 429 and attempt < MAX_RETRY_429:
                logger.warning("429 rate limit — %.0f초 뒤 재시도 (%d/%d)", delay, attempt + 1, MAX_RETRY_429)
                time.sleep(delay)
                delay *= 2
                continue
            break

        if resp.status_code in (401, 403):
            # 서명 원문 구성 요소만 남긴다(키 값 없음). 로컬 시계 오차를 제일 먼저 의심.
            raise NaverAdsError(
                f"인증 실패 {resp.status_code}. 제일 먼저 로컬 시계 오차(X-Timestamp) 를 의심할 것. "
                f"서명 원문 = '{ts}.{method}.{path}' (timestamp=밀리초, METHOD 대문자, path 는 쿼리 제외). "
                f"응답: {resp.text[:300]}",
                status=resp.status_code, body=resp.text)
        if resp.status_code >= 400:
            raise NaverAdsError(f"HTTP {resp.status_code} {method} {path}: {resp.text[:300]}",
                                status=resp.status_code, body=resp.text)
        return resp.json()

    def get(self, path: str, **params) -> Any:
        return self.request("GET", path, params=params or None)

    def close(self) -> None:
        self._http.close()
