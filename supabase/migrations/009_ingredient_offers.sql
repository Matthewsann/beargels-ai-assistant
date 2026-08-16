-- 자재 하나에 발주처별 가격을 여러 개 둔다 — "같은 자재인데 발주처마다 값이 다르다".
-- Supabase SQL Editor에서 실행하세요.
--
-- ingredients 의 pack_qty/pack_cost/supplier 는 계속 "실제로 사는 조건"이다
-- (원가 계산은 이 값 하나로 한다). 이 표는 다른 발주처들의 시세를 곁에 적어
-- 두는 곳이고, 화면이 최저가를 견줘서 "어디서 사는 게 싼지"를 알려준다.

create table if not exists ingredient_offers (
  id          bigserial primary key,
  ingredient_id bigint not null references ingredients(id) on delete cascade,
  supplier    text   not null,
  pack_qty    numeric,          -- 이 발주처에서 한 번 사면 오는 양(자재와 같은 단위)
  pack_cost   numeric,          -- 그 값
  note        text,
  updated_at  timestamptz default now(),
  unique (ingredient_id, supplier)
);

create index if not exists idx_ing_offers_ing on ingredient_offers (ingredient_id);
