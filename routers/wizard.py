"""
routers/wizard.py — Secrets Wizard recipe API.

Serves step-by-step recipes for obtaining secrets from external providers.
Recipes loaded from wizard_recipes/*.json at startup.
"""

import json
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/wizard", tags=["wizard"])

RECIPES_DIR = Path(__file__).parent.parent / "wizard_recipes"

_recipes: dict[str, dict] = {}


def _load_recipes() -> None:
    """Load all recipe JSON files from wizard_recipes/."""
    _recipes.clear()
    if not RECIPES_DIR.is_dir():
        return
    for f in sorted(RECIPES_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            rid = data.get("id", f.stem)
            _recipes[rid] = data
        except (json.JSONDecodeError, KeyError) as e:
            print(f"[wizard] Skipping invalid recipe {f.name}: {e}")


# Load on import
_load_recipes()


@router.get("/recipes")
def list_recipes():
    """List all recipes — metadata only (no steps)."""
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "provider": r["provider"],
            "description": r["description"],
            "env_var": r.get("env_var", ""),
            "tags": r.get("tags", []),
        }
        for r in _recipes.values()
    ]


@router.get("/recipes/{recipe_id}")
def get_recipe(recipe_id: str):
    """Full recipe with all steps."""
    recipe = _recipes.get(recipe_id)
    if not recipe:
        raise HTTPException(404, "Recipe not found")
    return recipe


@router.post("/recipes/reload")
def reload_recipes():
    """Reload recipes from disk."""
    _load_recipes()
    return {"status": "ok", "count": len(_recipes)}
