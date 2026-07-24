-- 폴더(주제) 기반 on-demand 발행으로 전환
-- Supabase SQL Editor에서 실행하세요.
--
-- 기존: 드라이브를 폴링해 파일마다 처리 (drive_file_id UNIQUE)
-- 변경: 텔레그램에서 주제 폴더명을 부르면 그 폴더를 한 게시물로 처리.
--       같은 주제를 다시 부를 수 있어야 하므로 UNIQUE 제약을 제거하고,
--       폴더 정보를 저장한다.

-- 재실행 허용: drive_file_id 유니크 제약 제거
alter table sns_posts drop constraint if exists sns_posts_drive_file_id_key;

-- 주제 폴더 정보
alter table sns_posts add column if not exists folder_id text;
alter table sns_posts add column if not exists is_reel boolean default false;
alter table sns_posts add column if not exists overlay_text text;
