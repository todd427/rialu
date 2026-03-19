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
    # name, slug, status, machine, platform, phase, notes
    ("Aislinge",    "aislinge",   "running",    "rose",   None,      "phase-4-eval",
     "Dream consolidation runtime. Radharc → Aislinge → Legion. Turns episodic Mnemos snapshots into learning statements."),
    ("Legion",      "legion",     "development","iris",   None,      "peekaboo",
     "Distributed AI swarm. IRC-based agent-to-agent protocol. SFI funding target Q2."),
    ("UCA Dissertation","uca-dissertation","development",None,None,  "chapter-3",
     "MSc Cyberpsychology dissertation, ATU Galway. Hard deadline 12 June 2026."),
    ("Mnemos",      "mnemos",     "deployed",   "rose",   "fly.io",  None,
     "Personal RAG memory system. ChromaDB + sentence-transformers + FastMCP."),
    ("Anseo",       "anseo",      "deployed",   None,     "railway", None,
     "Django community platform. anseo.irish and alpha.anseo.irish."),
    ("Scéal",       "sceal",      "development","daisy",  None,      "tokeniser",
     "Irish language NLP model training on Daisy (RTX 5060)."),
    ("Foghliam",    "foghliam",   "development","daisy",  None,      "scaffold",
     "Irish learning tool. Dev server running on Daisy :8000."),
    ("AfterWords",  "afterwords", "research",   None,     None,      None,
     "Digital legacy avatar project. University of Souls framing. toddBot = AI avatar."),
    ("Rialú",       "rialu",      "development",None,     "fly.io",  "phase-1",
     "This app. Personal command centre at rialu.ie."),
    ("CyberSafer",  "cybersafer", "paused",     None,     "cf-pages",None,
     "v2 deployed. 21 scenarios, 6 categories, 764 test checks. ICO registration pending."),
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
        for platform, service, cost, period in BUDGET:
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
        for name, slug, status, machine, platform, phase, notes in PROJECTS:
            existing = conn.execute(
                "SELECT id FROM projects WHERE slug = ?", (slug,)
            ).fetchone()
            if existing:
                print(f"  skip project (exists): {name}")
                continue
            conn.execute(
                """INSERT INTO projects (name, slug, status, machine, platform, phase, notes)
                   VALUES (?,?,?,?,?,?,?)""",
                (name, slug, status, machine, platform, phase, notes),
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
