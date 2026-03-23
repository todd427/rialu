"""
auth.py — Authentication dependencies for Rialú API.

- verify_faire_token: Bearer token auth for Faire desktop client
- Checks Authorization header against FAIRE_WS_TOKEN env var
- Skipped in TEST_MODE
"""

import os
from fastapi import HTTPException, Header
from typing import Optional


def verify_faire_token(authorization: Optional[str] = Header(None)):
    """FastAPI dependency — verify Bearer token from Faire client."""
    if os.environ.get("RIALU_TEST") == "1":
        return  # Skip auth in test mode

    expected = os.environ.get("FAIRE_WS_TOKEN", "")
    if not expected:
        return  # No token configured — skip (dev mode)

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = authorization[7:]  # strip "Bearer "
    if token != expected:
        raise HTTPException(status_code=401, detail="Invalid token")
