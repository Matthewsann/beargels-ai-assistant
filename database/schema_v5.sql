-- ---------------------------------------------------------------------------
-- 답글 자동화 1단계: AI 원본 보존 + 수정률 측정 (2026-08-04)
--
-- 왜: 직원이 답글을 고치면 AI 초안이 덮어써져 사라진다. 'AI가 뭘 틀렸는지'
--     비교할 수 없으면 고도화도, 자동화 판단도 불가능하다.
--   ai_draft   : AI 가 만든 원본(직원 수정과 무관하게 보존)
--   kind       : 리뷰 유형(photo_only/praise_detail/…) — 유형별 수정률 계산용
--   posted_at  : '답글 등록함' 누른 시각
-- 수정률 = (posted 시점에 reply_draft ≠ ai_draft 인 비율), 유형별 집계.
-- ---------------------------------------------------------------------------

alter table public.reviews add column if not exists ai_draft  text;
alter table public.reviews add column if not exists kind      text;
alter table public.reviews add column if not exists posted_at timestamptz;
create index if not exists reviews_kind_idx on public.reviews (kind, reply_status);
