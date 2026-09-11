-- ---------------------------------------------------------------------------
-- 업무 보드 — 하위 업무 (2026-09-11)
--
-- Supabase SQL Editor 에 붙여넣고 Run. 여러 번 실행해도 안전합니다.
-- 실행 전에도 보드는 예전처럼 돕니다 — '하위 업무 추가'만 "준비가 필요해요"
-- 라고 안내합니다(database/work_store.py `subtasks_ready`).
--
-- 왜: 업무 하나가 여러 손을 거친다("9월 메뉴판" = 사진 고르기 → 문구 →
--     인쇄 발주). 지금은 그걸 따로 등록하면 서로 무관한 줄 3개가 되고,
--     하나로 적으면 어디까지 했는지 안 보인다.
--
-- 한 단계만: 하위 업무의 하위 업무는 두지 않는다(화면이 폰이라 들여쓰기
--   두 단계부터는 읽히지 않는다). 코드가 막고, 표는 막지 않는다.
-- 상위 업무를 지우면 하위도 같이 지운다(on delete cascade).
-- 회의 할 일(meeting_tasks)은 상위가 될 수 없다 — 회의를 지우면 같이
--   지워져야 하는데 FK 가 다른 표를 가리키면 그 약속이 깨진다.
-- ---------------------------------------------------------------------------

alter table public.work_tasks
    add column if not exists parent_id bigint
        references public.work_tasks(id) on delete cascade;

-- 보드가 "이 업무의 하위"를 모을 때 쓴다
create index if not exists work_tasks_parent_idx
    on public.work_tasks (parent_id);
