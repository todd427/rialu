"""
mcp_server.py — MCP server for Rialú.

Exposes project data to Claude via MCP.
Mount point: /mcp (mounted in main.py)

Auth: OAuth 2.1 with PKCE + Dynamic Client Registration (DCR).
      Same pattern as Mnemos — auto-approves authorization for a personal server.
      State persisted to /data/oauth_state.json (Fly.io volume).

Canonical public URL: https://rialu.ie/mcp

OAuth issuer URL: RIALU_MCP_ISSUER (default https://rialu.ie) is embedded in
the OAuth Protected Resource Metadata (RFC 9728). MUST match the canonical
client-facing URL or Claude Code will refuse the cross-origin metadata
reference and report "Failed to connect".

DNS rebinding protection: DISABLED.

  The MCP SDK's TransportSecuritySettings (CVE-2025-66416 mitigation,
  mcp>=1.23.0) is designed for localhost MCP servers reachable via DNS
  rebinding from a malicious browser tab. Rialú is a public OAuth-protected
  server behind Cloudflare; the threat model doesn't apply, and the SDK's
  Host-header validation actively interferes with legitimate Cloudflare ->
  Fly.io traffic (CF rewrites Host: rialu.ie -> rialu.fly.dev when
  forwarding to the origin, and the SDK's internal resource-server check
  is keyed off the issuer URL, not the explicit allowed_hosts list, so
  setting transport_security with both hostnames doesn't help).

  Security in this deployment is layered:
    - TLS termination + (optional) Cloudflare Access at the edge
    - OAuth 2.1 PKCE + DCR for authentication
    - Bearer token + scope validation per request

  Disabling DNS rebinding protection here is the SDK-documented path for
  servers managing security at another layer (see
  https://github.com/modelcontextprotocol/python-sdk/issues/1798).

Tools:
  list_projects   — all projects with status
  update_project  — patch status, platform, site_url, or any other project field by id
  create_project  — create a new project, returns the created record
  get_project     — fetch a single project by id
"""

import json
import os
import re
import secrets as secrets_mod
import time
from pathlib import Path
from typing import Literal, Optional

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
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from db import db, row_to_dict


# ── Config ───────────────────────────────────────────────────────────────────

# Canonical public URL — embedded in OAuth Protected Resource Metadata.
# See module docstring for why this must match the canonical client-facing URL.
_ISSUER_URL = os.environ.get("RIALU_MCP_ISSUER", "https://rialu.ie")
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

mcp = FastMCP(
    name="Rialú",
    instructions=(
        "Rialú is Todd's personal DevOps command centre. Use these tools to "
        "view and manage project status across the portfolio."
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
    # See module docstring for why this is disabled. Security is handled at the
    # OAuth + Cloudflare layers, not at the SDK's DNS-rebinding-protection layer.
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
    stateless_http=True,
)


def _slugify(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_-]+", "-", s)
    return s.strip("-")[:64]


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


@mcp.tool()
def create_project(
    name: str,
    status: str = "development",
    phase: Optional[str] = None,
    platform: Optional[str] = None,
    repo_url: Optional[str] = None,
    site_url: Optional[str] = None,
    machine: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """
    Create a new project in Rialú.

    Slug is auto-generated from name. If the slug already exists, a suffix
    is appended to ensure uniqueness. Returns the created project record.

    Args:
        name:     Project display name, e.g. 'Litir'
        status:   'development' (default), 'deployed', 'paused', 'research', 'running'
        phase:    Current phase string, e.g. 'prd', 'phase-1'
        platform: e.g. 'fly.io', 'railway', 'cf-pages', 'local'
        repo_url: GitHub URL, e.g. 'https://github.com/todd427/anseo'
        site_url: Live URL, e.g. 'https://litir.anseo.irish'
        machine:  Primary machine, e.g. 'daisy', 'rose', 'iris'
        notes:    Free-text notes
    """
    slug = _slugify(name)
    with db() as conn:
        existing = conn.execute(
            "SELECT id FROM projects WHERE slug = ?", (slug,)
        ).fetchone()
        if existing:
            slug = f"{slug}-{existing['id']}"
        cur = conn.execute(
            """INSERT INTO projects
               (name, slug, phase, status, notes, repo_url, site_url, machine, platform)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, slug, phase, status, notes, repo_url, site_url, machine, platform),
        )
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return row_to_dict(row)


@mcp.tool()
def get_project(project_id: int) -> dict:
    """
    Fetch a single project by id. Returns the full project record.

    Args:
        project_id: Project id (from list_projects)
    """
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
    if not row:
        return {"error": f"Project {project_id} not found"}
    return row_to_dict(row)


# ── ASGI app ─────────────────────────────────────────────────────────────────

def get_asgi_app():
    """Return the MCP ASGI app for mounting in main.py."""
    return mcp.streamable_http_app()
