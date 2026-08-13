-- 자재(원부자재) + 메뉴 레시피 — 원가 자동 계산의 근거 데이터
-- Supabase SQL Editor에서 실행하세요.
--
-- 흐름: 자재 단가 등록 → 메뉴별 사용량(레시피) 등록 → 재료원가 자동 계산
--       → 판매가 결정 → 배달가 결정.
-- 자재 가격이 바뀌면 그 자재를 쓰는 모든 메뉴의 원가가 즉시 재계산된다.
-- 초기 데이터: data/ingredients_seed.json → scripts/seed_ingredients.py

create table if not exists ingredients (
  id         bigint generated always as identity primary key,
  name       text not null,
  unit       text not null default 'ea',   -- g / ml / ea
  pack_qty   numeric not null default 1,   -- 구매 단위 수량 (예: 3200g)
  pack_cost  numeric not null default 0,   -- 구매가(원) — 단위단가 = pack_cost/pack_qty
  category   text,                          -- 신선식품/공산품/베이커리/반제품 등
  note       text,
  updated_at timestamptz default now(),
  unique (name, unit)
);

create table if not exists menu_recipes (
  id            bigint generated always as identity primary key,
  sku           text not null references menu_items(sku) on delete cascade,
  ingredient_id bigint not null references ingredients(id) on delete restrict,
  qty           numeric not null,           -- 이 메뉴 1개에 쓰는 양(자재의 unit 기준)
  updated_at    timestamptz default now(),
  unique (sku, ingredient_id)
);

create index if not exists idx_menu_recipes_sku on menu_recipes (sku);
create index if not exists idx_menu_recipes_ing on menu_recipes (ingredient_id);
