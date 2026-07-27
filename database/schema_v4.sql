-- ---------------------------------------------------------------------------
-- 에러 자동 기록 (2026-07-28)
--
-- Supabase SQL Editor 에 붙여넣고 Run. 여러 번 실행해도 안전합니다.
--
-- 왜 DB 에 남기나:
--   PythonAnywhere 의 에러 로그는 로그인해야 볼 수 있어서, 집 PC 의 새벽
--   자동 점검이 읽을 수 없다. 그래서 앱이 스스로 에러를 여기에 적어두고,
--   집 PC 가 매일 이 표를 읽어 원인을 고친다.
--
-- source: service(직원 웹앱) / worker(집 PC 일꾼)
-- fixed : 자동 점검이 처리한 건은 true 로 바꿔 다음 날 또 보지 않게 한다.
-- ---------------------------------------------------------------------------

create table if not exists public.error_log (
    id        bigint generated always as identity primary key,
    at        timestamptz not null default now(),
    source    text not null default 'service',
    path      text,                    -- 어느 화면에서 났는지
    kind      text,                    -- 예외 종류(ValueError 등)
    message   text,                    -- 한 줄 요약
    detail    text,                    -- 스택 트레이스(앞부분만)
    fixed     boolean not null default false,
    note      text                     -- 자동 점검이 남긴 처리 메모
);
create index if not exists error_log_at_idx    on public.error_log (at desc);
create index if not exists error_log_fixed_idx on public.error_log (fixed, at desc);
