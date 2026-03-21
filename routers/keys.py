"""
routers/keys.py — Encrypted key vault endpoints.

All responses return metadata only (name, provider, hint, env_var).
The actual key value is only returned via POST /api/keys/{id}/reveal,
which logs the access in key_audit_log.
"""

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import db, row_to_dict
from key_vault import encrypt_key, decrypt_key, key_hint

router = APIRouter(prefix="/api", tags=["keys"])


# ── Models ───────────────────────────────────────────────────────────────────

class KeyIn(BaseModel):
    name: str
    provider: str
    value: str
    env_var: Optional[str] = None
    notes: Optional[str] = None


class KeyUpdate(BaseModel):
    name: Optional[str] = None
    provider: Optional[str] = None
    value: Optional[str] = None
    env_var: Optional[str] = None
    notes: Optional[str] = None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _key_to_safe(row) -> dict:
    """Convert a key_store row to a safe dict (no encrypted_value)."""
    d = row_to_dict(row)
    d.pop("encrypted_value", None)
    return d


def _audit(conn, key_id: int, action: str, detail: str = None):
    conn.execute(
        "INSERT INTO key_audit_log (key_id, action, detail) VALUES (?, ?, ?)",
        (key_id, action, detail),
    )


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/keys")
def list_keys():
    """List all keys — metadata only, no values."""
    with db() as conn:
        rows = conn.execute(
            "SELECT id, name, provider, hint, env_var, notes, created_at, updated_at FROM key_store ORDER BY provider, name"
        ).fetchall()
    return [row_to_dict(r) for r in rows]


@router.get("/keys/{key_id}")
def get_key(key_id: int):
    """Get a single key's metadata."""
    with db() as conn:
        row = conn.execute(
            "SELECT id, name, provider, hint, env_var, notes, created_at, updated_at FROM key_store WHERE id = ?",
            (key_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Key not found")
    return row_to_dict(row)


@router.post("/keys", status_code=201)
def create_key(payload: KeyIn):
    """Store a new encrypted key."""
    encrypted = encrypt_key(payload.value)
    hint = key_hint(payload.value)
    with db() as conn:
        existing = conn.execute(
            "SELECT id FROM key_store WHERE name = ?", (payload.name,)
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="Key with this name already exists")
        cur = conn.execute(
            """INSERT INTO key_store (name, provider, encrypted_value, hint, env_var, notes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (payload.name, payload.provider, encrypted, hint, payload.env_var, payload.notes),
        )
        key_id = cur.lastrowid
        _audit(conn, key_id, "created", f"provider={payload.provider}")
    return {"id": key_id, "name": payload.name, "hint": hint}


@router.put("/keys/{key_id}")
def update_key(key_id: int, payload: KeyUpdate):
    """Update a key's metadata or value."""
    with db() as conn:
        row = conn.execute("SELECT * FROM key_store WHERE id = ?", (key_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Key not found")

        updates = {}
        if payload.name is not None:
            updates["name"] = payload.name
        if payload.provider is not None:
            updates["provider"] = payload.provider
        if payload.env_var is not None:
            updates["env_var"] = payload.env_var
        if payload.notes is not None:
            updates["notes"] = payload.notes
        if payload.value is not None:
            updates["encrypted_value"] = encrypt_key(payload.value)
            updates["hint"] = key_hint(payload.value)

        if not updates:
            return {"status": "no changes"}

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [key_id]
        conn.execute(
            f"UPDATE key_store SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
            values,
        )
        changed = list(updates.keys())
        if "encrypted_value" in changed:
            changed.remove("encrypted_value")
            changed.append("value")
        _audit(conn, key_id, "updated", f"fields: {', '.join(changed)}")
    return {"status": "updated", "id": key_id}


@router.delete("/keys/{key_id}", status_code=204)
def delete_key(key_id: int):
    """Delete a key."""
    with db() as conn:
        row = conn.execute("SELECT id, name FROM key_store WHERE id = ?", (key_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Key not found")
        _audit(conn, key_id, "deleted", f"name={row['name']}")
        conn.execute("DELETE FROM key_store WHERE id = ?", (key_id,))


@router.post("/keys/{key_id}/reveal")
def reveal_key(key_id: int):
    """Decrypt and return the key value. Logged in audit trail."""
    with db() as conn:
        row = conn.execute(
            "SELECT id, name, encrypted_value FROM key_store WHERE id = ?",
            (key_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Key not found")
        try:
            value = decrypt_key(row["encrypted_value"])
        except ValueError as e:
            raise HTTPException(status_code=500, detail=str(e))
        _audit(conn, key_id, "revealed")
    return {"id": key_id, "name": row["name"], "value": value}


@router.get("/keys/{key_id}/audit")
def key_audit(key_id: int):
    """Get audit log for a specific key."""
    with db() as conn:
        row = conn.execute("SELECT id FROM key_store WHERE id = ?", (key_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Key not found")
        rows = conn.execute(
            "SELECT * FROM key_audit_log WHERE key_id = ? ORDER BY performed_at DESC LIMIT 50",
            (key_id,),
        ).fetchall()
    return [row_to_dict(r) for r in rows]
