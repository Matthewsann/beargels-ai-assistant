-- ---------------------------------------------------------------------------
-- 알림 저장소 notifications — Notification Layer Phase 1 (2026-09-11)
--
-- Supabase SQL Editor 에 붙여넣고 Run. 여러 번 실행해도 안전합니다.
-- ⚠️ 아직 실행하지 않았습니다(prepared / not executed). 사장님이 실행합니다.
--    실행 전에는 아무 코드도 이 표를 읽거나 쓰지 않습니다 — 기존 알림
--    (error_log → 알림함)은 그대로 돕니다.
--
-- 왜: 사장님용 알림이 error_log(기술 오류 로그)에 얹혀 있어서
--     ① 중요도·수신자·재알림 개념이 없고
--     ② 중복 방지 키가 '날짜'라 같은 문제가 매일 새 행으로 태어나며
--        (잔소리 7일 연속·고객 요청 9일 연속 실측)
--     ③ [확인]=fixed 하나라 '읽었다'와 '해결됐다'를 구분하지 못하고
--     ④ 기술 오류 61건이 50건 창을 밀어내 사장님 알림이 화면에서 사라졌다.
--     error_log 를 넓히지 않고(새벽 점검·장부 경고가 그 표를 읽는 계약을
--     지키려고) 사람용 알림만 담을 표를 따로 둔다.
--     설계: P0-1 Notification Layer 설계 보고서 ⑥(2026-09-10).
--
-- 한 줄 = 문제 하나. 같은 문제가 다시 나면 새 행이 아니라 occurrences 와
-- last_seen_at 을 올린다. 그래서 dedupe_key 는 '열려 있는 동안만' 유일하다
-- (아래 부분 유니크 인덱스). 해결된 뒤 같은 문제가 또 나면 그건 재발이라
-- 새 행이다.
--
-- status 수명주기: open → sending → sent → read → resolved
--   open      만들어짐. 알림함에 바로 보임. 발송 대기.
--   sending   발송 중(Dispatcher 가 잠근 상태 — 일꾼이 죽어도 회수하려고).
--   sent      바깥 채널로 1회 이상 나감. 웹 전용이면 open → read 로 건너뜀.
--   read      사람이 [확인]. 문제는 아직 열려 있다 — 재알림 정책은 계속.
--   resolved  해결. 자동(업무 완료·요청 공유·세션 복구) 또는 [처리됨].
--   muted     사람이 "이건 그만". 같은 키를 당분간 만들지 않는다.
--   expired   참고성(info)이 시간 지나 저절로 닫힘.
--
-- 이 파일이 만드는 것: 표 1개 · 인덱스 3개 · RLS 정책 1개. 기존 표는 건드리지
-- 않는다. task_id 는 work_tasks(id) 를 가리키는 FK — work_tasks 는 schema_v10
-- 에서 bigint identity 로 만들어져 있어 타입이 맞고, 업무를 지우면 null 로
-- 풀린다(알림은 남는다). 회의 할 일(meeting_tasks, 화면 id "m:34")은 FK 로
-- 못 가리키므로 source_ref 에 'task:m:34' 문자열로 잇는다.
-- ---------------------------------------------------------------------------

create table if not exists public.notifications (
    id              bigint generated always as identity primary key,

    -- 무엇이
    event_type      text not null,                 -- 예: work.overdue / request.unshared / session.expired
    dedupe_key      text not null,                 -- 문제의 정체성. 예: work.overdue:w:12 / session.expired:배민
    severity        text not null default 'normal'
                    check (severity in ('critical', 'high', 'normal', 'info')),
    title           text not null,                 -- 폰 한 줄
    message         text,                          -- 원인 — 할 일 (사장님 말)
    link            text,                          -- 화면 경로. 예: /work, /review

    -- 누구에게 (people 표가 생기기 전까지는 문자열: 'owner' / 이름)
    recipient_type  text not null default 'role'
                    check (recipient_type in ('role', 'person', 'system')),
    recipient_id    text not null default 'owner',

    -- 어디까지 왔나
    status          text not null default 'open'
                    check (status in ('open', 'sending', 'sent', 'read',
                                      'resolved', 'muted', 'expired')),
    occurrences     integer not null default 1,    -- 같은 문제가 몇 번 확인됐나
    last_seen_at    timestamptz not null default now(),   -- 처음 본 시각은 created_at
    sent_at         timestamptz,
    read_at         timestamptz,
    resolved_at     timestamptz,
    resolved_by     text,                          -- 'auto' / 사람 표시
    resolve_reason  text,                          -- task_done / request_shared / manual / auto_recovered / expired
    remind_at       timestamptz,                   -- 다음 재알림 시각(정책이 정함). null = 재알림 없음
    remind_count    integer not null default 0,

    -- 어디서 왔고 무엇과 이어지나
    source          text not null default 'worker', -- worker / service (누가 만들었나)
    source_ref      text,                          -- 원천 참조. 예: review:1734 / task:w:12 / task:m:34 / job:992 / error_log:120
    task_id         bigint references public.work_tasks(id) on delete set null,
    attempts        jsonb not null default '{}'::jsonb,   -- 채널별 발송 기록 {"email":{"at":…,"ok":false,"err":…}}

    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

-- 같은 문제는 열려 있는 동안 한 줄만 — DB 가 중복을 막는다.
-- resolved/muted/expired 는 제외: 해결된 뒤 재발하면 새 행이 맞다.
create unique index if not exists notifications_open_key
    on public.notifications (dedupe_key)
    where status in ('open', 'sending', 'sent', 'read');

-- Dispatcher 가 "보낼 것·다시 알릴 것"을 집는다
create index if not exists notifications_due_idx
    on public.notifications (status, remind_at);

-- 알림함이 "열린 것부터 최신순"으로 읽는다
create index if not exists notifications_inbox_idx
    on public.notifications (status, created_at desc);

-- ---------- RLS + anon 정책 (기존 표와 동일한 실용적 타협) ----------
alter table public.notifications enable row level security;

do $$ begin
  create policy notifications_anon on public.notifications
      for all to anon using (true) with check (true);
exception when duplicate_object then null; end $$;
