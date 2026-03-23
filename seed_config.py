"""
seed_config.py — One-time population of platforms, APIs, and initial projects.

Run ONCE after first deploy:
    python seed_config.py

Safe to re-run — uses INSERT OR IGNORE / INSERT OR REPLACE.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from db import init_db, db

# Domain pricing by TLD (annual, GBP)
DOMAIN_PRICING_GBP = {
    ".ie":    5.09,   # €5.99
    ".irish": 5.09,   # €5.99
    ".eu":    5.09,   # €5.99
    ".com":   10.20,
    ".uk":    6.80,
    ".us":    10.20,
    ".org":   10.20,
}


def _load_domains():
    """Read domains.txt and return budget entries."""
    import os
    path = os.path.join(os.path.dirname(__file__), "domains.txt")
    if not os.path.exists(path):
        return []
    entries = []
    with open(path) as f:
        for line in f:
            domain = line.strip()
            if not domain:
                continue
            # Find matching TLD
            cost = None
            for tld, price in DOMAIN_PRICING_GBP.items():
                if domain.endswith(tld):
                    cost = price
                    break
            if cost is None:
                cost = 10.20  # default
            entries.append(("domains", domain, cost, "annual"))
    return entries


BUDGET = [
    ("fly.io",       "mnemos",           8.40,  "monthly"),
    ("fly.io",       "git-mcp-foxxelabs",0.00,  "monthly"),
    ("railway",      "anseo",            3.00,  "monthly"),
    ("railway",      "anseo-alpha",      2.00,  "monthly"),
    ("cloudflare",   "foxxelabs.ie",     0.00,  "monthly"),
    ("cloudflare",   "cybersafer.uk",    0.00,  "monthly"),
]

API_REGISTRY = [
    # name, provider, auth_key_ref, billing_model, cost_unit, cost_per_unit_gbp, usage_api_endpoint
    ("Claude API",    "Anthropic",  "ANTHROPIC_API_KEY", "per_token",  "1k_tokens",  0.003,  "https://api.anthropic.com/v1/usage"),
    ("Fly.io API",    "Fly.io",     "FLY_API_TOKEN",     "included",   None,         0.0,    None),
    ("Railway GQL",   "Railway",    "RAILWAY_API_TOKEN", "included",   None,         0.0,    None),
    ("Cloudflare",    "Cloudflare", "CF_API_TOKEN",      "free",       None,         0.0,    None),
    ("GitHub API",    "GitHub",     "GITHUB_TOKEN",      "free",       None,         0.0,    None),
    ("Mnemos MCP",    "FoxxeLabs",  "MNEMOS_API_KEY",    "self-hosted",None,         0.0,    "https://mnemos.foxxelabs.ie/api/stats"),
]

PROJECTS = [
    # name, slug, status, machine, platform, phase, notes, site_url
    ("Aislinge",    "aislinge",   "running",    "rose",   None,      "phase-4-eval",
     "Dream consolidation runtime. Radharc → Aislinge → Legion. Turns episodic Mnemos snapshots into learning statements.",
     None),
    ("Legion",      "legion",     "development","iris",   None,      "peekaboo",
     "Distributed AI swarm. IRC-based agent-to-agent protocol. SFI funding target Q2.",
     None),
    ("UCA Dissertation","uca-dissertation","development",None,None,  "chapter-3",
     "MSc Cyberpsychology dissertation, ATU Galway. Hard deadline 12 June 2026.",
     None),
    ("Mnemos",      "mnemos",     "deployed",   "rose",   "fly.io",  None,
     "Personal RAG memory system. ChromaDB + sentence-transformers + FastMCP.",
     "https://mnemos.foxxelabs.ie"),
    ("Anseo",       "anseo",      "deployed",   None,     "railway", None,
     "Django community platform. anseo.irish and alpha.anseo.irish.",
     "https://anseo.irish"),
    ("Scéal",       "sceal",      "development","daisy",  None,      "tokeniser",
     "Irish language NLP model training on Daisy (RTX 5060).",
     None),
    ("Foghliam",    "foghliam",   "development","daisy",  None,      "scaffold",
     "Irish learning tool. Dev server running on Daisy :8000.",
     None),
    ("AfterWords",  "afterwords", "research",   None,     None,      None,
     "Digital legacy avatar project. University of Souls framing. toddBot = AI avatar.",
     None),
    ("Rialú",       "rialu",      "development",None,     "fly.io",  "phase-1",
     "This app. Personal command centre at rialu.ie.",
     "https://rialu.ie"),
    ("CyberSafer",  "cybersafer", "paused",     None,     "cf-pages",None,
     "v2 deployed. 21 scenarios, 6 categories, 764 test checks. ICO registration pending.",
     "https://cybersafer.uk"),
    ("Sentinel",    "sentinel",   "deployed",   None,     "fly.io",  None,
     "Centralised IP threat intelligence for FoxxeLabs Django projects.",
     "https://sentinel.foxxelabs.ie"),
    ("git-mcp",     "git-mcp",    "deployed",   None,     "fly.io",  None,
     "MCP server providing Git operations for GitHub repos.",
     "https://git-mcp-foxxelabs.fly.dev"),
]

MILESTONES = {
    "aislinge": [
        ("Phase 2 — abstraction pass", True),
        ("Phase 3 — ingestion into Mnemos", True),
        ("Phase 4 — evaluation", False),
        ("Phase 5 — hybrid retrieval (FTS5 + RRF)", False),
    ],
    "legion": [
        ("IRC scaffold", True),
        ("Peekaboo — agent-to-agent messaging", False),
        ("Swarm coordination protocol", False),
        ("SFI funding letter — cost breakdown", False),
    ],
    "uca-dissertation": [
        ("Chapter 1 — Introduction", True),
        ("Chapter 2 — Literature review", True),
        ("Chapter 3 — Cyberpsychology framework", False),
        ("Chapter 4 — Methodology", False),
        ("Chapter 5 — Analysis & Results", False),
        ("Final submission", False),
    ],
    "mnemos": [
        ("get_doc_count MCP tool deployed", True),
        ("Automated ingestion — email, Anseo, FoxxeLabs", True),
        ("Hybrid retrieval — standalone FTS5 + RRF layer", False),
        ("Google Drive ingestion (ingest_gdrive.py)", False),
    ],
    "rialu": [
        ("Phase 1 — Foundation (FastAPI + SQLite + Deployments + Projects + Worklog)", False),
        ("Phase 2 — Machine agents (rialu-agent daemon)", False),
        ("Phase 3 — Intelligence (Anthropic poller, Timeline, Kanban)", False),
        ("Phase 4 — Polish (exports, Mnemos integration)", False),
    ],
}


def seed():
    init_db()

    with db() as conn:
        # budget
        all_budget = BUDGET + _load_domains()
        for platform, service, cost, period in all_budget:
            conn.execute(
                """INSERT OR IGNORE INTO budget (platform, service_name, cost_gbp, period)
                   VALUES (?, ?, ?, ?)""",
                (platform, service, cost, period),
            )

        # api registry
        for name, provider, key_ref, model, unit, cost, endpoint in API_REGISTRY:
            conn.execute(
                """INSERT OR IGNORE INTO api_registry
                   (name, provider, auth_key_ref, billing_model, cost_unit, cost_per_unit_gbp, usage_api_endpoint)
                   VALUES (?,?,?,?,?,?,?)""",
                (name, provider, key_ref, model, unit, cost, endpoint),
            )

        # projects
        for name, slug, status, machine, platform, phase, notes, site_url in PROJECTS:
            existing = conn.execute(
                "SELECT id FROM projects WHERE slug = ?", (slug,)
            ).fetchone()
            if existing:
                # Backfill site_url if not already set
                if site_url:
                    conn.execute(
                        "UPDATE projects SET site_url = ? WHERE id = ? AND (site_url IS NULL OR site_url = '')",
                        (site_url, existing["id"]),
                    )
                print(f"  skip project (exists): {name}")
                continue
            conn.execute(
                """INSERT INTO projects (name, slug, status, machine, platform, phase, notes, site_url)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (name, slug, status, machine, platform, phase, notes, site_url),
            )
            print(f"  created project: {name}")

        # milestones — only if project exists and has no milestones yet
        for slug, items in MILESTONES.items():
            row = conn.execute(
                "SELECT id FROM projects WHERE slug = ?", (slug,)
            ).fetchone()
            if not row:
                continue
            pid = row["id"]
            existing = conn.execute(
                "SELECT COUNT(*) as c FROM milestones WHERE project_id = ?", (pid,)
            ).fetchone()["c"]
            if existing:
                continue
            for i, (title, done) in enumerate(items):
                conn.execute(
                    "INSERT INTO milestones (project_id, title, done, sort_order) VALUES (?,?,?,?)",
                    (pid, title, int(done), i),
                )
            print(f"  seeded milestones: {slug}")

    print("\nSeed complete.")


if __name__ == "__main__":
    seed()
