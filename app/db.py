"""SQLite storage. One file at data/dashboard.db; plain sqlite3, no ORM."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("DASHBOARD_DB", ROOT / "data" / "dashboard.db"))  # override to test on a copy
SCHEMA_VERSION = 3

PLAN_DAYS_SQL = """
CREATE TABLE IF NOT EXISTS plan_days (
    week_start     TEXT NOT NULL REFERENCES plan_weeks(week_start),
    dow            INTEGER NOT NULL,         -- 0 = Mon .. 6 = Sun
    kind           TEXT NOT NULL DEFAULT 'rest',
    distance_km    REAL NOT NULL DEFAULT 0,
    note           TEXT NOT NULL DEFAULT '',
    strength_focus TEXT NOT NULL DEFAULT '', -- planned gym session: upper | lower | full | ''
    PRIMARY KEY (week_start, dow)
)
"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS races (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    race_date   TEXT NOT NULL,
    goal        TEXT,                          -- unused (v1)
    distance_km REAL,
    priority    TEXT NOT NULL DEFAULT 'B',     -- A = the goal race the plan builds to; B/C = tune-ups
    goal_time   TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS plan_weeks (
    week_start TEXT PRIMARY KEY,          -- Monday, ISO date
    week_no    INTEGER NOT NULL,
    phase      TEXT NOT NULL DEFAULT '',
    notes      TEXT NOT NULL DEFAULT ''
);

{PLAN_DAYS_SQL};

CREATE TABLE IF NOT EXISTS activities (
    id                INTEGER PRIMARY KEY,       -- Garmin activityId; negative = added manually
    start_local       TEXT NOT NULL,             -- 'YYYY-MM-DD HH:MM:SS' local time
    day               TEXT NOT NULL,             -- 'YYYY-MM-DD' as recorded
    type_key          TEXT NOT NULL,
    name              TEXT NOT NULL DEFAULT '',
    distance_m        REAL NOT NULL DEFAULT 0,
    duration_s        REAL NOT NULL DEFAULT 0,
    avg_hr            REAL,
    category          TEXT NOT NULL,             -- automatic: run | strength | extra
    category_override TEXT,                      -- manual, survives refreshes
    strength_focus    TEXT,                      -- manual: upper | lower | full
    raw_json          TEXT NOT NULL,
    source            TEXT NOT NULL DEFAULT 'garmin',   -- garmin | manual
    day_override      TEXT                       -- manual: count it on a different day
);
CREATE INDEX IF NOT EXISTS idx_activities_day ON activities(day);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def blank_days(conn: sqlite3.Connection, week_start: str) -> None:
    for dow in range(7):
        conn.execute(
            "INSERT OR IGNORE INTO plan_days(week_start, dow) VALUES(?, ?)", (week_start, dow)
        )


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def _backup(conn: sqlite3.Connection, name: str) -> None:
    with sqlite3.connect(DB_PATH.with_name(name)) as dest:
        conn.backup(dest)


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """v1 stored free-text miles workouts (filler data). Back up, then start the daily plan blank in km."""
    if "text" not in _columns(conn, "plan_days"):
        return  # fresh database, already in the new shape
    _backup(conn, "dashboard.pre-v2-backup.db")
    conn.execute("DROP TABLE plan_days")
    conn.execute(PLAN_DAYS_SQL)
    conn.execute("UPDATE plan_weeks SET notes = ''")
    for row in conn.execute("SELECT week_start FROM plan_weeks").fetchall():
        blank_days(conn, row["week_start"])


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    """v3: race details (distance/priority/goal), manual activities and per-activity day override. Additive only."""
    race_cols, act_cols = _columns(conn, "races"), _columns(conn, "activities")
    if "priority" in race_cols and "source" in act_cols:
        return  # fresh database or already migrated
    _backup(conn, "dashboard.pre-v3-backup.db")

    fresh_races = "priority" not in race_cols
    for column, ddl in (
        ("distance_km", "REAL"),
        ("priority", "TEXT NOT NULL DEFAULT 'B'"),
        ("goal_time", "TEXT NOT NULL DEFAULT ''"),
        ("notes", "TEXT NOT NULL DEFAULT ''"),
    ):
        if column not in race_cols:
            conn.execute(f"ALTER TABLE races ADD COLUMN {column} {ddl}")
    if fresh_races:
        # Existing races came from the spreadsheet: the latest is the goal race, the rest are tune-ups.
        conn.execute("UPDATE races SET priority = 'A' WHERE race_date = (SELECT MAX(race_date) FROM races)")
        conn.execute("UPDATE races SET distance_km = 21.0975 WHERE lower(name) LIKE '%half%'")
        conn.execute(
            "UPDATE races SET distance_km = 42.195 WHERE distance_km IS NULL AND lower(name) LIKE '%marathon%'"
        )

    if "source" not in act_cols:
        conn.execute("ALTER TABLE activities ADD COLUMN source TEXT NOT NULL DEFAULT 'garmin'")
    if "day_override" not in act_cols:
        conn.execute("ALTER TABLE activities ADD COLUMN day_override TEXT")


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 2:
            _migrate_to_v2(conn)
        if version < 3:
            _migrate_to_v3(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
