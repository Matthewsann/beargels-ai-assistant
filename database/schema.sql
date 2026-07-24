-- ==========================================================
-- 베어글스 AI 운영 비서 — Supabase 스키마
-- ==========================================================
-- 적용:  Supabase 대시보드 → SQL Editor → 아래 전체 붙여넣고 Run
--        (publishable 키로는 DDL 불가하므로 최초 1회 수동 실행)
--
-- 보안:  현재 백엔드는 publishable(anon) 키로 접근하므로 anon 롤에 read/write
--        정책을 연다. 노출 위험 없는 개인용 단일 백엔드라 실용적 타협.
--        더 엄격히 하려면 service_role(secret) 키로 바꾸고 아래 anon 정책 제거.
-- ==========================================================

-- ---------- 주문 / 매출 ----------
create table if not exists public.orders (
    id                bigint generated always as identity primary key,
    platform          text not null,                 -- baemin / coupang / store
    order_no          text not null,                 -- 플랫폼 주문번호
    status            text,                          -- 배달완료 등
    ordered_at        text,                          -- 원문 주문시각 문자열
    ordered_date      date,                          -- 파싱된 날짜(집계용)
    menu              text,
    price             integer,                       -- 결제금액(원)
    pay_type          text,
    delivery_method   text,                          -- 한집배달/알뜰배달 등
    ad_service        text,
    raw               text,                          -- 원문(디버그/재파싱)
    collected_at      timestamptz not null default now(),
    unique (platform, order_no)
);
create index if not exists orders_date_idx     on public.orders (ordered_date);
create index if not exists orders_platform_idx on public.orders (platform, ordered_date);

-- ---------- 리뷰 / 별점 ----------
create table if not exists public.reviews (
    id                bigint generated always as identity primary key,
    platform          text not null,
    review_no         text,                          -- 플랫폼 리뷰번호
    author            text,
    rating            integer,                       -- 별점 0~5
    content           text,                          -- 본문(사진만이면 null)
    written_at        text,                          -- 원문 작성일 문자열
    written_date      date,                          -- 파싱된 날짜
    menus             jsonb,                         -- 주문 메뉴 목록
    delivery_type     text,
    reply_status      text not null default 'none',  -- none/drafted/posted
    raw               text,
    collected_at      timestamptz not null default now(),
    unique (platform, review_no)
);
create index if not exists reviews_date_idx  on public.reviews (written_date);
create index if not exists reviews_reply_idx on public.reviews (reply_status);

-- ---------- 할 일 (아침 브리핑 입력 → 진행 추적 → 저녁 리뷰) ----------
create table if not exists public.tasks (
    id                bigint generated always as identity primary key,
    task_date         date not null default current_date,
    description       text not null,
    priority          integer not null default 3,    -- 1(높음) ~ 5(낮음)
    status            text not null default 'pending', -- pending / done
    note              text,
    created_at        timestamptz not null default now(),
    completed_at      timestamptz
);
create index if not exists tasks_date_status_idx on public.tasks (task_date, status);

-- ---------- 일일 브리핑/리뷰 로그 ----------
create table if not exists public.daily_summaries (
    id                bigint generated always as identity primary key,
    summary_date      date not null default current_date,
    kind              text not null,                 -- morning / evening
    content           text not null,
    created_at        timestamptz not null default now(),
    unique (summary_date, kind)
);

-- ---------- RLS + anon 접근 정책 ----------
alter table public.orders          enable row level security;
alter table public.reviews         enable row level security;
alter table public.tasks           enable row level security;
alter table public.daily_summaries enable row level security;

drop policy if exists orders_anon_all    on public.orders;
drop policy if exists reviews_anon_all   on public.reviews;
drop policy if exists tasks_anon_all     on public.tasks;
drop policy if exists summaries_anon_all  on public.daily_summaries;

create policy orders_anon_all    on public.orders          for all to anon using (true) with check (true);
create policy reviews_anon_all   on public.reviews         for all to anon using (true) with check (true);
create policy tasks_anon_all     on public.tasks           for all to anon using (true) with check (true);
create policy summaries_anon_all on public.daily_summaries for all to anon using (true) with check (true);
