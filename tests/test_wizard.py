"""
tests/test_wizard.py — Tests for /api/wizard recipe endpoints.
"""

import json
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from main import app
from db import init_db

client = TestClient(app)

RECIPES_DIR = Path(__file__).parent.parent / "wizard_recipes"


@pytest.fixture(autouse=True)
def setup():
    init_db()


def test_list_recipes():
    resp = client.get("/api/wizard/recipes")
    assert resp.status_code == 200
    recipes = resp.json()
    assert len(recipes) >= 9
    # Metadata only — no steps in list response
    for r in recipes:
        assert "id" in r
        assert "name" in r
        assert "provider" in r
        assert "steps" not in r


def test_get_recipe():
    resp = client.get("/api/wizard/recipes/anthropic-api-key")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "anthropic-api-key"
    assert "steps" in data
    assert len(data["steps"]) > 0


def test_get_recipe_not_found():
    resp = client.get("/api/wizard/recipes/nonexistent-recipe")
    assert resp.status_code == 404


def test_recipe_has_required_fields():
    """Every recipe must have id, name, provider, description, steps."""
    resp = client.get("/api/wizard/recipes")
    for meta in resp.json():
        detail = client.get(f"/api/wizard/recipes/{meta['id']}").json()
        assert "id" in detail
        assert "name" in detail
        assert "provider" in detail
        assert "description" in detail
        assert "steps" in detail
        assert len(detail["steps"]) > 0
        # Every step must have id, title, instruction
        for step in detail["steps"]:
            assert "id" in step, f"Step missing id in {detail['id']}"
            assert "title" in step, f"Step missing title in {detail['id']}"
            assert "instruction" in step, f"Step missing instruction in {detail['id']}"


def test_recipe_json_files_valid():
    """All JSON files in wizard_recipes/ parse correctly."""
    for f in RECIPES_DIR.glob("*.json"):
        data = json.loads(f.read_text())
        assert "id" in data, f"{f.name} missing id"
        assert "steps" in data, f"{f.name} missing steps"


def test_at_least_one_input_step():
    """Every recipe should have at least one step that captures a secret."""
    resp = client.get("/api/wizard/recipes")
    for meta in resp.json():
        detail = client.get(f"/api/wizard/recipes/{meta['id']}").json()
        has_input = any(s.get("input_type") for s in detail["steps"])
        assert has_input, f"Recipe {detail['id']} has no input steps — nothing to capture"


def test_reload_recipes():
    resp = client.post("/api/wizard/recipes/reload")
    assert resp.status_code == 200
    assert resp.json()["count"] >= 9
