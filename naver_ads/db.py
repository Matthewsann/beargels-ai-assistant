"""SQLite 저장소 — data/ads.db (SPEC 4). 모든 적재는 UPSERT 라 재실행해도 같다."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DB_PATH = DATA_DIR / "ads.db"
RAW_DIR = DATA_DIR / "raw"

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id            TEXT PRIMARY KEY,
    name          TEXT,
    campaign_type TEXT,
    status        TEXT,
    daily_budget  INTEGER,
    reg_tm        TEXT,
    fetched_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS adgroups (
    id           TEXT PRIMARY KEY,
    campaign_id  TEXT NOT NULL,
    name         TEXT,
    bid_amt      INTEGER,
    status       TEXT,
    daily_budget INTEGER,
    reg_tm       TEXT,
    fetched_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ads (
    id             TEXT PRIMARY KEY,
    adgroup_id     TEXT NOT NULL,
    status         TEXT,
    inspect_status TEXT,
    fetched_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_stats (
    stat_date   TEXT NOT NULL,
    entity_type TEXT NOT NULL,      -- campaign | adgroup | ad
    entity_id   TEXT NOT NULL,
    imp_cnt     INTEGER,
    clk_cnt     INTEGER,
    cost        INTEGER,            -- salesAmt(원)
    ctr         REAL,
    cpc         REAL,
    avg_rnk     REAL,
    ccnt        INTEGER,            -- 전환수(참고)
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (stat_date, entity_type, entity_id)
);
CREATE TABLE IF NOT EXISTS bizmoney (
    snap_date  TEXT PRIMARY KEY,
    balance    INTEGER,
    fetched_at TEXT NOT NULL
);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def upsert(con: sqlite3.Connection, table: str, rows: list[dict], key: tuple[str, ...]) -> int:
    """rows 를 key 기준으로 INSERT … ON CONFLICT DO UPDATE. 넣은 행 수를 돌려준다."""
    if not rows:
        return 0
    cols = list(rows[0].keys())
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in key)
    sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
           f"ON CONFLICT({', '.join(key)}) DO UPDATE SET {updates}")
    con.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
    return len(rows)


def save_raw(name: str, payload, day: str | None = None) -> Path:
    """API 원본 JSON 을 data/raw/YYYY-MM-DD/<name>.json 에 보존(재파싱 대비)."""
    day = day or datetime.now().strftime("%Y-%m-%d")
    out = RAW_DIR / day / f"{name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return out
