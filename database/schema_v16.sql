-- ---------------------------------------------------------------------------
-- 배달 플랫폼 수수료 일별 집계 (2026-09-23, v16)
--
-- Supabase SQL Editor 에 붙여넣고 Run. 여러 번 실행해도 안전합니다.
-- (Claude 가 Supabase 연결로 직접 적용했고, 이 파일은 기록용입니다.)
--
-- 왜: 사장님(2026-09-22)이 배달 매출 구조 — 매출 대비 수수료·배달비·광고비가
--     기간별로 얼마인지 — 를 보고 싶어 했다. 쿠팡이츠 포털의 주문 목록 응답
--     (`orderSettlement`)에 건별로 중개이용료·결제수수료·배달비·부가세·상점부담
--     쿠폰·광고비가 들어 있고, 집 PC 일꾼이 이미 `orders.raw` 에 원본째 저장하고
--     있다. 그 원본을 날짜·플랫폼별로 합친 표가 이것이다(`database/platform_fees.py`).
--     배민은 아직 보류(2026-09-22 사장님) — 칸만 같은 구조로 비워 둔다.
--
-- 금액은 전부 원, 공급가액(부가세 별도). vat 는 수수료 셋에 붙는 부가세 합.
-- net 은 포털의 '기본 정산 예정 금액'(= 매출 − 쿠폰 − 수수료 − 배달비 − 부가세).
-- ---------------------------------------------------------------------------
create table if not exists public.platform_fees_daily (
  platform     text    not null,              -- 'coupang' | 'baemin'
  day          date    not null,
  orders       integer not null default 0,    -- 완료 주문 건수
  cancelled    integer not null default 0,    -- 취소 건수(금액엔 안 넣는다)
  sales        bigint  not null default 0,    -- 매출액(판매가, 취소분 제외)
  coupon       bigint  not null default 0,    -- 상점부담 쿠폰
  service_fee  bigint  not null default 0,    -- 중개 이용료
  payment_fee  bigint  not null default 0,    -- 결제대행사 수수료
  delivery_fee bigint  not null default 0,    -- 배달비
  vat          bigint  not null default 0,    -- 수수료 부가세
  ad_fee       bigint  not null default 0,    -- 광고비(주문에 붙은 CPC)
  net          bigint  not null default 0,    -- 정산 예정 금액
  updated_at   timestamptz not null default now(),
  primary key (platform, day)
);
create index if not exists platform_fees_daily_day_idx on public.platform_fees_daily (day);
