import json

from drix import __version__
from drix.analysis import analyze
from drix.inventory import reconcile_inventory
from drix.model import Inventory, PackageKey, PackageRecord, RequirementEdge
from drix.render import render_json, render_text


def test_focus_shows_dependency_path_and_missing_state() -> None:
    inventory = Inventory()
    app = PackageRecord(PackageKey("pypi", "app"), "1")
    app.dependencies.append(
        RequirementEdge(app.key, PackageKey("pypi", "ghost"), "ghost>=2", ">=2")
    )
    inventory.add(app)
    reconcile_inventory(inventory)
    results, roots, missing = analyze(inventory, focus="ghost")

    text = render_text(inventory, results, roots, missing, "ghost")
    assert "version: MISSING" in text
    assert "app [pip] -> ghost [pip]" in text


def test_global_json_reports_transitive_count_without_materializing_lists() -> None:
    inventory = Inventory()
    app = PackageRecord(PackageKey("pypi", "app"), "1")
    leaf = PackageRecord(PackageKey("pypi", "leaf"), "1")
    app.dependencies.append(RequirementEdge(app.key, leaf.key, "leaf"))
    inventory.add(app)
    inventory.add(leaf)
    results, roots, missing = analyze(inventory)

    payload = json.loads(render_json(inventory, results, roots, missing))
    row = next(item for item in payload["packages"] if item["package"]["name"] == "leaf")
    assert row["transitive_dependents_count"] == 1
    assert "transitive_dependents" not in row


def test_focused_json_materializes_transitive_dependents() -> None:
    inventory = Inventory()
    app = PackageRecord(PackageKey("pypi", "app"), "1")
    leaf = PackageRecord(PackageKey("pypi", "leaf"), "1")
    app.dependencies.append(RequirementEdge(app.key, leaf.key, "leaf"))
    inventory.add(app)
    inventory.add(leaf)
    results, roots, missing = analyze(inventory, focus="leaf")

    payload = json.loads(render_json(inventory, results, roots, missing, "leaf"))
    row = payload["packages"][0]
    assert row["transitive_dependents_count"] == 1
    assert row["transitive_dependents"] == [{"ecosystem": "pypi", "name": "app"}]


def test_json_has_explicit_schema_and_tool_version() -> None:
    inventory = Inventory()
    inventory.add(PackageRecord(PackageKey("pypi", "app"), "1"))
    results, roots, missing = analyze(inventory)

    payload = json.loads(render_json(inventory, results, roots, missing))
    assert payload["schema_version"] == 1
    assert payload["drix_version"] == __version__
