-- ---------------------------------------------------------------------------
-- 마케팅 캘린더 (2026-08-27)
--
-- Supabase SQL Editor 에 붙여넣고 Run. 여러 번 실행해도 안전합니다.
--
-- 왜: 마케팅 실행 기록(캠페인)과 포스 장부의 일별·상품별 매출을 연결해
--     "이 마케팅이 실제 매출/타겟 상품을 움직였나"를 보는 /mkt 페이지용.
--     매출 원천은 구글드라이브 장부관리 폴더의 TOS/IMU 포스 엑셀
--     (집 PC 일꾼이 로컬 동기화 폴더를 스캔해 자동 반영).
-- ---------------------------------------------------------------------------

-- 마케팅 캠페인/변수 기록
create table if not exists public.mkt_campaigns (
    id              bigint generated always as identity primary key,
    title           text not null,
    category        text not null,            -- delivery / sns / place / store / var(변수)
    start_date      date not null,
    end_date        date,                     -- null = 진행중 (var 는 당일 단발)
    target_products jsonb,                    -- ["버터떡"] 제목에서 자동 인식 or 직접
    cost            integer,                  -- 원. null = 미입력(무료)
    memo            text,
    status          text not null default 'live',   -- live / done
    created_at      timestamptz not null default now()
);
create index if not exists mkt_campaigns_start_idx on public.mkt_campaigns (start_date);

-- 일별 매출 (채널별·출처별). 같은 (날짜,채널,출처)는 최신 파싱으로 대체.
create table if not exists public.sales_daily (
    id              bigint generated always as identity primary key,
    sale_date       date not null,
    channel         text not null,            -- store / baemin / coupang / yogiyo ...
    amount          integer not null default 0,
    orders_count    integer,
    source          text not null,            -- tos / imu / baemin_xls / coupang_xls
    imported_at     timestamptz not null default now(),
    unique (sale_date, channel, source)
);
create index if not exists sales_daily_date_idx on public.sales_daily (sale_date);

-- 일별 상품별 매출 (매장 포스 기준)
create table if not exists public.product_sales_daily (
    id              bigint generated always as identity primary key,
    sale_date       date not null,
    product         text not null,
    category        text,
    qty             integer not null default 0,
    amount          integer not null default 0,
    source          text not null,            -- tos / imu
    imported_at     timestamptz not null default now(),
    unique (sale_date, product, source)
);
create index if not exists product_sales_daily_date_idx
    on public.product_sales_daily (sale_date);
create index if not exists product_sales_daily_product_idx
    on public.product_sales_daily (product, sale_date);

-- 장부 파일 반영 로그 — 같은 파일(경로+수정시각)은 다시 파싱하지 않는다
create table if not exists public.pos_files (
    id              bigint generated always as identity primary key,
    file_name       text not null,
    file_mtime      text not null,            -- ISO 문자열
    file_size       bigint,
    kind            text,                     -- tos / imu / baemin_xls / coupang_xls / skip
    date_from       date,
    date_to         date,
    status          text not null default 'done',   -- done / error
    note            text,
    imported_at     timestamptz not null default now(),
    unique (file_name, file_mtime)
);

-- ---------- RLS + anon 정책 (기존 테이블과 동일한 실용적 타협) ----------
alter table public.mkt_campaigns       enable row level security;
alter table public.sales_daily         enable row level security;
alter table public.product_sales_daily enable row level security;
alter table public.pos_files           enable row level security;

do $$ begin
  create policy mkt_campaigns_anon on public.mkt_campaigns
      for all to anon using (true) with check (true);
exception when duplicate_object then null; end $$;
do $$ begin
  create policy sales_daily_anon on public.sales_daily
      for all to anon using (true) with check (true);
exception when duplicate_object then null; end $$;
do $$ begin
  create policy product_sales_daily_anon on public.product_sales_daily
      for all to anon using (true) with check (true);
exception when duplicate_object then null; end $$;
do $$ begin
  create policy pos_files_anon on public.pos_files
      for all to anon using (true) with check (true);
exception when duplicate_object then null; end $$;
