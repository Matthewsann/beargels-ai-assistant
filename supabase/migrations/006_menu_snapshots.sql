-- 채널(배민/쿠팡/네이버)에 '실제 노출 중인' 메뉴 스냅샷
-- Supabase SQL Editor에서 실행하세요.
--
-- 집 PC 일꾼이 menu_collect 작업으로 각 채널의 메뉴를 긁어 여기 넣는다.
-- /menu 화면의 '채널 대조'가 이 스냅샷과 정본(menu_items)을 비교해
-- 이름/가격 불일치·누락을 보여준다. 수집할 때마다 채널 단위로 갈아끼운다.

create table if not exists menu_channel_snapshots (
  id           bigint generated always as identity primary key,
  channel      text not null check (channel in ('baemin','coupang','naver')),
  menu_name    text not null,
  price        integer,
  category     text,
  description  text,
  matched_sku  text,          -- 정본 SKU 자동 매칭 결과(없으면 미매칭)
  raw          jsonb,
  collected_at timestamptz default now(),
  unique (channel, menu_name)
);

create index if not exists idx_menu_snapshots_channel on menu_channel_snapshots (channel);
