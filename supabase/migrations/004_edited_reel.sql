-- 자동 편집된 릴스의 공개 URL 저장
-- Supabase SQL Editor에서 실행하세요.
--
-- 릴스 주제를 부르면 편집기(video_editor)가 9:16 릴스 mp4를 만들어
-- Supabase 공개 버킷(sns-media)에 올린다. 그 공개 URL을 여기 저장해두고,
-- 승인 시 원본이 아니라 이 편집본을 Buffer로 발행한다.

alter table sns_posts add column if not exists edited_media_url text;
