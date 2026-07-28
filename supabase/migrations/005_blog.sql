-- ============================================================
-- 블로그 자동화 테이블 (베어글스 AI 비서 통합용)
--
-- 구조는 리뷰답글(schema_v2.sql)과 동일한 패턴을 따른다:
--   [웹] service/app.py  --(jobs 에 요청)-->  [집 PC 일꾼] worker
--   [집 PC 일꾼] --(결과 기록)--> blog_* 테이블 --> [웹]이 읽어서 보여줌
--
-- jobs 테이블은 새로 만들지 않고 기존 것을 재사용한다(kind 로 구분):
--   kind = 'blog_recommend' 글감 추천   / 'blog_draft'   초안 생성
--          'blog_publish'   네이버 임시저장 / 'blog_rank' 순위 확인
--
-- 실행: Supabase 대시보드 → SQL Editor → 이 파일 전체 붙여넣고 Run
-- ============================================================

-- 1) 글 창고 — 기존 automation/library/ 폴더를 대체
create table if not exists public.blog_posts (
    id             bigint generated always as identity primary key,
    title          text,
    body           text,                              -- 네이버에 붙여넣을 본문
    post_type      text,                              -- 정보성/신메뉴/일상/후기/이벤트
    main_keyword   text,
    sub_keywords   jsonb,                             -- ["세부1","세부2"]
    tags           jsonb,
    status         text not null default 'ready',     -- ready/scheduled/published/trashed
    scheduled_at   timestamptz,                       -- 예약 발행 시각
    published_at   timestamptz,                       -- 실제 발행 시각(사장님이 표시)
    naver_url      text,                              -- 발행된 글 주소
    prepared_at    timestamptz,                       -- 네이버 임시저장 완료 시각
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);
create index if not exists blog_posts_status_idx    on public.blog_posts (status, created_at desc);
create index if not exists blog_posts_scheduled_idx on public.blog_posts (scheduled_at);

-- 2) 키워드 순위 추적 — 기존 webapp/rank_history.json 을 대체
create table if not exists public.blog_ranks (
    id           bigint generated always as identity primary key,
    keyword      text not null,
    checked_at   timestamptz not null default now(),
    found        boolean not null default false,
    rank         integer,                             -- 전체 순위(1이 최상위)
    page         integer,                             -- 약 10위 = 1페이지
    pos_in_page  integer,
    scanned      integer,                             -- 몇 개까지 훑었는지
    post_id      bigint references public.blog_posts (id) on delete set null
);
create index if not exists blog_ranks_kw_idx on public.blog_ranks (keyword, checked_at desc);

-- 3) 글감 추천 — 기존 webapp/recommendations.json 을 대체
create table if not exists public.blog_recommendations (
    id             bigint generated always as identity primary key,
    priority       integer,                           -- 발행 추천 순서(1이 먼저)
    tier           text,                              -- green(승산)/yellow(장기전)/red(브랜드용)
    title          text not null,
    post_type      text,
    main_keyword   text,
    sub_keywords   jsonb,
    competition    text,                              -- 낮음/중간/높음
    search_intent  text,                              -- 정보/지역방문/브랜드
    why            text,                              -- 금고 근거 한 줄
    timing         text,
    used_post_id   bigint references public.blog_posts (id) on delete set null,
    created_at     timestamptz not null default now()
);
create index if not exists blog_recs_priority_idx on public.blog_recommendations (priority);

-- 4) jobs 테이블이 아직 없다면 만들어 둔다(리뷰답글 스키마와 동일 · 이미 있으면 건너뜀)
create table if not exists public.jobs (
    id            bigint generated always as identity primary key,
    kind          text not null default 'collect',
    status        text not null default 'pending',    -- pending/running/done/error
    requested_by  text,
    requested_at  timestamptz not null default now(),
    started_at    timestamptz,
    finished_at   timestamptz,
    message       text,
    result_count  integer
);
create index if not exists jobs_status_idx on public.jobs (status, requested_at);

-- 블로그 잡은 대상 글을 가리켜야 할 때가 있다(발행 요청 등) → 컬럼 추가(있으면 무시)
alter table public.jobs add column if not exists payload jsonb;
