"""
mcp_server.py — MCP server for Rialú.

Exposes key vault and project data to Claude via MCP.
Mount point: /mcp (mounted in main.py)

Auth: OAuth 2.1 with PKCE + Dynamic Client Registration (DCR).
      Same pattern as Mnemos — auto-approves authorization for a personal server.
      State persisted to /data/oauth_state.json (Fly.io volume).

Claude.ai connector URL: https://rialu.ie/mcp

Tools:
  vault_status    — is vault initialised, how many keys
  list_keys       — metadata only (name, provider, hint, env_var)
  reveal_key      — decrypt by name, audited
  store_key       — create a new encrypted key
  update_key      — rotate a key value by name
  generate_key    — get-or-create a crypto-random key
  list_projects   — all projects with status
  update_project  — patch status, platform, site_url, or any other project field by id
"""

import json
import os
import secrets as secrets_mod
import time
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlparse

from pydantic import AnyHttpUrl
from mcp.server.fastmcp import FastMCP
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.auth.provider import (
    OAuthAuthorizationServerProvider,
    AuthorizationParams,
    AuthorizationCode,
    AccessToken,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from db import db, row_to_dict
from key_vault import encrypt_key, decrypt_key, key_hint, generate_random_key


# ── Config ───────────────────────────────────────────────────────────────────

_ISSUER_URL = os.environ.get("RIALU_MCP_ISSUER", "https://rialu.fly.dev")
_OAUTH_STATE = os.environ.get("RIALU_OAUTH_STATE_PATH", "/data/oauth_state.json")
_SCOPE = "mcp"
_TOKEN_LIFETIME = 30 * 24 * 3600  # 30 days
_CODE_LIFETIME = 600  # 10 minutes

ALLOWED_REDIRECT_DOMAINS = ["claude.ai", "localhost", "127.0.0.1"]


def _redirect_allowed(uri: str) -> bool:
    return any(domain in uri for domain in ALLOWED_REDIRECT_DOMAINS)


# ── OAuth Provider ───────────────────────────────────────────────────────────

class RialuOAuthProvider(OAuthAuthorizationServerProvider):
    """
    Personal OAuth 2.1 provider for Rialú MCP.
    Auto-approves all authorization from allowed redirect domains.
    State persisted to JSON file on Fly.io volume.
    """

    def __init__(self, state_file: str):
        self._path = Path(state_file)
        self._clients: dict = {}
        self._codes: dict = {}
        self._tokens: dict = {}
        self._refresh: dict = {}
        self._load()

    def _load(self) -> None:
        try:
            if self._path.exists():
                with self._path.open() as f:
                    state = json.load(f)
                self._clients = state.get("clients", {})
                self._codes = state.get("codes", {})
                self._tokens = state.get("tokens", {})
                self._refresh = state.get("refresh", {})
                print(f"[rialu-oauth] Loaded: {len(self._clients)} clients, {len(self._tokens)} tokens")
        except Exception as exc:
            print(f"[rialu-oauth] Could not load state ({exc}); starting fresh")

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            with tmp.open("w") as f:
                json.dump({
                    "clients": self._clients,
                    "codes": self._codes,
                    "tokens": self._tokens,
                    "refresh": self._refresh,
                }, f, indent=2)
            tmp.replace(self._path)
        except Exception as exc:
            print(f"[rialu-oauth] Could not save state: {exc}")

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self._clients.get(client_id)
        if not data:
            return None
        try:
            return OAuthClientInformationFull.model_validate(data)
        except Exception:
            return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info.model_dump(mode="json")
        self._save()
        print(f"[rialu-oauth] Registered client: {client_info.client_id}")

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        raw_redirect = getattr(params, "redirect_uri", None)
        redirect_uri = str(raw_redirect) if raw_redirect else ""
        if not redirect_uri:
            raise ValueError("redirect_uri is required")
        if not _redirect_allowed(redirect_uri):
            raise ValueError(f"redirect_uri not in allowlist: {redirect_uri}")

        scope_val = getattr(params, "scope", None) or getattr(params, "scopes", None) or _SCOPE
        challenge = getattr(params, "code_challenge", None)
        challenge_str = str(challenge) if challenge is not None else None
        method = getattr(params, "code_challenge_method", "S256")
        method_str = str(method) if method is not None else "S256"
        state_val = getattr(params, "state", None)
        state_str = str(state_val) if state_val is not None else None

        code = secrets_mod.token_urlsafe(32)
        self._codes[code] = {
            "code": code,
            "client_id": client.client_id,
            "redirect_uri": redirect_uri,
            "scope": scope_val,
            "code_challenge": challenge_str,
            "code_challenge_method": method_str,
            "expires_at": time.time() + _CODE_LIFETIME,
        }
        self._save()
        print(f"[rialu-oauth] Issued auth code for client {client.client_id}")
        return construct_redirect_uri(redirect_uri, code=code, state=state_str)

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        data = self._codes.get(authorization_code)
        if not data:
            return None
        if time.time() > data["expires_at"]:
            self._codes.pop(authorization_code, None)
            self._save()
            return None
        if data["client_id"] != client.client_id:
            return None
        scope_raw = data.get("scope", _SCOPE)
        scopes = scope_raw.split() if isinstance(scope_raw, str) else scope_raw
        return AuthorizationCode(
            code=data["code"],
            client_id=data["client_id"],
            redirect_uri=AnyHttpUrl(data["redirect_uri"]),
            redirect_uri_provided_explicitly=True,
            expires_at=data["expires_at"],
            scopes=scopes,
            code_challenge=data.get("code_challenge"),
            code_challenge_method=data.get("code_challenge_method", "S256"),
        )

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        self._codes.pop(authorization_code.code, None)
        access_token = secrets_mod.token_urlsafe(48)
        refresh_token = secrets_mod.token_urlsafe(48)
        scopes = authorization_code.scopes or [_SCOPE]
        expires_at = time.time() + _TOKEN_LIFETIME
        self._tokens[access_token] = {
            "token": access_token,
            "client_id": client.client_id,
            "scopes": scopes,
            "expires_at": expires_at,
        }
        self._refresh[refresh_token] = {
            "token": refresh_token,
            "client_id": client.client_id,
            "scopes": scopes,
            "access_token": access_token,
        }
        self._save()
        print(f"[rialu-oauth] Issued tokens for client {client.client_id}")
        return OAuthToken(
            access_token=access_token,
            token_type="bearer",
            expires_in=int(_TOKEN_LIFETIME),
            scope=" ".join(scopes),
            refresh_token=refresh_token,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        data = self._tokens.get(token)
        if not data:
            return None
        if time.time() > data["expires_at"]:
            return None
        return AccessToken(
            token=data["token"],
            client_id=data["client_id"],
            scopes=data.get("scopes", [_SCOPE]),
            expires_at=int(data["expires_at"]),
        )

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        data = self._refresh.get(refresh_token)
        if not data or data["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=data["token"],
            client_id=data["client_id"],
            scopes=data.get("scopes", [_SCOPE]),
        )

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        new_access = secrets_mod.token_urlsafe(48)
        expires_at = time.time() + _TOKEN_LIFETIME
        use_scopes = scopes or refresh_token.scopes or [_SCOPE]
        self._tokens[new_access] = {
            "token": new_access,
            "client_id": client.client_id,
            "scopes": use_scopes,
            "expires_at": expires_at,
        }
        if refresh_token.token in self._refresh:
            self._refresh[refresh_token.token]["access_token"] = new_access
        self._save()
        return OAuthToken(
            access_token=new_access,
            token_type="bearer",
            expires_in=int(_TOKEN_LIFETIME),
            scope=" ".join(use_scopes),
            refresh_token=refresh_token.token,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            self._tokens.pop(token.token, None)
        else:
            rt_data = self._refresh.pop(token.token, None)
            if rt_data:
                self._tokens.pop(rt_data.get("access_token", ""), None)
        self._save()


# ── Server ────────────────────────────────────────────────────────────────────

_oauth_provider = RialuOAuthProvider(state_file=_OAUTH_STATE)
_external_host = urlparse(_ISSUER_URL).hostname or "rialu.ie"

mcp = FastMCP(
    name="Rialú",
    host=_external_host,
    instructions=(
        "Rialú is Todd's personal DevOps command centre. Use these tools to "
        "manage the encrypted key vault (list, reveal, store, update, generate keys) and "
        "view project status across the portfolio."
    ),
    auth_server_provider=_oauth_provider,
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(_ISSUER_URL),
        resource_server_url=AnyHttpUrl(_ISSUER_URL),
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=["mcp"],
            default_scopes=["mcp"],
        ),
        required_scopes=["mcp"],
    ),
    stateless_http=True,
)


# ── Vault tools ──────────────────────────────────────────────────────────────

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
            return {"error": f"Key '{name}' not found"}
        try:
            value = decrypt_key(row["encrypted_value"])
        except ValueError as e:
            return {"error": f"Decryption failed: {e}"}
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
            return {"error": f"Key '{name}' already exists — use update_key to rotate"}
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
            return {"error": f"Key '{name}' not found"}
        conn.execute(
            "UPDATE key_store SET encrypted_value = ?, hint = ?, updated_at = datetime('now') WHERE id = ?",
            (encrypted, hint, row["id"]),
        )
        conn.execute(
            "INSERT INTO key_audit_log (key_id, action, detail) VALUES (?, 'updated', 'value rotated via MCP')",
            (row["id"],),
        )
    return {"id": row["id"], "name": name, "hint": hint, "status": "updated"}


@mcp.tool()
def generate_key(
    name: str,
    provider: str,
    length: int = 32,
    encoding: Literal["hex", "base64"] = "hex",
    env_var: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """
    Generate a cryptographically random key and store it — get-or-create semantics.

    If a key with this name already exists, returns the existing plaintext value
    (audited). If not, generates a new random key, encrypts and stores it, then
    returns the plaintext value.

    Response includes `created: bool` so callers can distinguish the two cases.
    The value is returned once — store it appropriately.

    Args:
        name:     Key name, e.g. 'SESSION_SECRET' or 'MNEMOS_HMAC_KEY'
        provider: Service or app this key belongs to, e.g. 'App', 'Mnemos'
        length:   Key size in bytes (8–64). Default 32 = 256-bit.
        encoding: 'hex' (default, 64-char string) or 'base64' (URL-safe, ~43 chars)
        env_var:  Optional env var name this maps to
        notes:    Optional notes
    """
    if not (8 <= length <= 64):
        return {"error": "length must be between 8 and 64 bytes"}

    with db() as conn:
        existing = conn.execute(
            "SELECT id, name, provider, encrypted_value, hint, env_var FROM key_store WHERE name = ?",
            (name,),
        ).fetchone()

        if existing:
            try:
                value = decrypt_key(existing["encrypted_value"])
            except ValueError as e:
                return {"error": f"Decryption failed: {e}"}
            conn.execute(
                "INSERT INTO key_audit_log (key_id, action, detail) "
                "VALUES (?, 'revealed', 'generate_key get-or-create (existing) via MCP')",
                (existing["id"],),
            )
            return {
                "id": existing["id"],
                "name": existing["name"],
                "provider": existing["provider"],
                "hint": existing["hint"],
                "env_var": existing["env_var"],
                "encoding": encoding,
                "value": value,
                "created": False,
            }

        value = generate_random_key(length, encoding)
        encrypted = encrypt_key(value)
        hint = key_hint(value)
        cur = conn.execute(
            "INSERT INTO key_store (name, provider, encrypted_value, hint, env_var, notes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, provider, encrypted, hint, env_var, notes),
        )
        key_id = cur.lastrowid
        conn.execute(
            "INSERT INTO key_audit_log (key_id, action, detail) "
            "VALUES (?, 'created', ?)",
            (key_id, f"generated ({length}B {encoding}) via MCP"),
        )

    return {
        "id": key_id,
        "name": name,
        "provider": provider,
        "hint": hint,
        "env_var": env_var,
        "encoding": encoding,
        "value": value,
        "created": True,
    }


# ── Project tools ────────────────────────────────────────────────────────────

@mcp.tool()
def list_projects() -> list[dict]:
    """List all Rialú projects with name, status, phase, and platform."""
    with db() as conn:
        rows = conn.execute(
            "SELECT id, name, slug, phase, status, platform, repo_url, site_url, machine, notes, updated_at "
            "FROM projects ORDER BY updated_at DESC"
        ).fetchall()
    return [row_to_dict(r) for r in rows]


@mcp.tool()
def update_project(
    project_id: int,
    status: Optional[str] = None,
    platform: Optional[str] = None,
    site_url: Optional[str] = None,
    phase: Optional[str] = None,
    machine: Optional[str] = None,
    notes: Optional[str] = None,
    repo_url: Optional[str] = None,
    name: Optional[str] = None,
) -> dict:
    """
    Update one or more fields on a project. All fields are optional — only
    supplied fields are changed. Returns the updated project record.

    Args:
        project_id: Project id (from list_projects)
        status:     e.g. 'development', 'deployed', 'paused', 'archived', 'running'
        platform:   e.g. 'fly.io', 'railway', 'cf-pages', 'tauri'
        site_url:   Live URL for the project
        phase:      Current phase string
        machine:    Primary machine, e.g. 'daisy', 'rose', 'iris'
        notes:      Free-text notes
        repo_url:   GitHub URL
        name:       Rename the project
    """
    fields = {k: v for k, v in {
        "name": name, "phase": phase, "status": status, "notes": notes,
        "repo_url": repo_url, "site_url": site_url, "machine": machine,
        "platform": platform,
    }.items() if v is not None}
    if not fields:
        return {"error": "No fields supplied"}
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    set_clause += ", updated_at = datetime('now')"
    values = list(fields.values()) + [project_id]
    with db() as conn:
        conn.execute(
            f"UPDATE projects SET {set_clause} WHERE id = ?", values
        )
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
    if not row:
        return {"error": f"Project {project_id} not found"}
    return row_to_dict(row)


# ── Login tools ──────────────────────────────────────────────────────────────

@mcp.tool()
def list_logins() -> list[dict]:
    """
    List all stored credentials — passwords masked.
    Returns name, url, username, pwd_hint, notes.
    """
    with db() as conn:
        rows = conn.execute(
            "SELECT id, name, url, username, pwd_hint, notes, created_at, updated_at "
            "FROM credential_store ORDER BY name"
        ).fetchall()
    return [row_to_dict(r) for r in rows]


@mcp.tool()
def reveal_login(name: str) -> dict:
    """
    Decrypt and return the password for a login by name. Audited.

    Args:
        name: Exact login name (case-sensitive), e.g. 'Anthropic Console'
    """
    with db() as conn:
        row = conn.execute(
            "SELECT id, name, url, username, encrypted_pwd, totp_secret, notes FROM credential_store WHERE name = ?",
            (name,),
        ).fetchone()
        if not row:
            return {"error": f"Login '{name}' not found"}
        try:
            password = decrypt_key(row["encrypted_pwd"])
        except ValueError as e:
            return {"error": f"Decryption failed: {e}"}
        totp = None
        if row["totp_secret"]:
            try:
                totp = decrypt_key(row["totp_secret"])
            except ValueError:
                pass
        conn.execute(
            "INSERT INTO credential_audit_log (credential_id, action, detail) VALUES (?, 'revealed', 'via MCP')",
            (row["id"],),
        )
    return {
        "id": row["id"],
        "name": row["name"],
        "url": row["url"],
        "username": row["username"],
        "password": password,
        "totp_secret": totp,
    }


@mcp.tool()
def store_login(
    name: str,
    password: str,
    username: Optional[str] = "",
    url: Optional[str] = None,
    totp_secret: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """
    Store a new login credential in the vault.

    Args:
        name:        Display name, e.g. 'Railway Dashboard', 'Google Cloud Console'
        username:    Email or username
        password:    Password to encrypt and store
        url:         Login page URL (optional)
        totp_secret: TOTP/2FA secret for backup (optional, encrypted)
        notes:       Optional notes
    """
    encrypted = encrypt_key(password)
    hint = key_hint(password)
    enc_totp = encrypt_key(totp_secret) if totp_secret else None
    with db() as conn:
        existing = conn.execute(
            "SELECT id FROM credential_store WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            return {"error": f"Login '{name}' already exists"}
        cur = conn.execute(
            "INSERT INTO credential_store (name, url, username, encrypted_pwd, pwd_hint, totp_secret, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, url, username, encrypted, hint, enc_totp, notes),
        )
        cid = cur.lastrowid
        conn.execute(
            "INSERT INTO credential_audit_log (credential_id, action, detail) VALUES (?, 'created', 'via MCP')",
            (cid,),
        )
    return {"id": cid, "name": name, "username": username, "pwd_hint": hint}


# ── ASGI app ─────────────────────────────────────────────────────────────────

def get_asgi_app():
    """Return the MCP ASGI app for mounting in main.py."""
    return mcp.streamable_http_app()
