-- 베어글스 SNS 자동화: 게시물 상태 추적 테이블
-- Supabase SQL Editor에서 실행하세요.

create table if not exists sns_posts (
    id uuid primary key default gen_random_uuid(),
    drive_file_id text not null unique,
    file_name text not null,
    mime_type text,
    status text not null default 'pending',
    -- pending | awaiting_approval | awaiting_feedback | published | cancelled | failed
    menu text,
    caption text,
    hashtags text[],
    feedback text,
    buffer_update_id text,
    error text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists idx_sns_posts_status on sns_posts (status);

-- updated_at 자동 갱신
create or replace function set_sns_posts_updated_at()
returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

drop trigger if exists trg_sns_posts_updated_at on sns_posts;
create trigger trg_sns_posts_updated_at
    before update on sns_posts
    for each row execute function set_sns_posts_updated_at();
