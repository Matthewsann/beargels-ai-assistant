-- ---------------------------------------------------------------------------
-- 회의 기록 (2026-08-27)
--
-- Supabase SQL Editor 에 붙여넣고 Run. 여러 번 실행해도 안전합니다.
--
-- 왜: 매장 회의에서 나온 이야기·결정·할 일이 카톡과 머릿속에만 남아
--     "지난번에 뭐라고 했더라"가 반복됐다. 비서 페이지(/meeting)에서
--     적고, 검색하고, 할 일은 홈에서 챙긴다.
--
-- AI 자동 정리(내용→결정사항·할 일 추출)는 넣지 않는다(사장님 결정
-- 2026-08-27) — 직원이 직접 적는다. 나중에 붙이더라도 표는 그대로 쓴다.
-- ---------------------------------------------------------------------------

create table if not exists public.meetings (
    id            bigint generated always as identity primary key,
    meeting_date  date not null,
    title         text not null,
    category      text,                    -- 자유 입력 (기본 5종 + 직원이 추가)
    attendees     text,                    -- "사장님, 지은" (자유 입력)
    body          text,                    -- 논의 내용
    decisions     text,                    -- 결정한 것 (한 줄에 하나)
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);
create index if not exists meetings_date_idx on public.meetings (meeting_date desc);

-- 회의에서 나온 할 일. 회의를 지우면 같이 지워진다.
create table if not exists public.meeting_tasks (
    id           bigint generated always as identity primary key,
    meeting_id   bigint not null references public.meetings(id) on delete cascade,
    content      text not null,
    owner        text,                     -- 담당 (자유 입력)
    due_date     date,
    done         boolean not null default false,
    done_at      timestamptz,
    sort         integer not null default 0,
    created_at   timestamptz not null default now()
);
create index if not exists meeting_tasks_meeting_idx
    on public.meeting_tasks (meeting_id);
-- 홈 화면이 "안 끝난 할 일"만 기한순으로 훑는다
create index if not exists meeting_tasks_open_idx
    on public.meeting_tasks (done, due_date);

-- ---------- RLS + anon 정책 (기존 표와 동일한 실용적 타협) ----------
alter table public.meetings      enable row level security;
alter table public.meeting_tasks enable row level security;

do $$ begin
  create policy meetings_anon on public.meetings
      for all to anon using (true) with check (true);
exception when duplicate_object then null; end $$;
do $$ begin
  create policy meeting_tasks_anon on public.meeting_tasks
      for all to anon using (true) with check (true);
exception when duplicate_object then null; end $$;
