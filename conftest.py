"""
conftest.py — Shared test fixtures.

Each test module sets os.environ["RIALU_DB"] to a temp file at import time.
This conftest provides a session-scoped teardown for any leftover temp files,
and ensures APScheduler doesn't start during tests.
"""

import os
import pytest


def pytest_configure(config):
    """Prevent APScheduler from starting during tests."""
    os.environ.setdefault("RIALU_TEST", "1")


@pytest.fixture(autouse=True)
def no_scheduler(monkeypatch):
    """Stub out the scheduler so it never starts during tests."""
    from unittest.mock import MagicMock
    import poller
    monkeypatch.setattr(poller, "scheduler", MagicMock())
