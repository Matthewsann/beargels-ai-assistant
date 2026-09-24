-- ---------------------------------------------------------------------------
-- 배달 플랫폼 지원금·보상·부분환불 (2026-09-24, v18)
--
-- Supabase SQL Editor 에 붙여넣고 Run. 여러 번 실행해도 안전합니다.
-- (Claude 가 Supabase 연결로 직접 적용했고, 이 파일은 기록용입니다.)
--
-- 왜: 사장님(2026-09-24) "고객 취소로 인한 손실보상 부분도 파악하여 오차범위 좁히기".
--     · 쿠팡 '취소·재주문 정산'의 보상 예정/완료 금액(comp) — 취소 주문은 매출 0 이라
--       보상금은 여기서만 온다 → 매출·실입금에 더한다.
--     · 배민 정산 명세의 프로모션 지원·조정(support, +) → 가게 부담 할인에서 뺀다.
--     · 배민 부분환불(refund) → 매출에서 뺀다.
--     명세 단위 값은 정산기간 날짜에 고르게 나눠 넣는다(platform_fees.settlements_to_ad_daily).
-- ---------------------------------------------------------------------------
alter table public.platform_ad_daily
  add column if not exists support bigint not null default 0,   -- 지원·조정 수입(+)
  add column if not exists comp    bigint not null default 0,   -- 취소 손실보상(+)
  add column if not exists refund  bigint not null default 0;   -- 부분환불(매출 차감, +값)
