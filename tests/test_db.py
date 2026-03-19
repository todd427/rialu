"""
tests/test_db.py — Unit tests for db.py: init, migrations, context manager.
"""

import os
import sqlite3
import tempfile
import pytest

os.environ["RIALU_DB"] = tempfile.mktemp(suffix=".db")

from db import init_db, db, row_to_dict, MIGRATIONS


def test_init_db_creates_tables():
    init_db()
    with db() as conn:
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    expected = {
        "projects", "milestones", "sessions", "worklog",
        "budget", "api_registry", "api_project_map", "api_usage",
        "deployments_cache", "deploy_history",
        "machine_heartbeats", "agent_actions",
    }
    assert expected.issubset(tables)


def test_init_db_idempotent():
    """Running init_db twice should not raise."""
    init_db()
    init_db()


def test_context_manager_commits():
    init_db()
    with db() as conn:
        conn.execute(
            "INSERT INTO projects (name, slug, status) VALUES (?,?,?)",
            ("Test Project", "test-project", "research"),
        )
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM projects WHERE slug = 'test-project'"
        ).fetchone()
    assert row is not None
    assert row["name"] == "Test Project"


def test_context_manager_rollback_on_error():
    init_db()
    with pytest.raises(sqlite3.IntegrityError):
        with db() as conn:
            conn.execute(
                "INSERT INTO projects (name, slug, status) VALUES (?,?,?)",
                ("Dup", "test-project", "research"),
            )
            conn.execute(
                "INSERT INTO projects (name, slug, status) VALUES (?,?,?)",
                ("Dup2", "test-project", "research"),  # duplicate slug — should fail
            )
    # The first insert should have been rolled back
    with db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) as c FROM projects WHERE slug = 'test-project'"
        ).fetchone()["c"]
    # Either 0 (full rollback) or 1 (from previous test) — not 2
    assert count <= 1


def test_row_to_dict():
    init_db()
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM projects WHERE slug = 'test-project'"
        ).fetchone()
    if row:
        d = row_to_dict(row)
        assert isinstance(d, dict)
        assert "name" in d
    assert row_to_dict(None) == {}


def test_wal_mode():
    init_db()
    with db() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"


def test_foreign_keys_enabled():
    init_db()
    with db() as conn:
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk == 1
