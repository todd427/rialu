# PRD: `update_project` MCP Tool

**Repo:** todd427/rialu  
**File:** `mcp_server.py`  
**Date:** 2026-03-27  
**Status:** Ready for implementation

---

## Problem

`mcp_server.py` exposes `list_projects` but has no write tool for projects. Any project field
correction (status, platform, site_url, etc.) requires hitting the REST API directly or using
the Rialú SPA. Claude cannot update project metadata via MCP.

This gap was discovered when trying to correct George (project id=16):
- `status`: `paused` → `deployed` (Phase 0 is complete and live)
- `platform`: `fly.io` → `railway` (Fly app is deprecated; George runs on Railway)
- `site_url`: `https://george-foxxelabs.fly.dev` → `https://george.foxxelabs.ie`

---

## Solution

Add a single `update_project` tool to `mcp_server.py`. The REST layer already has a full
`PUT /api/projects/{id}` endpoint with a `ProjectUpdate` Pydantic model — this is a thin MCP
wrapper around that existing logic, hitting the DB directly (same pattern as `list_projects`).

---

## Implementation

### 1. Update module docstring

Add to the Tools list in the module-level docstring:

```
  update_project  — patch status, platform, site_url, or any other project field by id
```

### 2. Add tool after `list_projects()`

```python
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
```

No new imports needed — `Optional` and `db`/`row_to_dict` are already in scope.

---

## Tests

Add to `tests/test_projects.py` (or a new `tests/test_mcp_update_project.py`):

```python
def test_update_project_status(fresh_db):
    # create a project
    # call update_project(project_id=..., status="deployed")
    # assert returned record has status == "deployed"
    # assert updated_at changed

def test_update_project_multiple_fields(fresh_db):
    # update status + platform + site_url in one call
    # assert all three changed

def test_update_project_no_fields_returns_error(fresh_db):
    # call update_project(project_id=1) with no optional args
    # assert {"error": "No fields supplied"}

def test_update_project_not_found_returns_error(fresh_db):
    # call update_project(project_id=99999, status="deployed")
    # assert {"error": "Project 99999 not found"}
```

---

## First Use After Deploy

Apply the following corrections to George (project id=16):

```python
update_project(
    project_id=16,
    status="deployed",
    platform="railway",
    site_url="https://george.foxxelabs.ie",
)
```

---

## Deploy

```bash
fly deploy
```

No schema changes. No migrations. No new dependencies.
