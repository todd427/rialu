"""
mcp_server.py — FastMCP server for Rialú.

Exposes key vault and project data to Claude via MCP.
Mount point: /mcp (appended in main.py)

Auth: Bearer token via RIALU_MCP_KEY env var.
      Claude.ai registers this as a custom MCP connector with:
        URL: https://rialu.ie/mcp
        Header: Authorization: Bearer <RIALU_MCP_KEY>

Tools:
  vault_status    — is vault initialised, how many keys
  list_keys       — metadata only (name, provider, hint, env_var)
  reveal_key      — decrypt by name, audited
  store_key       — create a new encrypted key
  update_key      — rotate a key value by name
  list_projects   — all projects with status
"""

import os
from typing import Optional

from fastmcp import FastMCP

from db import db, row_to_dict
from key_vault import encrypt_key, decrypt_key, key_hint

mcp = FastMCP("rialu")

_MCP_KEY = os.environ.get("RIALU_MCP_KEY", "")


def _check_auth(token: str | None):
    """Raise if token doesn't match RIALU_MCP_KEY."""
    if not _MCP_KEY:
        raise PermissionError("RIALU_MCP_KEY not configured on server")
    if not token or token != _MCP_KEY:
        raise PermissionError("Invalid MCP key")


# ── Vault ────────────────────────────────────────────────────────────────────

@mcp.tool()
def vault_status() -> dict:
    """Check whether the Rialú key vault is initialised and how many keys are stored."""
    initialized = bool(os.environ.get("RIALU_VAULT_KEY", ""))
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM key_store").fetchone()[0]
    return {"initialized": initialized, "key_count": count}


@mcp.tool()
def list_keys() -> list[dict]:
    """
    List all stored keys — metadata only.
    Returns name, provider, hint (last 4 chars of value), env_var, notes.
    """
    with db() as conn:
        rows = conn.execute(
            "SELECT id, name, provider, hint, env_var, notes, created_at, updated_at "
            "FROM key_store ORDER BY provider, name"
        ).fetchall()
    return [row_to_dict(r) for r in rows]


@mcp.tool()
def reveal_key(name: str) -> dict:
    """
    Decrypt and return the value of a key by name. Access is logged in the audit trail.

    Args:
        name: Exact key name as stored (case-sensitive).
    """
    with db() as conn:
        row = conn.execute(
            "SELECT id, name, provider, encrypted_value, env_var FROM key_store WHERE name = ?",
            (name,),
        ).fetchone()
        if not row:
            raise ValueError(f"Key '{name}' not found")
        try:
            value = decrypt_key(row["encrypted_value"])
        except ValueError as e:
            raise ValueError(f"Decryption failed: {e}")
        conn.execute(
            "INSERT INTO key_audit_log (key_id, action, detail) VALUES (?, 'revealed', 'via MCP')",
            (row["id"],),
        )
    return {
        "id": row["id"],
        "name": row["name"],
        "provider": row["provider"],
        "env_var": row["env_var"],
        "value": value,
    }


@mcp.tool()
def store_key(
    name: str,
    provider: str,
    value: str,
    env_var: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """
    Store a new encrypted key in the Rialú vault.

    Args:
        name:     Unique key name, e.g. 'OPENAI_API_KEY'
        provider: Service name, e.g. 'OpenAI', 'Anthropic', 'Fly.io'
        value:    The secret value to encrypt and store
        env_var:  Optional env var name this key maps to
        notes:    Optional notes
    """
    encrypted = encrypt_key(value)
    hint = key_hint(value)
    with db() as conn:
        existing = conn.execute(
            "SELECT id FROM key_store WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            raise ValueError(f"Key '{name}' already exists — use update_key to rotate")
        cur = conn.execute(
            "INSERT INTO key_store (name, provider, encrypted_value, hint, env_var, notes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, provider, encrypted, hint, env_var, notes),
        )
        key_id = cur.lastrowid
        conn.execute(
            "INSERT INTO key_audit_log (key_id, action, detail) VALUES (?, 'created', 'via MCP')",
            (key_id,),
        )
    return {"id": key_id, "name": name, "provider": provider, "hint": hint}


@mcp.tool()
def update_key(name: str, value: str) -> dict:
    """
    Rotate the value of an existing key by name.

    Args:
        name:  Exact key name (case-sensitive)
        value: New secret value
    """
    encrypted = encrypt_key(value)
    hint = key_hint(value)
    with db() as conn:
        row = conn.execute(
            "SELECT id FROM key_store WHERE name = ?", (name,)
        ).fetchone()
        if not row:
            raise ValueError(f"Key '{name}' not found")
        conn.execute(
            "UPDATE key_store SET encrypted_value = ?, hint = ?, updated_at = datetime('now') WHERE id = ?",
            (encrypted, hint, row["id"]),
        )
        conn.execute(
            "INSERT INTO key_audit_log (key_id, action, detail) VALUES (?, 'updated', 'value rotated via MCP')",
            (row["id"],),
        )
    return {"id": row["id"], "name": name, "hint": hint, "status": "updated"}


# ── Projects ─────────────────────────────────────────────────────────────────

@mcp.tool()
def list_projects() -> list[dict]:
    """List all Rialú projects with name, status, phase, and platform."""
    with db() as conn:
        rows = conn.execute(
            "SELECT id, name, slug, phase, status, platform, repo_url, machine, notes, updated_at "
            "FROM projects ORDER BY updated_at DESC"
        ).fetchall()
    return [row_to_dict(r) for r in rows]


# ── ASGI app ─────────────────────────────────────────────────────────────────

def get_asgi_app():
    """Return the FastMCP ASGI app for mounting in main.py."""
    return mcp.http_app(path="/")
