# Rialú

**Irish:** *RIAL-oo* — control, regulation, governance.

Personal command centre for FoxxeLabs. Tracks projects, machine agents, cloud deployments, work sessions, and API costs from a single interface.

**Domain:** [rialu.ie](https://rialu.ie)  
**Stack:** FastAPI · SQLite · APScheduler · Fly.io · Cloudflare Access  
**Wireframe:** [foxxelabs.ie/ops-dashboard/](https://foxxelabs.ie/ops-dashboard/)  
**PRD:** [docs/rialu-prd-v1.md](docs/rialu-prd-v1.md)

---

## Quick start (local)

```bash
cd /home/Projects
git clone https://github.com/todd427/rialu
cd rialu

# venv — follow FoxxeLabs convention
python -m venv ../venvs/rialu-env
source ../venvs/rialu-env/bin/activate

pip install -r requirements.txt
pip install -r requirements-test.txt

# seed DB and run
python seed_config.py
python main.py
# → http://localhost:8080
```

---

## Tests

```bash
pytest tests/ -v
```

All 5 test modules, ~44 tests. No external dependencies needed — pollers are stubbed.

---

## Deploy to Fly.io

```bash
# First time only
fly launch --no-deploy
fly volumes create rialu_data --size 1 --region lhr

# Secrets
fly secrets set FLY_API_TOKEN=<token>
fly secrets set RAILWAY_API_TOKEN=<token>

# Deploy
fly deploy

# Seed production DB
fly ssh console -C "python seed_config.py"
```

Then point `rialu.ie` at the Fly app via Cloudflare DNS, and configure Cloudflare Access:
1. Create Access application for `rialu.ie`
2. Add Google OAuth identity provider
3. Policy: allow email = `todd@foxxelabs.ie`

---

## Structure

```
rialu/
├── docs/rialu-prd-v1.md     # Full PRD
├── main.py                  # FastAPI app + lifespan
├── db.py                    # SQLite migrations + context manager
├── poller.py                # APScheduler: Fly.io + Railway (60s each)
├── seed_config.py           # One-time: populate projects, budget, APIs
├── routers/
│   ├── projects.py          # CRUD: projects, milestones, sessions
│   ├── worklog.py           # Work log + rolling stats
│   ├── deployments.py       # Cached deploy status + refresh
│   ├── budget.py            # Platform costs + API registry
│   └── machines.py          # Phase 2 stub
├── static/index.html        # Single-file SPA (all tabs, all themes)
└── tests/                   # 5 test modules
```

## Build phases

| Phase | Scope | Status |
|---|---|---|
| **1** | Foundation: FastAPI + SQLite + Deployments + Projects + Work log | ✓ Done |
| 2 | Machine agents: rialu-agent daemon, Machines tab, action proxy | — |
| 3 | Intelligence: Anthropic usage poller, Timeline + Kanban views, alerts | — |
| 4 | Polish: exports, Mnemos integration, OAuth 2.1 agent auth | — |
