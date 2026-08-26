"""
SQLite connection + schema, matching the pattern verified in
HKUDS/AI-Trader's service/server/database.py:get_db_connection()
(WAL journal mode + busy_timeout, one connection per call -- this is
fine here because writes only come from the single worker process,
never from the API process, so there's no cross-process write
contention to pool against).
"""

import os
import sqlite3

_DB_PATH = os.getenv("ARES_CONTROL_DB_PATH", os.path.join(os.path.dirname(__file__), "ares_control.db"))


def get_db_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_database() -> None:
    conn = get_db_connection()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS toggle_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                unit_name TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('start', 'stop')),
                requested_by TEXT NOT NULL,
                requested_at TEXT NOT NULL DEFAULT (datetime('now')),
                approved INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'processing', 'done', 'failed')),
                error TEXT,
                processed_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daemon_status_cache (
                unit_name TEXT PRIMARY KEY,
                active_state TEXT NOT NULL,
                sub_state TEXT NOT NULL,
                memory_bytes INTEGER,
                tasks_current INTEGER,
                n_restarts INTEGER,
                cpu_usage_ns INTEGER,
                main_pid TEXT,
                active_enter_timestamp TEXT,
                checked_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        # Backfill columns for DBs created before this schema existed.
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(daemon_status_cache)")}
        for col, coltype in [
            ("memory_bytes", "INTEGER"), ("tasks_current", "INTEGER"),
            ("n_restarts", "INTEGER"), ("cpu_usage_ns", "INTEGER"),
            ("main_pid", "TEXT"), ("active_enter_timestamp", "TEXT"),
        ]:
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE daemon_status_cache ADD COLUMN {col} {coltype}")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS health_endpoint_cache (
                name TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                http_code INTEGER,
                response_time_ms REAL,
                ok INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                checked_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.commit()
    finally:
        conn.close()
