from __future__ import annotations

from collections import deque
from pathlib import Path

from drix.analysis import _reachability, analyze
from drix.inventory import _merge_python_metadata_into_conda, _python_record, reconcile_inventory
from drix.manifests import load_manifest
from drix.model import (
    Inventory,
    Manifest,
    ManifestRequirement,
    PackageKey,
    PackageRecord,
    RequirementEdge,
)


def test_conda_owned_python_metadata_keeps_requires_dist_extras() -> None:
    inventory = Inventory()
    owner = PackageRecord(PackageKey("conda", "featurepkg"), "1.0", python_names={"featurepkg"})
    inventory.add(owner)
    inventory.add(PackageRecord(PackageKey("conda", "optionaldep"), "2.0", python_names={"optionaldep"}))

    python_metadata = _python_record(
        "featurepkg",
        "1.0",
        ['optionaldep>=2; extra == "fast"'],
    )
    _merge_python_metadata_into_conda(inventory, owner.key, python_metadata)
    reconcile_inventory(inventory)

    manifest = Manifest(
        (
            ManifestRequirement(
                PackageKey("pypi", "featurepkg"),
                "featurepkg[fast]",
                extras=("fast",),
            ),
        ),
        "requirements.txt",
    )
    results, roots, missing = analyze(inventory, manifest)

    assert roots == {owner.key}
    assert missing == []
    optional = next(row for row in results if row.package == PackageKey("conda", "optionaldep"))
    assert optional.direct_dependents == (owner.key,)
    assert optional.affected_roots == (owner.key,)


def test_constraint_file_affects_conflict_but_not_roots(tmp_path: Path) -> None:
    constraints = tmp_path / "constraints.txt"
    constraints.write_text("lib<2\n", encoding="utf-8")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("app\n-c constraints.txt\n", encoding="utf-8")
    manifest = load_manifest(requirements)

    assert manifest.roots == {PackageKey("pypi", "app")}
    lib_constraint = next(req for req in manifest.requirements if req.package.name == "lib")
    assert lib_constraint.is_root is False

    inventory = Inventory()
    app = PackageRecord(PackageKey("pypi", "app"), "1")
    app.dependencies.append(
        RequirementEdge(app.key, PackageKey("pypi", "lib"), "lib>=2", ">=2")
    )
    inventory.add(app)
    inventory.add(PackageRecord(PackageKey("pypi", "lib"), "2.5"))
    reconcile_inventory(inventory)

    results, roots, missing = analyze(inventory, manifest)
    assert roots == {app.key}
    assert missing == []
    lib = next(row for row in results if row.package == PackageKey("pypi", "lib"))
    assert lib.version_state == "CONFLICT"
    assert {item.package.ecosystem for item in lib.conflict_by} == {"manifest", "pypi"}


def test_constraint_only_package_does_not_become_missing_root(tmp_path: Path) -> None:
    (tmp_path / "constraints.txt").write_text("ghost==9\n", encoding="utf-8")
    req = tmp_path / "requirements.txt"
    req.write_text("app\n-cconstraints.txt\n", encoding="utf-8")
    manifest = load_manifest(req)

    inventory = Inventory()
    inventory.add(PackageRecord(PackageKey("pypi", "app"), "1"))
    results, roots, missing = analyze(inventory, manifest)

    assert roots == {PackageKey("pypi", "app")}
    assert missing == []
    assert all(row.package.name != "ghost" for row in results)


def test_pip_tools_hash_continuation_preserves_requirement(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text(
        "numpy==2.1.0 \\\n"
        "    --hash=sha256:aaa \\\n"
        "    --hash=sha256:bbb\n",
        encoding="utf-8",
    )
    manifest = load_manifest(path)
    assert len(manifest.requirements) == 1
    assert manifest.requirements[0].package == PackageKey("pypi", "numpy")
    assert manifest.requirements[0].specifier == "==2.1.0"


def test_requirement_include_cycle_terminates_and_keeps_unique_requirements(tmp_path: Path) -> None:
    a = tmp_path / "requirements.txt"
    b = tmp_path / "other.txt"
    a.write_text("-r other.txt\na>=1\n", encoding="utf-8")
    b.write_text("-r requirements.txt\nb>=1\n", encoding="utf-8")

    manifest = load_manifest(a)
    assert {req.package.name for req in manifest.requirements} == {"a", "b"}


def test_extras_markers_and_cycle_reach_fixpoint() -> None:
    inventory = Inventory()
    a = PackageRecord(PackageKey("pypi", "a"), "1")
    b = PackageRecord(PackageKey("pypi", "b"), "1")
    leaf = PackageRecord(PackageKey("pypi", "leaf"), "1")

    a.dependencies.append(
        RequirementEdge(
            a.key,
            b.key,
            "b[fast]",
            extras=("fast",),
        )
    )
    b.dependencies.extend(
        [
            RequirementEdge(b.key, a.key, "a"),
            RequirementEdge(
                b.key,
                leaf.key,
                'leaf; extra == "fast"',
                marker='extra == "fast"',
            ),
        ]
    )
    for record in (a, b, leaf):
        inventory.add(record)

    manifest = Manifest((ManifestRequirement(a.key, "a"),), "requirements.txt")
    results, roots, missing = analyze(inventory, manifest)
    assert roots == {a.key}
    assert missing == []
    leaf_row = next(row for row in results if row.package == leaf.key)
    assert leaf_row.direct_dependents == (b.key,)
    assert leaf_row.affected_roots == (a.key,)


def _brute_ancestors(
    forward: dict[PackageKey, set[PackageKey]], target: PackageKey
) -> set[PackageKey]:
    reverse = {key: set() for key in forward}
    for parent, targets in forward.items():
        for child in targets:
            reverse[child].add(parent)
    seen = {target}
    queue = deque([target])
    while queue:
        node = queue.popleft()
        for parent in reverse[node]:
            if parent not in seen:
                seen.add(parent)
                queue.append(parent)
    return seen


def test_exhaustive_four_node_reachability_matches_bruteforce() -> None:
    keys = tuple(PackageKey("pypi", f"n{i}") for i in range(4))
    possible_edges = tuple((a, b) for a in keys for b in keys if a != b)

    # Every directed graph on four labelled nodes: 2^12 = 4096 graphs.
    for mask in range(1 << len(possible_edges)):
        forward = {key: set() for key in keys}
        for bit, (parent, target) in enumerate(possible_edges):
            if mask & (1 << bit):
                forward[parent].add(target)

        nodes, node_index, component_of, ancestor_bits, _ = _reachability(forward)
        for target in keys:
            bits = ancestor_bits[component_of[target]]
            actual = {
                node
                for node in nodes
                if bits & (1 << node_index[node])
            }
            assert actual == _brute_ancestors(forward, target)


def test_same_named_conda_package_without_python_metadata_does_not_satisfy_pip_dependency() -> None:
    inventory = Inventory()
    app = PackageRecord(PackageKey("pypi", "app"), "1")
    app.dependencies.append(
        RequirementEdge(app.key, PackageKey("pypi", "native-lib"), "native-lib>=1", ">=1")
    )
    inventory.add(app)
    # Same spelling alone is not evidence that this native Conda package provides a
    # Python distribution named native-lib.
    inventory.add(PackageRecord(PackageKey("conda", "native-lib"), "1"))

    reconcile_inventory(inventory)
    results, _, _ = analyze(inventory)

    missing = next(row for row in results if row.package == PackageKey("pypi", "native-lib"))
    assert missing.version_state == "MISSING"
    assert missing.affected_roots == (app.key,)


def test_conda_file_metadata_can_map_different_package_and_python_names() -> None:
    from drix.inventory import _python_names_from_conda_files

    aliases = _python_names_from_conda_files(
        [
            "lib/python3.12/site-packages/torch-2.5.1.dist-info/METADATA",
            "lib/python3.12/site-packages/torch-2.5.1.dist-info/RECORD",
        ]
    )
    assert aliases == {"torch"}

    inventory = Inventory()
    conda = PackageRecord(PackageKey("conda", "pytorch"), "2.5.1", python_names=aliases)
    inventory.add(conda)
    manifest = Manifest(
        (ManifestRequirement(PackageKey("pypi", "torch"), "torch>=2"),),
        "requirements.txt",
    )
    results, roots, missing = analyze(inventory, manifest)
    assert roots == {conda.key}
    assert missing == []
    row = next(item for item in results if item.package == conda.key)
    assert row.version_state == "OK"


def test_pip_specifier_on_conda_owned_python_package_keeps_pep440_semantics() -> None:
    inventory = Inventory()
    numpy = PackageRecord(PackageKey("conda", "numpy"), "1.26.4", python_names={"numpy"})
    app = PackageRecord(PackageKey("pypi", "app"), "1")
    app.dependencies.append(
        RequirementEdge(
            app.key,
            PackageKey("pypi", "numpy"),
            "numpy~=1.26",
            "~=1.26",
        )
    )
    inventory.add(numpy)
    inventory.add(app)
    reconcile_inventory(inventory)

    results, _, _ = analyze(inventory)
    row = next(item for item in results if item.package == numpy.key)
    assert row.version_state == "OK"
    assert row.contributors[0].ecosystem == "pypi"


def test_mixed_pip_and_conda_constraints_can_prove_conflict() -> None:
    inventory = Inventory()
    shared = PackageRecord(PackageKey("conda", "shared"), "2.0", python_names={"shared"})
    native = PackageRecord(PackageKey("conda", "native"), "1")
    native.dependencies.append(
        RequirementEdge(native.key, shared.key, "shared >=2", ">=2")
    )
    pyapp = PackageRecord(PackageKey("pypi", "pyapp"), "1")
    pyapp.dependencies.append(
        RequirementEdge(pyapp.key, PackageKey("pypi", "shared"), "shared<2", "<2")
    )
    for record in (shared, native, pyapp):
        inventory.add(record)
    reconcile_inventory(inventory)

    results, _, _ = analyze(inventory)
    row = next(item for item in results if item.package == shared.key)
    assert row.version_state == "CONFLICT"
    assert {item.ecosystem for item in row.conflict_by} == {"conda", "pypi"}


def test_unknown_conda_build_pin_does_not_corrupt_pip_semantics() -> None:
    from drix.constraints import analyze_constraints
    from drix.model import ConstraintContributor

    result = analyze_constraints(
        "conda",
        "1.26.4",
        (
            ConstraintContributor(
                PackageKey("manifest", "environment.yml"),
                "numpy=1.26=py312_0",
                "=1.26=py312_0",
                "conda",
            ),
            ConstraintContributor(
                PackageKey("pypi", "app"),
                "numpy~=1.26",
                "~=1.26",
                "pypi",
            ),
        ),
    )
    assert result.state == "UNKNOWN"


def test_large_chain_keeps_counts_without_materializing_quadratic_lists() -> None:
    count = 5000
    inventory = Inventory()
    for index in range(count):
        key = PackageKey("pypi", f"scale-{index}")
        record = PackageRecord(key, "1")
        if index + 1 < count:
            target = PackageKey("pypi", f"scale-{index + 1}")
            record.dependencies.append(RequirementEdge(key, target, target.name))
        inventory.add(record)

    root = PackageKey("pypi", "scale-0")
    manifest = Manifest((ManifestRequirement(root, root.name),), "requirements.txt")
    results, _, _ = analyze(inventory, manifest)
    leaf = next(row for row in results if row.package == PackageKey("pypi", f"scale-{count - 1}"))

    assert leaf.transitive_count == count - 1
    assert leaf.transitive_dependents == ()

    focused, _, _ = analyze(inventory, manifest, focus=leaf.package.name)
    focused_leaf = next(row for row in focused if row.package == leaf.package)
    assert len(focused_leaf.transitive_dependents) == count - 1


def test_load_inventory_merges_conda_owned_python_metadata_and_keeps_pip_overlay(
    tmp_path: Path, monkeypatch
) -> None:
    import json

    import drix.inventory as inventory_module

    meta = tmp_path / "conda-meta"
    meta.mkdir()
    (meta / "pytorch-2.5.1-0.json").write_text(
        json.dumps(
            {
                "name": "pytorch",
                "version": "2.5.1",
                "depends": [],
                "files": [
                    "lib/python3.12/site-packages/torch-2.5.1.dist-info/METADATA"
                ],
            }
        ),
        encoding="utf-8",
    )
    fake_python = tmp_path / "bin" / "python"
    fake_python.parent.mkdir()
    fake_python.write_text("", encoding="utf-8")

    conda_owned = inventory_module._python_record(
        "torch", "2.5.1", ["typing-extensions>=4"]
    )
    pip_overlay = inventory_module._python_record(
        "overlay", "1.0", ["torch>=2"]
    )

    monkeypatch.setattr(inventory_module, "_python_for_prefix", lambda prefix: fake_python)
    monkeypatch.setattr(
        inventory_module,
        "_python_records_external",
        lambda python: (
            [(conda_owned, False), (pip_overlay, True)],
            {
                "python_version": "3.12",
                "python_full_version": "3.12.0",
                "extra": "",
            },
        ),
    )

    inventory = inventory_module.load_inventory(tmp_path)

    torch_key = PackageKey("conda", "pytorch")
    assert PackageKey("pypi", "torch") not in inventory.packages
    assert "torch" in inventory.packages[torch_key].python_names
    assert any(edge.target.name == "typing-extensions" for edge in inventory.packages[torch_key].dependencies)
    overlay = inventory.packages[PackageKey("pypi", "overlay")]
    assert overlay.dependencies[0].target == torch_key


def test_conflict_witness_drops_unrelated_constraints_even_when_core_needs_more_than_four() -> None:
    from drix.constraints import analyze_constraints
    from drix.model import ConstraintContributor

    contributors = [
        ConstraintContributor(PackageKey("pypi", "bound"), "target>=0,<5", ">=0,<5", "pypi")
    ]
    for major in range(5):
        contributors.append(
            ConstraintContributor(
                PackageKey("pypi", f"exclude-{major}"),
                f"target!={major}.*",
                f"!={major}.*",
                "pypi",
            )
        )
    contributors.extend(
        [
            ConstraintContributor(PackageKey("pypi", "noise-a"), "target>=0", ">=0", "pypi"),
            ConstraintContributor(PackageKey("pypi", "noise-b"), "target<99", "<99", "pypi"),
        ]
    )

    result = analyze_constraints("pypi", "2.0", tuple(contributors))
    assert result.state == "CONFLICT"
    names = {item.package.name for item in result.conflict_by}
    assert "noise-a" not in names
    assert "noise-b" not in names
    assert names == {"bound", *(f"exclude-{major}" for major in range(5))}


def test_requirement_include_variants_and_inline_comments(tmp_path: Path) -> None:
    (tmp_path / "base.txt").write_text("basepkg>=1\n", encoding="utf-8")
    (tmp_path / "constraints.txt").write_text("lib<3\n", encoding="utf-8")
    root = tmp_path / "requirements.txt"
    root.write_text(
        "--requirement=base.txt  # roots\n"
        "--constraint=constraints.txt  # pins\n"
        "app\n",
        encoding="utf-8",
    )

    manifest = load_manifest(root)
    assert manifest.roots == {PackageKey("pypi", "basepkg"), PackageKey("pypi", "app")}
    constraint = next(req for req in manifest.requirements if req.package.name == "lib")
    assert constraint.is_root is False


def test_conda_python_alias_detection_handles_windows_paths() -> None:
    from drix.inventory import _python_names_from_conda_files

    assert _python_names_from_conda_files(
        [r"Lib\\site-packages\\scikit_learn-1.6.0.dist-info\\METADATA"]
    ) == {"scikit-learn"}


def test_active_virtualenv_is_target_for_standalone_discovery(tmp_path: Path, monkeypatch) -> None:
    import drix.inventory as inventory_module

    python = tmp_path / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path))
    monkeypatch.delenv("CONDA_PREFIX", raising=False)

    active = inventory_module._active_environment_prefix()
    assert active == tmp_path.resolve()
    assert inventory_module._target_python(None, active) == python.resolve()


def test_frozen_binary_uses_path_python_not_its_embedded_interpreter(monkeypatch, tmp_path: Path) -> None:
    import drix.inventory as inventory_module

    external = tmp_path / "python3"
    external.write_text("", encoding="utf-8")
    monkeypatch.setattr(inventory_module, "_is_frozen", lambda: True)
    monkeypatch.setattr(inventory_module.shutil, "which", lambda command: str(external) if command == "python3" else None)

    assert inventory_module._target_python(None, None) == external.resolve()


def test_active_conda_prefix_drives_python_metadata_source(tmp_path: Path, monkeypatch) -> None:
    import json

    import drix.inventory as inventory_module

    meta = tmp_path / "conda-meta"
    meta.mkdir()
    (meta / "numpy-2.0-0.json").write_text(
        json.dumps({"name": "numpy", "version": "2.0", "depends": [], "files": []}),
        encoding="utf-8",
    )
    python = tmp_path / "bin" / "python"
    python.parent.mkdir()
    python.write_text("", encoding="utf-8")
    monkeypatch.setenv("CONDA_PREFIX", str(tmp_path))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    called: list[Path] = []

    def fake_external(target: Path):
        called.append(target)
        return [], {"python_version": "3.12", "python_full_version": "3.12.0", "extra": ""}

    monkeypatch.setattr(inventory_module, "_python_records_external", fake_external)
    inventory_module.load_inventory()

    assert called == [python.resolve()]


def test_frozen_inventory_never_uses_embedded_metadata(monkeypatch, tmp_path: Path) -> None:
    import drix.inventory as inventory_module

    external = tmp_path / "python3"
    external.write_text("", encoding="utf-8")
    monkeypatch.delenv("CONDA_PREFIX", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setattr(inventory_module, "_is_frozen", lambda: True)
    monkeypatch.setattr(inventory_module, "_python_from_path", lambda: external)
    monkeypatch.setattr(
        inventory_module,
        "_python_records_current",
        lambda: (_ for _ in ()).throw(AssertionError("embedded metadata must not be inspected")),
    )
    monkeypatch.setattr(
        inventory_module,
        "_python_records_external",
        lambda python: ([], {"python_version": "3.12", "python_full_version": "3.12.0", "extra": ""}),
    )

    inventory = inventory_module.load_inventory()
    assert inventory.source == "current environment"


def test_frozen_external_process_restores_original_library_paths(monkeypatch) -> None:
    import drix.inventory as inventory_module

    monkeypatch.setattr(inventory_module, "_is_frozen", lambda: True)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEI/bundled")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/local/lib")
    monkeypatch.setenv("LIBPATH", "/tmp/_MEI/bundled-aix")
    monkeypatch.delenv("LIBPATH_ORIG", raising=False)

    env = inventory_module._external_process_environment()

    assert env["LD_LIBRARY_PATH"] == "/usr/local/lib"
    assert "LIBPATH" not in env


def test_external_python_receives_sanitized_environment(monkeypatch, tmp_path: Path) -> None:
    import json

    import drix.inventory as inventory_module

    python = tmp_path / "python"
    python.write_text("", encoding="utf-8")
    captured: dict[str, object] = {}

    class Result:
        stdout = json.dumps(
            {
                "marker_environment": {
                    "python_version": "3.12",
                    "python_full_version": "3.12.0",
                    "extra": "",
                },
                "distributions": [],
            }
        )

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return Result()

    monkeypatch.setattr(inventory_module, "_is_frozen", lambda: True)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEI")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/system/lib")
    monkeypatch.setattr(inventory_module.subprocess, "run", fake_run)

    inventory_module._python_records_external(python)

    assert captured["env"]["LD_LIBRARY_PATH"] == "/system/lib"


def test_explicit_prefix_without_python_does_not_fall_back_to_drix_python(tmp_path: Path) -> None:
    import drix.inventory as inventory_module

    assert inventory_module._target_python(tmp_path, None) is None


def test_active_prefix_without_python_does_not_fall_back_to_drix_python(tmp_path: Path) -> None:
    import drix.inventory as inventory_module

    assert inventory_module._target_python(None, tmp_path) is None
