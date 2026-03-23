"""
tests/test_cc_wrapper.py — Tests for CC stream-json parser.
"""

import json
import pytest
import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agent'))
from cc_wrapper import CCSession


@pytest.fixture
def session():
    return CCSession(
        agent_id="test-agent",
        project_id=1,
        rialu_base="http://localhost:9999",  # won't be called
    )


def test_session_defaults(session):
    assert session.agent_id == "test-agent"
    assert session.project_id == 1
    assert session.total_cost == 0.0
    assert session.session_id is None


def test_needs_approval_empty(session):
    assert session._needs_approval("bash") is False


def test_needs_approval_configured():
    s = CCSession(require_approval_for=["bash", "computer"])
    assert s._needs_approval("bash") is True
    assert s._needs_approval("read") is False


def test_sign_no_key():
    s = CCSession(agent_id="test")
    headers = s._sign(b"test body")
    assert headers == {}


def test_sign_with_key():
    s = CCSession(agent_id="test", agent_key="secret123")
    headers = s._sign(b"test body")
    assert "X-Rialu-Sig" in headers
    assert headers["X-Rialu-Sig"].startswith("sha256=")
    assert len(headers["X-Rialu-Sig"]) == 71  # "sha256=" + 64 hex chars


@pytest.mark.asyncio
async def test_handle_line_init(session):
    """Parse a system init line."""
    line = json.dumps({
        "type": "system",
        "subtype": "init",
        "session_id": "abc-123",
        "model": "claude-sonnet-4-6",
        "tools": ["Bash", "Read"],
    })
    # Patch _emit to capture calls
    emitted = []
    async def mock_emit(event_type, payload):
        emitted.append((event_type, payload))
    session._emit = mock_emit

    await session._handle_line(line)
    assert session.session_id == "abc-123"
    assert len(emitted) == 1
    assert emitted[0][0] == "cc_init"
    assert emitted[0][1]["model"] == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_handle_line_text(session):
    """Parse an assistant text message."""
    line = json.dumps({
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": "Hello world"}],
            "usage": {},
        },
    })
    emitted = []
    async def mock_emit(event_type, payload):
        emitted.append((event_type, payload))
    session._emit = mock_emit

    await session._handle_line(line)
    assert len(emitted) == 1
    assert emitted[0][0] == "cc_text"
    assert emitted[0][1]["text"] == "Hello world"


@pytest.mark.asyncio
async def test_handle_line_tool_use(session):
    """Parse a tool_use block."""
    line = json.dumps({
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}, "id": "tu_1"},
            ],
            "usage": {},
        },
    })
    emitted = []
    async def mock_emit(event_type, payload):
        emitted.append((event_type, payload))
    session._emit = mock_emit

    await session._handle_line(line)
    assert len(emitted) == 1
    assert emitted[0][0] == "cc_tool_call"
    assert emitted[0][1]["tool_name"] == "Bash"
    assert emitted[0][1]["tool_args"] == {"command": "ls"}


@pytest.mark.asyncio
async def test_handle_line_result(session):
    """Parse a result line with cost."""
    line = json.dumps({
        "type": "result",
        "subtype": "success",
        "total_cost_usd": 0.0542,
        "duration_ms": 3200,
        "num_turns": 2,
        "result": "Done.",
    })
    emitted = []
    async def mock_emit(event_type, payload):
        emitted.append((event_type, payload))
    session._emit = mock_emit

    await session._handle_line(line)
    assert session.total_cost == 0.0542
    assert len(emitted) == 1
    assert emitted[0][0] == "cc_cost_update"
    assert emitted[0][1]["total_cost_usd"] == 0.0542


@pytest.mark.asyncio
async def test_handle_line_invalid_json(session):
    """Invalid JSON is silently skipped."""
    emitted = []
    async def mock_emit(event_type, payload):
        emitted.append((event_type, payload))
    session._emit = mock_emit

    await session._handle_line("not json at all")
    assert len(emitted) == 0


@pytest.mark.asyncio
async def test_handle_line_empty_text(session):
    """Empty text blocks are not emitted."""
    line = json.dumps({
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": "   "}],
            "usage": {},
        },
    })
    emitted = []
    async def mock_emit(event_type, payload):
        emitted.append((event_type, payload))
    session._emit = mock_emit

    await session._handle_line(line)
    assert len(emitted) == 0


@pytest.mark.asyncio
async def test_handle_line_mixed_content(session):
    """Message with both text and tool_use emits both events."""
    line = json.dumps({
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Let me check"},
                {"type": "tool_use", "name": "Read", "input": {"file": "main.py"}, "id": "tu_2"},
            ],
            "usage": {},
        },
    })
    emitted = []
    async def mock_emit(event_type, payload):
        emitted.append((event_type, payload))
    session._emit = mock_emit

    await session._handle_line(line)
    assert len(emitted) == 2
    assert emitted[0][0] == "cc_text"
    assert emitted[1][0] == "cc_tool_call"
