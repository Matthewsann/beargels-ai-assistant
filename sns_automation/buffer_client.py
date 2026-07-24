"""Buffer 새 API(GraphQL) 연동 — 인스타그램 발행.

Buffer의 옛 REST API는 신규 발급이 중단됐고, 새 Public API로 대체됐다.
토큰은 Buffer 웹사이트 → Settings → API 에서 발급 (Bearer 인증).

발행 흐름:
    createPost 뮤테이션 (channelId + text + assets[image.url])
    mode: shareNow(즉시) / addToQueue(큐의 다음 슬롯에 예약)

이미지/영상은 Buffer 서버가 접근 가능한 공개 URL이어야 한다
(media_host.py 가 Supabase 공개 버킷에 잠깐 올려 URL을 만들어 준다).
"""

import logging

import httpx

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.buffer.com/graphql"

_CREATE_POST_MUTATION = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess { post { id status dueAt } }
    ... on NotFoundError { message }
    ... on UnauthorizedError { message }
    ... on UnexpectedError { message }
    ... on RestProxyError { message }
    ... on LimitReachedError { message }
    ... on InvalidInputError { message }
  }
}
"""

_CHANNELS_QUERY = """
query Channels($orgId: OrganizationId!) {
  channels(input: { organizationId: $orgId }) {
    id name service displayName isDisconnected
  }
}
"""

_ACCOUNT_QUERY = "{ account { id email organizations { id name } } }"


class BufferError(Exception):
    pass


class BufferClient:
    def __init__(self, access_token: str, channel_id: str):
        self.access_token = access_token
        self.channel_id = channel_id

    async def _graphql(self, query: str, variables: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=60) as client:
            res = await client.post(
                GRAPHQL_URL,
                headers={"Authorization": f"Bearer {self.access_token}"},
                json={"query": query, "variables": variables or {}},
            )
        body = res.json()
        if res.status_code != 200 or body.get("errors"):
            errors = body.get("errors") or [{"message": res.text}]
            message = "; ".join(e.get("message", "?") for e in errors)
            raise BufferError(f"Buffer API 오류: {message}")
        return body["data"]

    async def verify(self) -> dict:
        """토큰이 유효한지 확인. 계정 정보를 반환."""
        data = await self._graphql(_ACCOUNT_QUERY)
        return data["account"]

    async def create_post(
        self,
        text: str,
        image_url: str | None = None,
        video_url: str | None = None,
        share_now: bool = False,
    ) -> dict:
        """게시물을 만든다. 기본은 큐 예약(addToQueue), share_now=True면 즉시 발행."""
        assets = []
        if image_url:
            assets.append({"image": {"url": image_url}})
        elif video_url:
            assets.append({"video": {"url": video_url}})

        data = await self._graphql(
            _CREATE_POST_MUTATION,
            {
                "input": {
                    "channelId": self.channel_id,
                    "text": text,
                    "assets": assets,
                    "mode": "shareNow" if share_now else "addToQueue",
                    "source": "api",
                }
            },
        )
        payload = data["createPost"]
        if payload["__typename"] != "PostActionSuccess":
            raise BufferError(f"Buffer 발행 실패: {payload.get('message', payload['__typename'])}")

        post = payload["post"]
        logger.info(
            "Buffer 등록 완료: post_id=%s status=%s dueAt=%s",
            post["id"], post.get("status"), post.get("dueAt"),
        )
        return post
