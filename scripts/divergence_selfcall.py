#!/usr/bin/env python3
"""
scripts/divergence_selfcall.py — trigger the weekly divergence digest over HTTP.

Run by the Fly *scheduled machine*. The DB volume (rialu_data) is single-attach,
so a separate machine cannot run the digest in-process against /data/rialu.db.
Instead this POSTs to the app's public edge, which auto-starts the app machine
that holds the volume and does the work.

Auth path (no Cloudflare service token needed):
  - Connect to rialu.fly.dev (Fly's public hostname; auto-starts the app machine)
  - Send `Host: rialu.ie` so CanonicalHostMiddleware doesn't 421
  - Send `Authorization: Bearer $FAIRE_WS_TOKEN` for verify_faire_token
    (FAIRE_WS_TOKEN is an app secret, inherited by every machine in the app)
"""

import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get("RIALU_SELFCALL_URL", "https://rialu.fly.dev/api/divergence/run")
TOKEN = os.environ.get("FAIRE_WS_TOKEN", "")


def main() -> int:
    if not TOKEN:
        print("FAIRE_WS_TOKEN not set in environment", file=sys.stderr)
        return 1
    req = urllib.request.Request(
        URL,
        method="POST",
        headers={"Host": "rialu.ie", "Authorization": f"Bearer {TOKEN}"},
    )
    try:
        # Generous timeout: the app machine may be cold-starting from stopped.
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"request failed: {e.reason}", file=sys.stderr)
        return 1

    try:
        data = json.loads(body)
        print(f"divergence run ok — checked {data.get('checked')} projects: {data.get('flags')}")
    except json.JSONDecodeError:
        print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
