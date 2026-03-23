"""
db.py — SQLite initialisation, migrations, and connection helper.
Database file lives at /data/rialu.db in production (Fly.io volume),
or ./rialu.db locally.

DB_PATH is resolved dynamically on every get_connection() call so that
tests can override RIALU_DB per-test via the environment.
"""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def _db_path() -> str:
    if os.environ.get("FLY_APP_NAME"):
        return "/data/rialu.db"
    return os.environ.get("RIALU_DB", str(Path(__file__).parent / "rialu.db"))


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


MIGRATIONS = [
    # 001 — core tables
    """
    CREATE TABLE IF NOT EXISTS projects (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        slug        TEXT NOT NULL UNIQUE,
        phase       TEXT,
        status      TEXT NOT NULL DEFAULT 'development',
        notes       TEXT,
        repo_url    TEXT,
        machine     TEXT,
        platform    TEXT,
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS milestones (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        title       TEXT NOT NULL,
        due_date    TEXT,
        done        INTEGER NOT NULL DEFAULT 0,
        sort_order  INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        session_type    TEXT NOT NULL DEFAULT 'code',
        notes           TEXT,
        started_at      TEXT NOT NULL DEFAULT (datetime('now')),
        ended_at        TEXT,
        duration_minutes INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS worklog (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        date            TEXT NOT NULL DEFAULT (date('now')),
        minutes         INTEGER NOT NULL,
        session_type    TEXT NOT NULL DEFAULT 'code',
        notes           TEXT,
        created_at      TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS budget (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        platform        TEXT NOT NULL,
        service_name    TEXT NOT NULL,
        cost_gbp        REAL NOT NULL DEFAULT 0,
        period          TEXT NOT NULL DEFAULT 'monthly',
        active          INTEGER NOT NULL DEFAULT 1,
        notes           TEXT,
        created_at      TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS api_registry (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        name                TEXT NOT NULL,
        provider            TEXT NOT NULL,
        auth_key_ref        TEXT,
        billing_model       TEXT NOT NULL DEFAULT 'per_token',
        cost_unit           TEXT,
        cost_per_unit_gbp   REAL DEFAULT 0,
        billing_url         TEXT,
        usage_api_endpoint  TEXT,
        notes               TEXT,
        active              INTEGER NOT NULL DEFAULT 1,
        created_at          TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS api_project_map (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        api_id              INTEGER NOT NULL REFERENCES api_registry(id) ON DELETE CASCADE,
        project_id          INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        usage_description   TEXT,
        is_primary          INTEGER NOT NULL DEFAULT 0,
        UNIQUE(api_id, project_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS api_usage (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        api_id          INTEGER NOT NULL REFERENCES api_registry(id) ON DELETE CASCADE,
        project_id      INTEGER REFERENCES projects(id) ON DELETE SET NULL,
        period_start    TEXT,
        period_end      TEXT,
        tokens_in       INTEGER,
        tokens_out      INTEGER,
        call_count      INTEGER,
        cost_gbp        REAL,
        source          TEXT NOT NULL DEFAULT 'manual',
        raw_response    TEXT,
        recorded_at     TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS deployments_cache (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        platform            TEXT NOT NULL,
        service_name        TEXT NOT NULL UNIQUE,
        status              TEXT NOT NULL DEFAULT 'unknown',
        last_deploy_at      TEXT,
        url                 TEXT,
        last_commit_hash    TEXT,
        last_commit_message TEXT,
        deploy_duration_s   INTEGER,
        checked_at          TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS deploy_history (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        service_name        TEXT NOT NULL,
        platform            TEXT NOT NULL,
        result              TEXT NOT NULL DEFAULT 'unknown',
        commit_hash         TEXT,
        commit_message      TEXT,
        duration_s          INTEGER,
        deployed_at         TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS machine_heartbeats (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        machine_name    TEXT NOT NULL,
        cpu_pct         REAL,
        ram_pct         REAL,
        gpu_pct         REAL,
        processes_json  TEXT,
        repos_json      TEXT,
        received_at     TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_actions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        machine_name    TEXT NOT NULL,
        action_type     TEXT NOT NULL,
        payload         TEXT,
        status          TEXT NOT NULL DEFAULT 'pending',
        result          TEXT,
        created_at      TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    # 002 — terminal sessions
    """
    CREATE TABLE IF NOT EXISTS terminal_sessions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        machine_name    TEXT NOT NULL,
        channel_id      TEXT NOT NULL UNIQUE,
        pane_id         TEXT,
        opened_at       TEXT NOT NULL DEFAULT (datetime('now')),
        closed_at       TEXT
    )
    """,
    # 003 — indexes
    "CREATE INDEX IF NOT EXISTS idx_worklog_date ON worklog(date)",
    "CREATE INDEX IF NOT EXISTS idx_worklog_project ON worklog(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_milestones_project ON milestones(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_heartbeats_machine ON machine_heartbeats(machine_name, received_at)",
    # 004 — worklog auto-git lookup
    "CREATE INDEX IF NOT EXISTS idx_worklog_autogit ON worklog(project_id, date, session_type)",
    # 005 — key vault
    """
    CREATE TABLE IF NOT EXISTS key_store (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        name            TEXT NOT NULL UNIQUE,
        provider        TEXT NOT NULL,
        encrypted_value TEXT NOT NULL,
        hint            TEXT NOT NULL DEFAULT '••••',
        env_var         TEXT,
        notes           TEXT,
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS key_audit_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        key_id          INTEGER NOT NULL REFERENCES key_store(id) ON DELETE CASCADE,
        action          TEXT NOT NULL,
        detail          TEXT,
        performed_at    TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_key_audit ON key_audit_log(key_id, performed_at)",
    # 007 — milestone review log
    """
    CREATE TABLE IF NOT EXISTS milestone_review_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        milestone_id    INTEGER NOT NULL REFERENCES milestones(id) ON DELETE CASCADE,
        project_name    TEXT NOT NULL,
        milestone_title TEXT NOT NULL,
        action          TEXT NOT NULL,
        evidence        TEXT,
        reviewed_at     TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_milestone_review ON milestone_review_log(reviewed_at)",
    # 008 — lines of code in worklog
    "ALTER TABLE worklog ADD COLUMN lines_added INTEGER DEFAULT 0",
    "ALTER TABLE worklog ADD COLUMN lines_removed INTEGER DEFAULT 0",
    # 006 — anthropic token usage (from CSV export)
    """
    CREATE TABLE IF NOT EXISTS anthropic_usage (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        usage_date          TEXT NOT NULL,
        model               TEXT NOT NULL,
        api_key_name        TEXT,
        input_tokens        INTEGER NOT NULL DEFAULT 0,
        cache_write_5m      INTEGER NOT NULL DEFAULT 0,
        cache_write_1h      INTEGER NOT NULL DEFAULT 0,
        cache_read          INTEGER NOT NULL DEFAULT 0,
        output_tokens       INTEGER NOT NULL DEFAULT 0,
        web_searches        INTEGER NOT NULL DEFAULT 0,
        cost_usd            REAL NOT NULL DEFAULT 0,
        imported_at         TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(usage_date, model, api_key_name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_anthropic_usage_date ON anthropic_usage(usage_date)",
    # 009 — project site URL
    "ALTER TABLE projects ADD COLUMN site_url TEXT",
    # 010 — agents table (Faire Phase 1)
    """
    CREATE TABLE IF NOT EXISTS agents (
        id          TEXT PRIMARY KEY,
        machine     TEXT NOT NULL,
        name        TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'offline',
        last_seen   TEXT,
        config      TEXT,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    # 011 — decisions queue (Faire Phase 1)
    """
    CREATE TABLE IF NOT EXISTS decisions (
        id            TEXT PRIMARY KEY,
        project_id    INTEGER NOT NULL REFERENCES projects(id),
        trigger_type  TEXT NOT NULL,
        priority      INTEGER DEFAULT 5,
        status        TEXT DEFAULT 'pending',
        payload       TEXT NOT NULL,
        context_refs  TEXT,
        agent_id      TEXT REFERENCES agents(id),
        timeout_secs  INTEGER DEFAULT 300,
        defer_until   TEXT,
        response      TEXT,
        responded_by  TEXT,
        created_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
        resolved_at   TEXT
    )
    """,
    # 012 — agent event stream (Faire Phase 1)
    """
    CREATE TABLE IF NOT EXISTS agent_events (
        id          TEXT PRIMARY KEY,
        agent_id    TEXT NOT NULL REFERENCES agents(id),
        project_id  INTEGER REFERENCES projects(id),
        event_type  TEXT NOT NULL,
        payload     TEXT,
        created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )
    """,
    # 013 — extend projects for Faire
    "ALTER TABLE projects ADD COLUMN cc_session_id TEXT",
    "ALTER TABLE projects ADD COLUMN cost_limit_hr REAL DEFAULT 1.0",
    "ALTER TABLE projects ADD COLUMN auto_approve_rules TEXT",
    # 014 — Faire indexes
    "CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_project ON decisions(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_events_agent ON agent_events(agent_id, created_at)",
]


def init_db() -> None:
    """Run all migrations idempotently. Safe to call on every startup."""
    with db() as conn:
        for sql in MIGRATIONS:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise


def row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row) if row else {}
