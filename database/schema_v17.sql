-- ---------------------------------------------------------------------------
-- 배달 플랫폼 광고비 일별 + 정산 명세 (2026-09-24, v17)
--
-- Supabase SQL Editor 에 붙여넣고 Run. 여러 번 실행해도 안전합니다.
-- (Claude 가 Supabase 연결로 직접 적용했고, 이 파일은 기록용입니다.)
--
-- 왜: 배민의 클릭 광고비(우리가게클릭)는 주문에 안 붙는다. 사장님(2026-09-24)
--     "기간에 맞춰서 광고료에 같이 포함해줘". 셀프서비스 정산내역 명세
--     (`/v3/settle/history/details/{giveId}`)의 `cpcDetails.dailyDetails` 에
--     날짜별 클릭 광고비가 있어 그걸 날짜별로 담는다(`platform_ad_daily`).
--     `platform_fees.rebuild` 가 이 표를 읽어 platform_fees_daily.ad_fee 에 얹는다.
--     정산 명세 원본은 `platform_settlements` 에 남긴다(입금액·기간·CPC 합).
-- ---------------------------------------------------------------------------
create table if not exists public.platform_ad_daily (
  platform   text    not null,              -- 'baemin' (쿠팡은 주문에 붙어 안 쓴다)
  day        date    not null,
  ad_fee     bigint  not null default 0,    -- 공급가액(원)
  ad_vat     bigint  not null default 0,    -- 부가세(명세 cpcVat 를 날짜 비율로 나눔)
  source     text,                          -- 'settle:cpcDetails'
  give_id    bigint,                        -- 어느 정산 명세에서 왔나
  updated_at timestamptz not null default now(),
  primary key (platform, day)
);
create table if not exists public.platform_settlements (
  platform    text   not null,
  give_id     bigint not null,
  start_date  date,
  end_date    date,
  deposit     bigint,                       -- 입금금액
  cpc_total   bigint,                       -- 우리가게클릭 차감(부가세 포함, 음수)
  raw         jsonb,
  updated_at  timestamptz not null default now(),
  primary key (platform, give_id)
);
