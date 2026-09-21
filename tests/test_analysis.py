from drix.analysis import analyze
from drix.model import (
    Inventory,
    Manifest,
    ManifestRequirement,
    PackageKey,
    PackageRecord,
    RequirementEdge,
)


def rec(name: str, deps: list[str] | None = None, version: str = "1.0") -> PackageRecord:
    key = PackageKey("pypi", name)
    record = PackageRecord(key, version)
    for dep in deps or []:
        record.dependencies.append(RequirementEdge(key, PackageKey("pypi", dep), dep, ""))
    return record


def test_ranking_prefers_declared_roots_affected() -> None:
    inv = Inventory()
    for record in [
        rec("a", ["shared"]),
        rec("b", ["shared"]),
        rec("c", ["branch"]),
        rec("branch", ["leaf1", "leaf2"]),
        rec("shared"),
        rec("leaf1"),
        rec("leaf2"),
    ]:
        inv.add(record)
    manifest = Manifest(
        (
            ManifestRequirement(PackageKey("pypi", "a"), "a"),
            ManifestRequirement(PackageKey("pypi", "b"), "b"),
            ManifestRequirement(PackageKey("pypi", "c"), "c"),
        ),
        "test",
    )
    results, roots, missing = analyze(inv, manifest)
    assert not missing
    assert len(roots) == 3
    assert results[0].package.name == "shared"
    shared = results[0]
    assert len(shared.affected_roots) == 2
    assert shared.transitive_count == 2


def test_manifest_constraint_participates_in_conflict() -> None:
    inv = Inventory()
    app = PackageRecord(PackageKey("pypi", "app"), "1")
    app.dependencies.append(
        RequirementEdge(app.key, PackageKey("pypi", "numpy"), "numpy>=2", ">=2")
    )
    inv.add(app)
    inv.add(PackageRecord(PackageKey("pypi", "numpy"), "1.26"))
    manifest = Manifest(
        (
            ManifestRequirement(PackageKey("pypi", "app"), "app"),
            ManifestRequirement(PackageKey("pypi", "numpy"), "numpy<2", "<2"),
        ),
        "requirements.txt",
    )
    results, _, _ = analyze(inv, manifest)
    numpy = next(row for row in results if row.package.name == "numpy")
    assert numpy.version_state == "CONFLICT"
    assert any(c.package.ecosystem == "manifest" for c in numpy.contributors)


def test_manifest_pin_conflict_has_witnesses() -> None:
    inv = Inventory()
    app = PackageRecord(PackageKey("pypi", "app"), "1")
    app.dependencies.append(
        RequirementEdge(app.key, PackageKey("pypi", "numpy"), "numpy>=2", ">=2")
    )
    inv.add(app)
    inv.add(PackageRecord(PackageKey("pypi", "numpy"), "1.26"))
    manifest = Manifest(
        (
            ManifestRequirement(PackageKey("pypi", "app"), "app"),
            ManifestRequirement(PackageKey("pypi", "numpy"), "numpy<2", "<2"),
        ),
        "requirements.txt",
    )
    results, _, _ = analyze(inv, manifest)
    numpy = next(row for row in results if row.package.name == "numpy")
    assert numpy.version_state == "CONFLICT"
    assert len(numpy.conflict_by) == 2
