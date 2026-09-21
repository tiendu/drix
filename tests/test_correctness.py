from drix.analysis import analyze, infer_roots
from drix.inventory import reconcile_inventory
from drix.model import (
    Inventory,
    Manifest,
    ManifestRequirement,
    PackageKey,
    PackageRecord,
    RequirementEdge,
    normalize_name,
)


def test_pep503_name_canonicalization() -> None:
    assert normalize_name("Zope.Interface") == "zope-interface"
    assert PackageKey("pypi", "foo_bar") == PackageKey("pypi", "foo.bar")


def test_pip_edge_reconciles_to_conda_owned_package() -> None:
    inventory = Inventory()
    numpy = PackageRecord(PackageKey("conda", "numpy"), "2.0", python_names={"numpy"})
    app = PackageRecord(PackageKey("pypi", "app"), "1.0")
    app.dependencies.append(
        RequirementEdge(app.key, PackageKey("pypi", "numpy"), "numpy>=1.26", ">=1.26")
    )
    inventory.add(numpy)
    inventory.add(app)

    reconcile_inventory(inventory)

    assert app.dependencies[0].target == PackageKey("conda", "numpy")
    assert PackageKey("pypi", "numpy") not in inventory.packages
    results, _, _ = analyze(inventory)
    numpy_risk = next(row for row in results if row.package == PackageKey("conda", "numpy"))
    assert numpy_risk.direct_dependents == (PackageKey("pypi", "app"),)
    assert numpy_risk.affected_roots == (PackageKey("pypi", "app"),)


def test_unresolved_dependency_is_explicitly_missing() -> None:
    inventory = Inventory()
    app = PackageRecord(PackageKey("pypi", "app"), "1.0")
    app.dependencies.append(
        RequirementEdge(app.key, PackageKey("pypi", "ghost"), "ghost>=2", ">=2")
    )
    inventory.add(app)
    reconcile_inventory(inventory)

    results, _, _ = analyze(inventory)
    ghost = next(row for row in results if row.package.name == "ghost")
    assert ghost.installed is False
    assert ghost.version_state == "MISSING"
    assert ghost.affected_roots == (PackageKey("pypi", "app"),)


def test_cycle_is_not_lost_during_root_inference() -> None:
    inventory = Inventory()
    a = PackageRecord(PackageKey("pypi", "a"), "1")
    b = PackageRecord(PackageKey("pypi", "b"), "1")
    a.dependencies.append(RequirementEdge(a.key, b.key, "b", ""))
    b.dependencies.append(RequirementEdge(b.key, a.key, "a", ""))
    inventory.add(a)
    inventory.add(b)

    assert infer_roots(inventory) == {a.key, b.key}


def test_focused_risk_contains_shortest_root_path() -> None:
    inventory = Inventory()
    app = PackageRecord(PackageKey("pypi", "app"), "1")
    mid = PackageRecord(PackageKey("pypi", "mid"), "1")
    leaf = PackageRecord(PackageKey("pypi", "leaf"), "1")
    app.dependencies.append(RequirementEdge(app.key, mid.key, "mid", ""))
    mid.dependencies.append(RequirementEdge(mid.key, leaf.key, "leaf", ""))
    for record in (app, mid, leaf):
        inventory.add(record)

    manifest = Manifest((ManifestRequirement(app.key, "app"),), "requirements.txt")
    results, _, _ = analyze(inventory, manifest, focus="leaf")
    leaf_risk = next(row for row in results if row.package == leaf.key)
    assert leaf_risk.root_paths == ((app.key, mid.key, leaf.key),)
