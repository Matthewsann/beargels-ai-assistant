-- 자재에 발주처 추가 — "오늘 쿠팡에서 뭘 시켜야 하지"를 화면에서 바로 보기 위함.
-- Supabase SQL Editor에서 실행하세요.
--
-- 값: 쿠팡 / 엠즈푸드 / 본사 / 그외  (비어 있으면 '미지정')
-- 컬럼이 없어도 앱은 동작한다(자재 저장 시 supplier 를 빼고 재시도).

alter table ingredients add column if not exists supplier text;
create index if not exists idx_ingredients_supplier on ingredients (supplier);
