"""notifications 표 마이그레이션(database/schema_v13.sql) 구조 검증 — Phase 1 (2026-09-11).

왜: 이 파일은 아직 DB 에 적용되지 않았다(사장님이 SQL Editor 에서 실행한다).
    적용되기 전에 사람이 읽는 것만으로 놓치기 쉬운 것을 기계가 잡는다 —
    필수 컬럼 누락, status 허용값 오타, 부분 유니크 인덱스가 resolved 를
    잘못 포함하는 것, work_tasks 와 FK 타입이 안 맞는 것, 기존 표를 건드리는 문.

DB·네트워크 불필요. 파일을 읽어 구조만 본다(문법은 Postgres 가 최종 판정 —
여기서는 괄호·문장 종결·키워드 수준만).
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SQL_PATH = ROOT / "database" / "schema_v13.sql"
WORK_TASKS_SQL = ROOT / "database" / "schema_v10.sql"


@pytest.fixture(scope="module")
def sql() -> str:
    return SQL_PATH.read_text(encoding="utf-8")


def _strip_comments(s: str) -> str:
    return re.sub(r"--[^\n]*", "", s)


def _statements(s: str) -> list[str]:
    """`;` 로 문장을 나눈다. do $$ … $$ 블록 안의 `;` 는 문장 경계가 아니다."""
    body = _strip_comments(s)
    out, buf, in_dollar = [], [], False
    for line in body.splitlines():
        if line.count("$$") % 2 == 1:
            in_dollar = not in_dollar
        buf.append(line)
        if not in_dollar and line.rstrip().endswith(";"):
            out.append("\n".join(buf).strip())
            buf = []
    tail = "\n".join(buf).strip()
    assert not tail, f"세미콜론으로 안 끝난 꼬리가 있다: {tail[:80]!r}"
    return out


def _create_table_block(s: str) -> str:
    m = re.search(r"create table if not exists public\.notifications\s*\((.*?)\n\);",
                  _strip_comments(s), re.S | re.I)
    assert m, "create table if not exists public.notifications ( … ); 블록이 없다"
    return m.group(1)


def _column_names(block: str) -> list[str]:
    names = []
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith(("check", "constraint", "primary", "unique")):
            continue
        m = re.match(r"([a-z_]+)\s+[a-z]", line)
        if m:
            names.append(m.group(1))
    return names


# ── 1. 문법(구조) ───────────────────────────────────────────────────────────

def test_sql_is_well_formed(sql):
    body = _strip_comments(sql)
    assert body.count("(") == body.count(")"), "괄호 짝이 안 맞는다"
    assert body.count("'") % 2 == 0, "따옴표 짝이 안 맞는다"
    stmts = _statements(sql)
    assert len(stmts) >= 6                       # 표 1 · 인덱스 3 · RLS 2
    assert all(st.endswith(";") for st in stmts)


def test_migration_is_idempotent_and_touches_only_notifications(sql):
    body = _strip_comments(sql).lower()
    for kw in ("create table", "create unique index", "create index"):
        for m in re.finditer(kw, body):
            after = body[m.end():m.end() + 20]
            assert "if not exists" in after, f"{kw} 에 if not exists 가 없다"
    # 기존 데이터·기존 표는 건드리지 않는다 — 문장 첫 단어로 본다
    # ("on delete set null" 같은 절 안의 낱말은 문장이 아니다)
    heads = [st.split(None, 1)[0].lower() for st in _statements(sql)]
    for banned in ("drop", "delete", "insert", "update", "truncate", "grant", "revoke"):
        assert banned not in heads, f"기존 시스템에 손대는 문이 있다: {banned!r}"
    for other in ("alter table public.work_tasks", "alter table public.error_log",
                  "alter table public.jobs", "alter table public.meeting_tasks"):
        assert other not in body
    assert "create policy" in body and "duplicate_object" in body   # 정책도 재실행 안전


# ── 2·3. 표 구조 · 필수 컬럼 ───────────────────────────────────────────────

REQUIRED = {
    "id", "event_type", "dedupe_key", "severity", "recipient_type", "recipient_id",
    "title", "message", "status", "occurrences", "last_seen_at",
    "sent_at", "read_at", "resolved_at", "remind_at", "task_id",
    "source", "source_ref", "created_at", "updated_at",
}


def test_required_columns_exist(sql):
    cols = set(_column_names(_create_table_block(sql)))
    missing = REQUIRED - cols
    assert not missing, f"필수 컬럼 누락: {sorted(missing)}"


def test_primary_key_and_not_null_core(sql):
    block = _create_table_block(sql)
    assert re.search(r"^\s*id\s+bigint generated always as identity primary key", block, re.M)
    for col in ("event_type", "dedupe_key", "title", "status", "severity",
                "recipient_type", "recipient_id", "source"):
        assert re.search(rf"^\s*{col}\s+text not null", block, re.M), f"{col} 은 not null 이어야"
    assert re.search(r"^\s*occurrences\s+integer not null default 1", block, re.M)
    assert re.search(r"^\s*attempts\s+jsonb not null default", block, re.M)


# ── 4. status / severity / recipient_type 허용값 ──────────────────────────────

def _check_values(block: str, col: str) -> list[str]:
    m = re.search(rf"check \({col} in \((.*?)\)\)", block, re.S)
    assert m, f"{col} 의 check 제약이 없다"
    return re.findall(r"'([a-z]+)'", m.group(1))


def test_status_values_cover_lifecycle(sql):
    vals = _check_values(_create_table_block(sql), "status")
    assert vals == ["open", "sending", "sent", "read", "resolved", "muted", "expired"]
    # 기본값은 open — 만들어지면 곧바로 알림함에 보인다
    assert re.search(r"status\s+text not null default 'open'", _create_table_block(sql))


def test_severity_and_recipient_type_values(sql):
    block = _create_table_block(sql)
    assert _check_values(block, "severity") == ["critical", "high", "normal", "info"]
    assert _check_values(block, "recipient_type") == ["role", "person", "system"]
    assert re.search(r"recipient_id\s+text not null default 'owner'", block)


# ── 5. dedupe 인덱스 ────────────────────────────────────────────────────────

def test_dedupe_index_is_partial_unique_on_open_states(sql):
    body = _strip_comments(sql)
    m = re.search(r"create unique index if not exists notifications_open_key\s+"
                  r"on public\.notifications \(dedupe_key\)\s+"
                  r"where status in \((.*?)\);", body, re.S)
    assert m, "dedupe_key 부분 유니크 인덱스가 없다"
    states = re.findall(r"'([a-z]+)'", m.group(1))
    assert set(states) == {"open", "sending", "sent", "read"}
    for closed in ("resolved", "muted", "expired"):
        assert closed not in states, f"{closed} 가 유니크 범위에 있으면 재발을 새 행으로 못 만든다"


def test_dispatcher_and_inbox_indexes_exist(sql):
    body = _strip_comments(sql)
    assert "create index if not exists notifications_due_idx" in body
    assert re.search(r"notifications_due_idx\s+on public\.notifications \(status, remind_at\)", body)
    assert re.search(r"notifications_inbox_idx\s+on public\.notifications \(status, created_at desc\)", body)


# ── 6. task_id FK — work_tasks 와 타입이 맞는가 ────────────────────────────

def test_task_id_fk_matches_work_tasks_pk(sql):
    block = _create_table_block(sql)
    assert re.search(r"task_id\s+bigint references public\.work_tasks\(id\) on delete set null",
                     block), "task_id FK 가 없거나 on delete set null 이 아니다"
    # work_tasks.id 가 실제로 bigint identity PK 인지 원본 파일에서 확인
    wt = _strip_comments(WORK_TASKS_SQL.read_text(encoding="utf-8"))
    assert re.search(r"create table if not exists public\.work_tasks\s*\(\s*"
                     r"id\s+bigint generated always as identity primary key", wt, re.S)
    # 이 마이그레이션은 work_tasks 보다 뒤에 실행돼야 한다 — 번호로 보장
    assert SQL_PATH.name == "schema_v13.sql" and WORK_TASKS_SQL.name == "schema_v10.sql"


def test_rls_policy_like_other_tables(sql):
    body = _strip_comments(sql)
    assert "alter table public.notifications enable row level security;" in body
    assert re.search(r"create policy notifications_anon on public\.notifications\s+"
                     r"for all to anon using \(true\) with check \(true\);", body)
