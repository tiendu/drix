from pathlib import Path

import pytest
from packaging.markers import default_environment

from drix.analysis import analyze, infer_roots
from drix.constraints import analyze_constraints
from drix.inventory import _parse_conda_dependency, reconcile_inventory
from drix.manifests import load_manifest
from drix.model import (
    ConstraintContributor,
    Inventory,
    Manifest,
    ManifestRequirement,
    PackageKey,
    PackageRecord,
    RequirementEdge,
)


def contributor(parent: str, specifier: str, ecosystem: str = "pypi") -> ConstraintContributor:
    return ConstraintContributor(
        PackageKey(ecosystem, parent),
        f"target{specifier}",
        specifier,
    )


def test_wildcard_include_and_exclude_is_a_conflict() -> None:
    result = analyze_constraints(
        "pypi",
        "1.5",
        (contributor("a", "==1.*"), contributor("b", "!=1.*")),
    )
    assert result.state == "CONFLICT"
    assert len(result.conflict_by) == 2


def test_bounded_range_fully_removed_by_wildcard_exclusion_is_conflict() -> None:
    result = analyze_constraints(
        "pypi",
        "1.5",
        (contributor("a", ">=1,<2"), contributor("b", "!=1.*")),
    )
    assert result.state == "CONFLICT"


def test_unknown_constraint_does_not_hide_a_known_conflicting_subset() -> None:
    result = analyze_constraints(
        "pypi",
        "1.5",
        (
            contributor("broken-metadata", "not-a-specifier"),
            contributor("a", "<1"),
            contributor("b", ">=2"),
        ),
    )
    assert result.state == "CONFLICT"
    assert {item.package.name for item in result.conflict_by} == {"a", "b"}


def test_conda_build_pin_is_unknown_without_native_conda_semantics() -> None:
    # A version-only fallback must never interpret the build string as part of a version.
    result = analyze_constraints(
        "conda",
        "3.12.7",
        (contributor("manifest", "=3.12.7=cpython_0", "conda"),),
    )
    assert result.state == "UNKNOWN"


def test_conda_dependency_is_not_satisfied_by_same_named_pip_package() -> None:
    inventory = Inventory()
    parent = PackageRecord(PackageKey("conda", "native-app"), "1")
    parent.dependencies.append(
        RequirementEdge(parent.key, PackageKey("conda", "shared-name"), "shared-name >=1", ">=1")
    )
    inventory.add(parent)
    inventory.add(PackageRecord(PackageKey("pypi", "shared-name"), "1"))

    reconcile_inventory(inventory)

    assert parent.dependencies[0].target == PackageKey("conda", "shared-name")
    analyze(inventory)
    missing = inventory.packages[PackageKey("conda", "shared-name")]
    assert missing.installed is False


def test_conda_manifest_root_is_not_satisfied_by_same_named_pip_package() -> None:
    inventory = Inventory()
    inventory.add(PackageRecord(PackageKey("pypi", "numpy"), "2.0"))
    manifest = Manifest(
        (ManifestRequirement(PackageKey("conda", "numpy"), "numpy=2", "=2"),),
        "environment.yml",
    )

    results, roots, missing = analyze(inventory, manifest)

    assert "conda:numpy" in missing
    assert PackageKey("conda", "numpy") in roots
    row = next(item for item in results if item.package == PackageKey("conda", "numpy"))
    assert row.version_state == "MISSING"


def test_conda_virtual_packages_are_not_reported_as_missing_packages() -> None:
    parent = PackageKey("conda", "python")
    assert _parse_conda_dependency("__glibc >=2.17,<3.0.a0", parent) is None
    assert _parse_conda_dependency("__linux >=5", parent) is None


def test_self_dependency_does_not_inflate_direct_dependents() -> None:
    inventory = Inventory()
    package = PackageRecord(PackageKey("pypi", "selfish"), "1")
    package.dependencies.append(
        RequirementEdge(package.key, package.key, "selfish>=1", ">=1")
    )
    inventory.add(package)
    reconcile_inventory(inventory)

    results, _, _ = analyze(inventory)
    row = next(item for item in results if item.package == package.key)
    assert row.direct_dependents == ()
    assert row.transitive_count == 0


def test_very_deep_graph_does_not_depend_on_python_recursion_limit() -> None:
    inventory = Inventory()
    count = 2500
    for index in range(count):
        key = PackageKey("pypi", f"p{index}")
        record = PackageRecord(key, "1")
        if index + 1 < count:
            record.dependencies.append(
                RequirementEdge(key, PackageKey("pypi", f"p{index + 1}"), f"p{index + 1}", "")
            )
        inventory.add(record)

    roots = infer_roots(inventory)
    assert roots == {PackageKey("pypi", "p0")}


def test_requirements_inline_comment_keeps_constraint(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text("numpy>=1.26,<2  # scientific stack\n", encoding="utf-8")

    manifest = load_manifest(path)

    assert set(manifest.requirements[0].specifier.split(",")) == {">=1.26", "<2"}


def test_malformed_requirements_line_fails_instead_of_dropping_constraint(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text("numpy=>1\n", encoding="utf-8")

    with pytest.raises(ValueError):
        load_manifest(path)


def test_malformed_environment_dependencies_shape_fails(tmp_path: Path) -> None:
    path = tmp_path / "environment.yml"
    path.write_text("dependencies: numpy\n", encoding="utf-8")

    with pytest.raises(TypeError):
        load_manifest(path)


def test_malformed_pyproject_dependencies_shape_fails(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname="x"\ndependencies="numpy"\n', encoding="utf-8")

    with pytest.raises(TypeError):
        load_manifest(path)


def test_conda_channel_qualified_root_parses_package_not_channel(tmp_path: Path) -> None:
    path = tmp_path / "environment.yml"
    path.write_text("dependencies:\n  - conda-forge::numpy>=2\n", encoding="utf-8")

    manifest = load_manifest(path)

    req = manifest.requirements[0]
    assert req.package == PackageKey("conda", "numpy")
    assert req.specifier == ">=2"


def test_inactive_python_marker_is_not_a_declared_root(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text('ancient-only; python_version < "2"\nrequests>=2\n', encoding="utf-8")

    manifest = load_manifest(path)

    assert PackageKey("pypi", "ancient-only") not in manifest.roots
    assert PackageKey("pypi", "requests") in manifest.roots


def test_environment_unknown_dependency_mapping_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "environment.yml"
    path.write_text("dependencies:\n  - mystery:\n      - thing\n", encoding="utf-8")

    with pytest.raises(ValueError):
        load_manifest(path)


def test_environment_non_mapping_root_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "environment.yml"
    path.write_text("- numpy\n- scipy\n", encoding="utf-8")

    with pytest.raises(TypeError):
        load_manifest(path)


def test_conda_name_identity_does_not_use_pep503_separator_folding() -> None:
    assert PackageKey("conda", "foo_bar") != PackageKey("conda", "foo-bar")
    assert PackageKey("conda", "foo.bar") != PackageKey("conda", "foo-bar")
    assert PackageKey("conda", "Foo_Bar") == PackageKey("conda", "foo_bar")


def test_pypi_to_conda_cross_name_bridge_is_ambiguous_not_guessed() -> None:
    inventory = Inventory()
    app = PackageRecord(PackageKey("pypi", "app"), "1")
    app.dependencies.append(
        RequirementEdge(app.key, PackageKey("pypi", "foo-bar"), "foo-bar>=1", ">=1")
    )
    inventory.add(app)
    inventory.add(PackageRecord(PackageKey("conda", "foo_bar"), "1", python_names={"foo-bar"}))
    inventory.add(PackageRecord(PackageKey("conda", "foo-bar"), "1", python_names={"foo-bar"}))

    reconcile_inventory(inventory)

    assert app.dependencies[0].target == PackageKey("pypi", "foo-bar")
    analyze(inventory)
    assert inventory.packages[PackageKey("pypi", "foo-bar")].installed is False
    assert any("ambiguous dependency identity" in item for item in inventory.diagnostics)


def test_focus_matching_keeps_conda_separator_identity() -> None:
    from drix.analysis import analyze

    inventory = Inventory()
    inventory.add(PackageRecord(PackageKey("conda", "python_abi"), "3.12"))
    inventory.add(PackageRecord(PackageKey("conda", "python-abi"), "9"))

    results, _, _ = analyze(inventory, focus="python_abi")
    underscored = next(row for row in results if row.package == PackageKey("conda", "python_abi"))
    dashed = next(row for row in results if row.package == PackageKey("conda", "python-abi"))
    assert underscored.root_paths == ((PackageKey("conda", "python_abi"),),)
    assert dashed.root_paths == ()


def test_duplicate_installed_versions_become_unknown_not_last_one_wins() -> None:
    inventory = Inventory()
    first = PackageRecord(PackageKey("pypi", "dup"), "1.0")
    second = PackageRecord(PackageKey("pypi", "dup"), "2.0")

    inventory.add(first)
    inventory.add(second)

    record = inventory.packages[PackageKey("pypi", "dup")]
    assert record.version is None
    assert record.installed is True
    assert any("multiple installed versions" in item for item in inventory.diagnostics)


def test_deep_chain_full_analysis_keeps_exact_blast_counts() -> None:
    count = 1200
    inventory = Inventory()
    for index in range(count):
        key = PackageKey("pypi", f"deep-{index}")
        record = PackageRecord(key, "1")
        if index + 1 < count:
            target = PackageKey("pypi", f"deep-{index + 1}")
            record.dependencies.append(RequirementEdge(key, target, target.name, ""))
        inventory.add(record)

    root = PackageKey("pypi", "deep-0")
    manifest = Manifest((ManifestRequirement(root, root.name),), "requirements.txt")
    results, roots, missing = analyze(inventory, manifest)

    assert roots == {root}
    assert missing == []
    leaf = next(row for row in results if row.package == PackageKey("pypi", f"deep-{count - 1}"))
    assert leaf.transitive_count == count - 1
    assert leaf.affected_roots == (root,)


def test_manifest_extra_activates_optional_dependency() -> None:
    inventory = Inventory()
    feature = PackageRecord(PackageKey("pypi", "featurepkg"), "1")
    optional = PackageKey("pypi", "optionaldep")
    feature.dependencies.append(
        RequirementEdge(
            feature.key,
            optional,
            'optionaldep; extra == "fast"',
            "",
            marker='extra == "fast"',
        )
    )
    inventory.add(feature)
    inventory.add(PackageRecord(optional, "1"))
    manifest = Manifest(
        (
            ManifestRequirement(
                feature.key,
                "featurepkg[fast]",
                extras=("fast",),
            ),
        ),
        "requirements.txt",
    )

    results, roots, missing = analyze(inventory, manifest)

    assert roots == {feature.key}
    assert missing == []
    row = next(item for item in results if item.package == optional)
    assert row.direct_dependents == (feature.key,)
    assert row.affected_roots == (feature.key,)


def test_unselected_extra_does_not_create_fake_missing_dependency() -> None:
    inventory = Inventory()
    feature = PackageRecord(PackageKey("pypi", "featurepkg"), "1")
    optional = PackageKey("pypi", "optionaldep")
    feature.dependencies.append(
        RequirementEdge(
            feature.key,
            optional,
            'optionaldep; extra == "fast"',
            "",
            marker='extra == "fast"',
        )
    )
    inventory.add(feature)
    manifest = Manifest(
        (ManifestRequirement(feature.key, "featurepkg"),),
        "requirements.txt",
    )

    results, _, missing = analyze(inventory, manifest)

    assert missing == []
    assert optional not in inventory.packages
    assert all(item.package != optional for item in results)


def test_transitive_extra_request_propagates_to_optional_dependency() -> None:
    inventory = Inventory()
    app = PackageRecord(PackageKey("pypi", "app"), "1")
    library = PackageRecord(PackageKey("pypi", "library"), "1")
    accelerator = PackageKey("pypi", "accelerator")
    app.dependencies.append(
        RequirementEdge(
            app.key,
            library.key,
            "library[fast]>=1",
            ">=1",
            extras=("fast",),
        )
    )
    library.dependencies.append(
        RequirementEdge(
            library.key,
            accelerator,
            'accelerator; extra == "fast"',
            "",
            marker='extra == "fast"',
        )
    )
    inventory.add(app)
    inventory.add(library)
    inventory.add(PackageRecord(accelerator, "1"))
    manifest = Manifest((ManifestRequirement(app.key, "app"),), "requirements.txt")

    results, _, _ = analyze(inventory, manifest)

    row = next(item for item in results if item.package == accelerator)
    assert row.direct_dependents == (library.key,)
    assert row.affected_roots == (app.key,)


def test_false_environment_marker_does_not_create_missing_dependency() -> None:
    inventory = Inventory(
        marker_environment={
            **default_environment(),
            "python_version": "3.12",
            "python_full_version": "3.12.0",
            "extra": "",
        }
    )
    app = PackageRecord(PackageKey("pypi", "app"), "1")
    ghost = PackageKey("pypi", "python1-only")
    app.dependencies.append(
        RequirementEdge(
            app.key,
            ghost,
            'python1-only; python_version < "2"',
            "",
            marker='python_version < "2"',
        )
    )
    inventory.add(app)

    results, _, _ = analyze(inventory)

    assert ghost not in inventory.packages
    assert all(item.package != ghost for item in results)


def test_requirements_manifest_preserves_requested_extras(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text("requests[socks]>=2\n", encoding="utf-8")

    manifest = load_manifest(path)

    assert manifest.requirements[0].package == PackageKey("pypi", "requests")
    assert manifest.requirements[0].extras == ("socks",)


def test_reverse_ordered_extra_chain_propagates_without_order_dependence() -> None:
    count = 350
    inventory = Inventory()
    records: list[PackageRecord] = []
    for index in range(count):
        key = PackageKey("pypi", f"extra-{index}")
        record = PackageRecord(key, "1")
        if index + 1 < count:
            target = PackageKey("pypi", f"extra-{index + 1}")
            record.dependencies.append(
                RequirementEdge(
                    key,
                    target,
                    f"{target.name}[fast]",
                    "",
                    marker='extra == "fast"',
                    extras=("fast",),
                )
            )
        records.append(record)

    # Deliberately insert in the worst order for a whole-graph fixpoint scan.
    for record in reversed(records):
        inventory.add(record)

    root = PackageKey("pypi", "extra-0")
    manifest = Manifest(
        (ManifestRequirement(root, "extra-0[fast]", extras=("fast",)),),
        "requirements.txt",
    )
    results, _, _ = analyze(inventory, manifest)

    leaf = next(row for row in results if row.package == PackageKey("pypi", f"extra-{count - 1}"))
    assert leaf.transitive_count == count - 1
    assert leaf.affected_roots == (root,)


def test_unreadable_marker_is_kept_conservatively_with_warning() -> None:
    inventory = Inventory()
    app = PackageRecord(PackageKey("pypi", "app"), "1")
    target = PackageKey("pypi", "maybe-needed")
    app.dependencies.append(
        RequirementEdge(
            app.key,
            target,
            "maybe-needed; definitely not valid marker syntax",
            "",
            marker="definitely not valid marker syntax",
        )
    )
    inventory.add(app)

    results, _, _ = analyze(inventory)

    row = next(item for item in results if item.package == target)
    assert row.version_state == "MISSING"
    assert row.affected_roots == (app.key,)
    assert any("could not evaluate dependency marker" in item for item in inventory.diagnostics)


def test_malformed_python_requirement_preserves_recoverable_edge() -> None:
    from drix.inventory import _python_record

    diagnostics: list[str] = []
    record = _python_record("app", "1", ["numpy => 2"], diagnostics=diagnostics)

    assert len(record.dependencies) == 1
    edge = record.dependencies[0]
    assert edge.target == PackageKey("pypi", "numpy")
    assert edge.specifier == "=> 2"
    assert any("malformed dependency metadata" in item for item in diagnostics)
    assert any("recovered dependency name 'numpy'" in item for item in diagnostics)


def test_unrecoverable_python_requirement_warns_analysis_may_be_incomplete() -> None:
    from drix.inventory import _python_record

    diagnostics: list[str] = []
    record = _python_record("app", "1", ["@@ definitely not a requirement @@"], diagnostics=diagnostics)

    assert record.dependencies == []
    assert any("analysis may be incomplete" in item for item in diagnostics)


def test_malformed_python_requirement_preserves_blast_and_becomes_unknown() -> None:
    from drix.inventory import _python_record

    inventory = Inventory()
    app = _python_record("app", "1", ["numpy => 2"])
    numpy = PackageRecord(PackageKey("pypi", "numpy"), "2.0")
    inventory.add(app)
    inventory.add(numpy)
    for message in app.metadata_diagnostics:
        inventory.diagnostics.append(message)

    results, _, _ = analyze(inventory)
    row = next(item for item in results if item.package == numpy.key)

    assert row.transitive_count == 1
    assert row.direct_dependents == (app.key,)
    assert row.version_state == "UNKNOWN"


def test_malformed_conda_dependency_warns_incomplete_instead_of_silent_drop(tmp_path: Path) -> None:
    import json

    from drix.inventory import _load_conda_prefix

    meta = tmp_path / "conda-meta"
    meta.mkdir()
    (meta / "app-1-0.json").write_text(
        json.dumps({"name": "app", "version": "1", "depends": ["@@ broken dependency @@"]}),
        encoding="utf-8",
    )
    inventory = Inventory()

    _load_conda_prefix(tmp_path, inventory)

    assert any("could not parse Conda dependency" in item for item in inventory.diagnostics)
    assert any("analysis may be incomplete" in item for item in inventory.diagnostics)


def test_editable_requirement_fails_loudly_instead_of_being_skipped(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text("-e .\nrequests>=2\n", encoding="utf-8")

    with pytest.raises(ValueError, match="editable requirement"):
        load_manifest(path)
