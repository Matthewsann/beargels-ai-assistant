-- ---------------------------------------------------------------------------
-- 직원용 리뷰 답글 웹서비스용 확장 (2026-07-26)
--
-- Supabase SQL Editor 에 통째로 붙여넣고 Run 하세요. 여러 번 실행해도 안전합니다.
--
-- 구조: [직원 웹앱] --(요청/조회)--> [Supabase] <--(폴링/저장)-- [집 PC 일꾼]
--   · jobs          : 직원이 누른 '리뷰수집' 요청 대기열
--   · worker_status : 집 PC 일꾼이 살아있는지(heartbeat)
--   · reviews       : 답글 초안 칼럼 추가
-- ---------------------------------------------------------------------------

-- 1) 리뷰 테이블에 답글 초안 보관용 칼럼 추가
alter table public.reviews add column if not exists reply_draft      text;
alter table public.reviews add column if not exists draft_updated_at timestamptz;
-- reply_status: none(미답변) / drafted(초안있음) / posted(답글 등록 완료)

-- 2) 수집 요청 대기열
create table if not exists public.jobs (
    id            bigint generated always as identity primary key,
    kind          text not null default 'collect',
    status        text not null default 'pending',   -- pending/running/done/error
    requested_by  text,                              -- 누가 눌렀는지(이름)
    requested_at  timestamptz not null default now(),
    started_at    timestamptz,
    finished_at   timestamptz,
    message       text,                              -- 실패 사유 등
    result_count  integer                            -- 새로 만든 답글 초안 수
);
create index if not exists jobs_status_idx on public.jobs (status, requested_at);

-- 3) 집 PC 일꾼 상태(살아있는지 확인용)
create table if not exists public.worker_status (
    name       text primary key,                     -- 'home-pc'
    last_seen  timestamptz not null default now(),
    state      text,                                 -- idle/working/error
    message    text
);
