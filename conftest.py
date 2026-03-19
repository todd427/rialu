"""
conftest.py — Shared test fixtures.

Provides:
  - fresh_db: autouse, gives every test function its own isolated SQLite file
              via pytest's tmp_path. Sets RIALU_DB env var dynamically so
              db._db_path() picks it up on each get_connection() call.
  - no_scheduler: autouse, stubs out APScheduler so tests never start it.
"""

import os
import pytest


def pytest_configure(config):
    os.environ.setdefault("RIALU_TEST", "1")


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    """Each test gets a clean, isolated SQLite database."""
    db_file = str(tmp_path / "rialu_test.db")
    os.environ["RIALU_DB"] = db_file
    yield db_file
    # tmp_path cleanup is handled by pytest


@pytest.fixture(autouse=True)
def no_scheduler(monkeypatch):
    """Prevent APScheduler from starting during tests."""
    from unittest.mock import MagicMock
    import poller
    monkeypatch.setattr(poller, "scheduler", MagicMock())
