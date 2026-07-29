-- 메뉴 정본(마스터) + 채널별 오버라이드 + 원가/수수료 설정
-- Supabase SQL Editor에서 실행하세요.
--
-- 배경: 채널(배민/쿠팡/네이버/매장)마다 메뉴 정보가 제각각이고 원가계산이
-- 깨져 있어, 여기를 '단일 정본'으로 삼는다. 초기 데이터는
-- data/menu_master_seed.json → scripts/seed_menu_master.py 로 넣는다.
-- 이후 수정은 직원용 웹(service)의 /menu 화면에서 한다.

create table if not exists menu_items (
  sku             text primary key,          -- 예: SAND-001
  menu_type       text default '단일',       -- 단일/세트
  category        text not null,             -- 베이커리/샌드위치/커피…
  group_name      text,                      -- 베이글/치아바타/라떼…
  name            text not null,             -- 정본 메뉴명(모든 채널 통일 기준)
  composition     text,
  description     text,
  store_price     integer,                   -- 매장가(원)
  delivery_price  integer,                   -- 배달가(원) — 별도 관리
  ingredient_cost numeric,                   -- 1인분 재료원가(원)
  cost_source     text,                      -- 원가 출처 메모
  store_active    boolean default true,
  delivery_active boolean default true,
  sort_order      integer,
  updated_at      timestamptz default now()
);

-- 채널별 예외(오버라이드). 값이 없으면 정본을 그대로 쓴다는 뜻.
create table if not exists menu_channels (
  id             bigint generated always as identity primary key,
  sku            text not null references menu_items(sku) on delete cascade,
  channel        text not null check (channel in ('store','baemin','coupang','naver')),
  name_override  text,
  price_override integer,
  active         boolean,
  note           text,
  updated_at     timestamptz default now(),
  unique (sku, channel)
);

-- 설정(카테고리별 목표 원가율, 채널 수수료율 등) — key/value(jsonb)
create table if not exists menu_settings (
  key        text primary key,
  value      jsonb not null,
  updated_at timestamptz default now()
);

create index if not exists idx_menu_items_category on menu_items (category, sort_order);
create index if not exists idx_menu_channels_sku on menu_channels (sku);
