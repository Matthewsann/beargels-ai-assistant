-- 세트 메뉴 구성 — 세트는 재료가 아니라 '메뉴의 묶음'이다.
-- Supabase SQL Editor에서 실행하세요.
--
-- 세트마다 재료를 처음부터 나열하면 아무도 안 한다(실제로 세트 22개 중
-- 레시피가 달린 게 0개였다). 구성 메뉴만 골라 담으면 원가가 합산되게 한다.
-- 베이글 매입가가 오르면 베이글 샌드위치와 그걸 품은 세트가 같이 갱신된다.
--
-- 선택지가 여럿인 자리('베이글 샌드위치 1종')는 같은 choice_group 으로 묶는다.
-- 그 자리의 원가는 **가장 비싼 것**으로 잡는다 — 손님이 제일 비싼 조합을
-- 고를 수 있으니 그게 최악이고, 그걸 견디면 안전하다(평균은 실제보다 좋아 보인다).

create table if not exists menu_components (
  id            bigserial primary key,
  sku           text not null,          -- 세트 메뉴
  component_sku text not null,          -- 구성 메뉴
  qty           numeric not null default 1,
  choice_group  text,                   -- 같은 값끼리 '택1'. 비면 항상 들어감
  updated_at    timestamptz default now(),
  unique (sku, component_sku, choice_group)
);

create index if not exists idx_menu_components_sku on menu_components (sku);
create index if not exists idx_menu_components_comp on menu_components (component_sku);
