"""
routers/logins.py — Credential store for email/password logins.

Same AES-256-GCM encryption as the key vault, with audit logging.
"""

from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import db, row_to_dict
from key_vault import encrypt_key, decrypt_key, key_hint

router = APIRouter(prefix="/api/logins", tags=["logins"])


# ── models ───────────────────────────────────────────────────────────────────

class LoginIn(BaseModel):
    name: str
    url: Optional[str] = None
    username: Optional[str] = ""
    password: Optional[str] = ""
    auth_method: str = "password"  # password, google-oauth, github-oauth, huggingface, passkey, other
    totp_secret: Optional[str] = None
    notes: Optional[str] = None


class LoginUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    auth_method: Optional[str] = None
    totp_secret: Optional[str] = None
    notes: Optional[str] = None


# ── CRUD ─────────────────────────────────────────────────────────────────────

@router.get("")
def list_logins():
    """List all credentials — passwords masked."""
    with db() as conn:
        rows = conn.execute(
            "SELECT id, name, url, username, pwd_hint, auth_method, notes, created_at, updated_at "
            "FROM credential_store ORDER BY name"
        ).fetchall()
    return [row_to_dict(r) for r in rows]


@router.post("", status_code=201)
def create_login(l: LoginIn):
    """Store a new credential."""
    encrypted = encrypt_key(l.password) if l.password else ""
    hint = key_hint(l.password) if l.password else "—"
    enc_totp = encrypt_key(l.totp_secret) if l.totp_secret else None
    with db() as conn:
        existing = conn.execute(
            "SELECT id FROM credential_store WHERE name = ?", (l.name,)
        ).fetchone()
        if existing:
            raise HTTPException(409, f"Login '{l.name}' already exists — use PUT to update")
        cur = conn.execute(
            """INSERT INTO credential_store (name, url, username, encrypted_pwd, pwd_hint, auth_method, totp_secret, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (l.name, l.url, l.username, encrypted, hint, l.auth_method, enc_totp, l.notes),
        )
        cid = cur.lastrowid
        conn.execute(
            "INSERT INTO credential_audit_log (credential_id, action, detail) VALUES (?, 'created', 'via API')",
            (cid,),
        )
        row = conn.execute("SELECT id, name, url, username, pwd_hint, auth_method, notes, created_at, updated_at FROM credential_store WHERE id = ?", (cid,)).fetchone()
    return row_to_dict(row)


@router.post("/{login_id}/reveal")
def reveal_login(login_id: int):
    """Decrypt and return the password. Audited."""
    with db() as conn:
        row = conn.execute(
            "SELECT id, name, url, username, encrypted_pwd, totp_secret, notes FROM credential_store WHERE id = ?",
            (login_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Login not found")
        try:
            password = decrypt_key(row["encrypted_pwd"])
        except ValueError as e:
            raise HTTPException(500, f"Decryption failed: {e}")
        totp = None
        if row["totp_secret"]:
            try:
                totp = decrypt_key(row["totp_secret"])
            except ValueError:
                totp = None
        conn.execute(
            "INSERT INTO credential_audit_log (credential_id, action, detail) VALUES (?, 'revealed', 'via API')",
            (row["id"],),
        )
    return {
        "id": row["id"],
        "name": row["name"],
        "url": row["url"],
        "username": row["username"],
        "password": password,
        "totp_secret": totp,
        "notes": row["notes"],
    }


@router.put("/{login_id}")
def update_login(login_id: int, l: LoginUpdate):
    """Update a credential. Password re-encrypted if changed."""
    with db() as conn:
        existing = conn.execute(
            "SELECT id FROM credential_store WHERE id = ?", (login_id,)
        ).fetchone()
        if not existing:
            raise HTTPException(404, "Login not found")

        updates = []
        values = []
        if l.name is not None:
            updates.append("name = ?")
            values.append(l.name)
        if l.url is not None:
            updates.append("url = ?")
            values.append(l.url)
        if l.username is not None:
            updates.append("username = ?")
            values.append(l.username)
        if l.password is not None:
            updates.append("encrypted_pwd = ?")
            values.append(encrypt_key(l.password))
            updates.append("pwd_hint = ?")
            values.append(key_hint(l.password))
        if l.auth_method is not None:
            updates.append("auth_method = ?")
            values.append(l.auth_method)
        if l.totp_secret is not None:
            updates.append("totp_secret = ?")
            values.append(encrypt_key(l.totp_secret) if l.totp_secret else None)
        if l.notes is not None:
            updates.append("notes = ?")
            values.append(l.notes)

        if not updates:
            raise HTTPException(400, "No fields to update")

        updates.append("updated_at = datetime('now')")
        values.append(login_id)
        conn.execute(
            f"UPDATE credential_store SET {', '.join(updates)} WHERE id = ?",
            values,
        )
        detail = "updated fields: " + ", ".join(f.split(" =")[0] for f in updates if "datetime" not in f)
        conn.execute(
            "INSERT INTO credential_audit_log (credential_id, action, detail) VALUES (?, 'updated', ?)",
            (login_id, detail),
        )
        row = conn.execute(
            "SELECT id, name, url, username, pwd_hint, auth_method, notes, created_at, updated_at FROM credential_store WHERE id = ?",
            (login_id,),
        ).fetchone()
    return row_to_dict(row)


@router.delete("/{login_id}", status_code=204)
def delete_login(login_id: int):
    with db() as conn:
        conn.execute("DELETE FROM credential_store WHERE id = ?", (login_id,))
