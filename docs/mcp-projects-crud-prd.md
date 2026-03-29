# PRD: MCP Tools — Project CRUD Expansion

**Project:** Rialú
**File:** `mcp_server.py`
**Branch:** `main`
**Status:** Ready for implementation

---

## Problem

The Rialú MCP server exposes `list_projects` and `update_project` but is missing
`create_project` and `get_project`. This means Claude cannot create new projects
via MCP — it has to instruct the user to visit rialu.ie manually, which breaks
the agentic workflow.

Discovered during a session where Claude needed to register the new Litir project
but could not do so programmatically.

---

## Scope

Add two tools to `mcp_server.py`:

1. `create_project` — create a new project, returns the created record
2. `get_project` — fetch a single project by id, returns the full record

No changes to `routers/projects.py`, `db.py`, or any other file.
No new database migrations — the `projects` table schema is unchanged.

---

## Implementation

### 1. `slugify` helper

The `slugify` function already exists in `routers/projects.py`. Copy it verbatim
into `mcp_server.py` as a module-level private function `_slugify` (prefixed to
avoid any future naming collision with the router):

```python
import re

def _slugify(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_-]+", "-", s)
    return s.strip("-")[:64]
```

`re` is already in the stdlib — no new imports required.

---

### 2. `create_project` tool

Add after the existing `update_project` tool in the `# ── Project tools ──`
section of `mcp_server.py`.

```python
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
```

---

### 3. `get_project` tool

Add immediately after `create_project`.

```python
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
```

---

### 4. Update the module docstring

The top-of-file docstring lists all exposed tools. Add the two new tools to the
`Tools:` section:

```
  create_project  — create a new project, returns the created record
  get_project     — fetch a single project by id
```

---

## Placement in `mcp_server.py`

The `# ── Project tools ──` section currently reads:

```python
@mcp.tool()
def list_projects() -> list[dict]:
    ...

@mcp.tool()
def update_project(...) -> dict:
    ...
```

After this change it should read:

```python
@mcp.tool()
def list_projects() -> list[dict]:
    ...

@mcp.tool()
def update_project(...) -> dict:
    ...

@mcp.tool()
def create_project(...) -> dict:
    ...

@mcp.tool()
def get_project(project_id: int) -> dict:
    ...
```

---

## Testing

Run the existing test suite — no new tests are strictly required since the
logic is a thin wrapper over the same DB calls used in `routers/projects.py`,
which is already tested. However, add at minimum:

- `test_mcp_update_project.py` already exists — check if it can be extended
- Add `tests/test_mcp_projects_crud.py` with:
  - `test_create_project_basic` — creates a project, checks id and slug returned
  - `test_create_project_slug_collision` — creates two projects with same name, checks suffix applied
  - `test_get_project_exists` — creates then fetches, checks fields match
  - `test_get_project_missing` — fetches non-existent id, checks error key in response

---

## Notes for CC

- `_slugify` is a copy of `slugify` from `routers/projects.py` — do not import
  across modules, just copy the 5-line function. The router and MCP server are
  independent execution contexts.
- Do not modify `routers/projects.py` — this PRD is `mcp_server.py` only.
- Do not add `delete_project` to the MCP surface. Deletion via Claude is too
  high-risk without a confirmation UI. The web interface at rialu.ie handles
  deletion.
- The Faire hub broadcast (`_broadcast_project`) used in the router is not
  needed in the MCP tools — the MCP server does not have access to the async
  event loop in the same way. Omit it.
- After deploying, disconnect and reconnect the Rialú integration in
  Claude.ai settings to reload the tool schema — the new tools will not
  appear until the schema is refreshed.
