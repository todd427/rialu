# Rialú

**Irish:** *RIAL-oo* — control, regulation, governance.

Personal command centre for FoxxeLabs. Tracks projects, machine agents, cloud deployments, work sessions, and API costs from a single interface.

**Domain:** [rialu.ie](https://rialu.ie)  
**Stack:** FastAPI · SQLite · APScheduler · Fly.io · Cloudflare Access  
**Wireframe:** [foxxelabs.ie/ops-dashboard/](https://foxxelabs.ie/ops-dashboard/)

## Structure

```
rialu/
├── docs/
│   └── rialu-prd-v1.md     # Product requirements
├── routers/                # FastAPI routers (phase 1+)
├── agent/                  # rialu-agent daemon (phase 2)
├── static/                 # Single-file SPA
└── tests/
```

## Build phases

| Phase | Scope |
|---|---|
| 1 | Foundation: FastAPI skeleton, SQLite, Deployments + Projects + Work log tabs |
| 2 | Machine agents: rialu-agent daemon, Machines tab, action proxy |
| 3 | Intelligence: Anthropic usage poller, Timeline + Kanban views, alerts |
| 4 | Polish: exports, Mnemos integration, OAuth 2.1 agent auth |

See `docs/rialu-prd-v1.md` for full specification.
