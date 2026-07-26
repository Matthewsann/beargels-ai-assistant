-- ---------------------------------------------------------------------------
-- 이미 답글을 단 리뷰 구분 (2026-07-26)
--
-- Supabase SQL Editor 에 붙여넣고 Run. 여러 번 실행해도 안전합니다.
--
-- 왜: 크롤러는 '사장님 답글이 이미 달렸는지'를 알고 있는데(쿠팡 replies,
--     배민 '사장님 댓글 등록하기' 버튼 유무) 저장할 때 버려지고 있었다.
--     그래서 이미 답변한 옛 리뷰까지 직원 화면에 떴다.
--   null  = 모름(아직 확인 못 함)
--   true  = 플랫폼에 이미 답글 있음 → 직원 화면에 안 띄운다
--   false = 미답변 → 답글 초안 대상
-- ---------------------------------------------------------------------------

alter table public.reviews add column if not exists platform_replied boolean;
create index if not exists reviews_platform_replied_idx
    on public.reviews (platform_replied);
