-- ---------------------------------------------------------------------------
-- 경영 대시보드 — 월별 장부 요약 (2026-09-03)
--
-- Supabase SQL Editor 에 붙여넣고 Run. 여러 번 실행해도 안전합니다.
-- (Claude 가 Supabase 연결로 직접 적용했고, 이 파일은 기록용입니다.)
--
-- 왜: 매출 대시보드(/sales)를 참고 대시보드(경영대시보드-사양서, 2026-08-22)
--     처럼 6탭(진단·매출·비용·상품·운영·시뮬)으로 넓히기로 했다(사장님
--     확정 2026-09-03). 원가·고정비·정산총액·영업이익은 포스가 아니라
--     사장님이 매달 정리하는 구글 시트 '베어글스_장부'의 요약시트에 있다.
--     집 PC 일꾼(worker/ledger_sheet.py)이 그 시트를 CSV 로 내려받아 달마다
--     한 행씩 여기에 넣는다.
--
-- ⚠️ 영업이익 = 정산총액 − 매입원가 − 고정비. 정산총액은 카드·배달 수수료가
--    이미 빠진 실입금액이라, 매출총액에서 수수료를 또 빼면 이중 차감이다.
--
-- status: confirmed(월이 끝난 뒤 시트가 갱신됨) / estimate(월이 끝나기 전
--         값 = 사장님 예상치). 화면은 estimate 를 '예상'으로 표시한다.
-- 목표치(시트 '목표' 열)는 menu_settings 의 'ledger_targets' 키에 둔다.
-- ---------------------------------------------------------------------------

create table if not exists public.ledger_monthly (
    ym                  text primary key,          -- '2026-07'
    sales_total         bigint,                    -- 매출총액
    orders_total        integer,                   -- 총주문건수
    store_sales         bigint,                    -- 매장매출
    store_orders        integer,
    delivery_sales      bigint,                    -- 배달매출
    delivery_orders     integer,
    settlement          bigint,                    -- 정산총액(실입금)
    cogs                bigint,                    -- 매입원가 총액
    delivery_fees       bigint,                    -- 배달 수수료(+광고)
    fixed_cost          bigint,                    -- 고정비 총액
    labor_cost          bigint,                    -- 인건비 (시트에 금액이 있는 달만)
    labor_rate          numeric,                   -- 인건비율 (0~1)
    rent_rate           numeric,                   -- 임대료율 (0~1)
    op_profit           bigint,                    -- 영업이익 (시트 값)
    non_op_cost         bigint,                    -- 영업외비용
    net_profit          bigint,                    -- 순이익
    status              text not null default 'confirmed',   -- confirmed / estimate
    source_modified_at  timestamptz,               -- 시트 마지막 수정 시각
    imported_at         timestamptz not null default now()
);

-- ---------- RLS + anon 정책 (기존 표와 동일한 실용적 타협) ----------
alter table public.ledger_monthly enable row level security;

do $$ begin
  create policy ledger_monthly_anon on public.ledger_monthly
      for all to anon using (true) with check (true);
exception when duplicate_object then null; end $$;
