"""
tests/test_db.py — Unit tests for db.py: init, migrations, context manager.
"""

import sqlite3
import pytest

from db import init_db, db, row_to_dict


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
    with db() as conn:
        conn.execute(
            "INSERT INTO projects (name, slug, status) VALUES (?,?,?)",
            ("First", "unique-slug", "research"),
        )
    with pytest.raises(sqlite3.IntegrityError):
        with db() as conn:
            conn.execute(
                "INSERT INTO projects (name, slug, status) VALUES (?,?,?)",
                ("Dup", "unique-slug-2", "research"),
            )
            conn.execute(
                "INSERT INTO projects (name, slug, status) VALUES (?,?,?)",
                ("Dup2", "unique-slug-2", "research"),  # duplicate — should fail
            )
    # The second INSERT should have been rolled back — unique-slug-2 should not exist
    with db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) as c FROM projects WHERE slug = 'unique-slug-2'"
        ).fetchone()["c"]
    assert count == 0


def test_row_to_dict():
    init_db()
    with db() as conn:
        conn.execute(
            "INSERT INTO projects (name, slug, status) VALUES (?,?,?)",
            ("Dict Test", "dict-test", "research"),
        )
        row = conn.execute(
            "SELECT * FROM projects WHERE slug = 'dict-test'"
        ).fetchone()
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
