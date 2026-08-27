-- ============================================================
-- 콘텐츠(주제) 채널 배분안 — "이 주제의 소재를 어느 채널에 어떻게 나눌까"
--
-- 흐름 (사장님 확정 2026-08-28):
--   주제 폴더에 소재가 차면 → [집 PC] AI 가 배분안 작성(blog_plan 잡)
--   → 여기 저장 → [웹] 사장님이 보고 승인/반려 → 각 채널이 배분대로 소비
--
-- plan(jsonb) 예:
--   {"angle": "자르는 순간의 단면", "channels": {
--      "blog":     {"photos": ["주제/IMG_1.jpg", ...], "clip": "주제/_클립/x.mp4",
--                   "title_hint": "송도 …"},
--      "insta":    {"reel": "훅=단면 티저, 6샷 구성", "cover": "주제/IMG_2.jpg"},
--      "danggeun": {"photos": ["…"], "copy_hint": "동네 이웃 말투 …"},
--      "place":    {"photos": ["…"]}}}
--
-- 실행: Supabase 대시보드 → SQL Editor → 이 파일 전체 붙여넣고 Run
-- ============================================================

create table if not exists public.media_plans (
    id          bigint generated always as identity primary key,
    topic       text not null,                      -- 원본소재의 주제 폴더 이름
    plan        jsonb not null,
    status      text not null default 'pending',    -- pending / approved / rejected
    created_at  timestamptz not null default now(),
    decided_at  timestamptz
);
create index if not exists media_plans_status_idx
    on public.media_plans (status, created_at desc);
