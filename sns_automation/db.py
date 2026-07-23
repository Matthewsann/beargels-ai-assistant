"""Supabase sns_posts 테이블 저장소.

supabase-py는 동기 클라이언트이므로 async 코드에서는
asyncio.to_thread로 감싸서 호출한다 (pipeline.py 참고).
"""

from typing import Any, Optional

from supabase import Client, create_client

# 게시물 상태
STATUS_PENDING = "pending"                # 파일 감지, 캡션 생성 전
STATUS_AWAITING_APPROVAL = "awaiting_approval"  # 텔레그램 승인 대기
STATUS_AWAITING_FEEDBACK = "awaiting_feedback"  # 수정 내용 입력 대기
STATUS_PUBLISHED = "published"            # Buffer 발행 완료
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"


class PostRepository:
    def __init__(self, supabase_url: str, supabase_key: str):
        self.client: Client = create_client(supabase_url, supabase_key)

    def is_processed(self, drive_file_id: str) -> bool:
        res = (
            self.client.table("sns_posts")
            .select("id")
            .eq("drive_file_id", drive_file_id)
            .limit(1)
            .execute()
        )
        return len(res.data) > 0

    def create_post(self, drive_file_id: str, file_name: str, mime_type: str) -> dict:
        res = (
            self.client.table("sns_posts")
            .insert(
                {
                    "drive_file_id": drive_file_id,
                    "file_name": file_name,
                    "mime_type": mime_type,
                    "status": STATUS_PENDING,
                }
            )
            .execute()
        )
        return res.data[0]

    def get_post(self, post_id: str) -> Optional[dict]:
        res = self.client.table("sns_posts").select("*").eq("id", post_id).limit(1).execute()
        return res.data[0] if res.data else None

    def update_post(self, post_id: str, **fields: Any) -> dict:
        res = self.client.table("sns_posts").update(fields).eq("id", post_id).execute()
        return res.data[0]
