-- ---------------------------------------------------------------------------
-- 매출 대시보드 — 시간대별 매출 (2026-09-03)
--
-- Supabase SQL Editor 에 붙여넣고 Run. 여러 번 실행해도 안전합니다.
-- (Claude 가 Supabase 연결로 직접 적용했고, 이 파일은 기록용입니다.)
--
-- 왜: 사장님이 매출 대시보드에서 "요일·시간대 패턴"을 보고 싶어 했는데
--     장부에는 결제 한 건마다 시각이 있으면서도(TOS '결제 상세내역' 시트의
--     결제시각, IMU 건별 내역의 매출일시) 일꾼이 날짜 단위로만 옮기고
--     시간을 버리고 있었다. 이 표가 그 시간을 받는다.
--
-- 채움: worker/pos_import.py 가 장부 엑셀을 읽을 때 sales_daily 와 함께
--       (날짜, 시, 채널, 출처) 단위로 upsert 한다. 과거 장부도 force 스캔으로
--       한 번 채웠다(2025-10 ~).
--
-- 월 목표(매장/배달)는 새 표 대신 menu_settings 의 'sales_goals' 키에
-- {"2026-09": {"store": 15000000, "delivery": 20000000}} 꼴로 둔다 —
-- 표 하나 더 만드는 것보다 기존 key-value 창고가 단순하다.
-- ---------------------------------------------------------------------------

create table if not exists public.sales_hourly (
    id              bigint generated always as identity primary key,
    sale_date       date not null,
    hour            smallint not null,            -- 0~23 (매장 시간, KST)
    channel         text not null,                -- store / baemin / coupang / yogiyo / ddangyo / etc
    amount          integer not null default 0,
    orders_count    integer not null default 0,   -- 결제 건수
    source          text not null,                -- tos / imu
    imported_at     timestamptz not null default now(),
    unique (sale_date, hour, channel, source)
);

create index if not exists sales_hourly_date_idx
    on public.sales_hourly (sale_date);

-- ---------- RLS + anon 정책 (기존 표와 동일한 실용적 타협) ----------
alter table public.sales_hourly enable row level security;

do $$ begin
  create policy sales_hourly_anon on public.sales_hourly
      for all to anon using (true) with check (true);
exception when duplicate_object then null; end $$;
