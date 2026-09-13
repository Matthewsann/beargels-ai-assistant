-- ---------------------------------------------------------------------------
-- 경영 대시보드 — 매출장부 시트 항목 (2026-09-13, v15)
--
-- Supabase SQL Editor 에 붙여넣고 Run. 여러 번 실행해도 안전합니다.
-- (Claude 가 Supabase 연결로 직접 적용했고, 이 파일은 기록용입니다.)
--
-- 왜: 사장님이 매출 탭에서 보고 싶은 것(2026-09-13) — 실제 매출총액(폐기·
--     식대를 뺀 값), 폐기율, 서비스&직원식대 비율, 단체주문 — 은 장부 파일의
--     '요약'이 아니라 **'매출장부' 시트**에 있다. 그 행들을 달마다 같이 담는다.
--     행 이름: 매장_실제_매출총액 · 매장_매출_폐기 · 매장_매출_서비스&직원식대 ·
--     매장_단체주문 · (있으면) 매장_단체주문_건수
-- ---------------------------------------------------------------------------
alter table public.ledger_monthly
  add column if not exists store_actual_sales bigint,   -- 매장_실제_매출총액
  add column if not exists store_waste        bigint,   -- 매장_매출_폐기
  add column if not exists store_staff_meal   bigint,   -- 매장_매출_서비스&직원식대
  add column if not exists group_amount       bigint,   -- 매장_단체주문 (금액)
  add column if not exists group_count        integer;  -- 매장_단체주문_건수 (시트에 행이 있을 때만)
