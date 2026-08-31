-- ---------------------------------------------------------------------------
-- 업무 보드 (2026-08-31)
--
-- Supabase SQL Editor 에 붙여넣고 Run. 여러 번 실행해도 안전합니다.
-- (2026-08-31 부터는 Claude 가 Supabase 연결로 직접 적용할 수 있어서,
--  이 파일은 '무엇을 만들었는지'를 리포에 남기는 기록용입니다.)
--
-- 왜: 관리자 업무가 **회의 기록 안에서만** 만들어졌다(meeting_tasks 는
--     meeting_id 가 필수). 그래서 회의와 상관없이 그때그때 생기는 일
--     ("9월 메뉴판 발주")을 적을 자리가 아예 없었다.
--
-- 역할 분담 (사장님 확정 2026-08-31):
--     비서  = 우선순위 매기기 · 오늘 할 것 알려주기 · 방치된 업무 리마인드
--     담당자 = 업무 등록 · 담당자/기한 정하기 · 진행 기록 · 완료 체크
--   → 우선순위는 **규칙으로 계산**한다(컬럼에 저장하지 않는다). AI 비용이
--     들지 않고, 기한·방치일이 바뀌면 저절로 따라 움직인다.
--
-- meeting_tasks 를 고쳐 쓰지 않고 표를 따로 두는 이유:
--   회의 할 일은 "그 회의에서 나온 것"이라 회의를 지우면 같이 지워진다
--   (on delete cascade). 회의와 무관한 업무가 거기 섞이면 회의 하나 지울 때
--   같이 날아간다. 화면에서는 둘을 합쳐 보여주되, 저장은 각자 자리에 둔다.
-- ---------------------------------------------------------------------------

create table if not exists public.work_tasks (
    id          bigint generated always as identity primary key,
    content     text not null,
    owner       text,                      -- 담당 (자유 입력, meeting_tasks 와 동일)
    due_date    date,
    done        boolean not null default false,
    done_at     timestamptz,
    memo        text,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

-- 보드가 "안 끝난 업무"만 기한순으로 훑는다
create index if not exists work_tasks_open_idx
    on public.work_tasks (done, due_date);

-- ---------- RLS + anon 정책 (기존 표와 동일한 실용적 타협) ----------
alter table public.work_tasks enable row level security;

do $$ begin
  create policy work_tasks_anon on public.work_tasks
      for all to anon using (true) with check (true);
exception when duplicate_object then null; end $$;
