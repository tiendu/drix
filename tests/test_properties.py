import random
from collections import deque
from itertools import pairwise

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from drix.analysis import analyze
from drix.constraints import _python_conflict_provable
from drix.model import (
    Inventory,
    Manifest,
    ManifestRequirement,
    PackageKey,
    PackageRecord,
    RequirementEdge,
)


def _shortest_distance(
    forward: dict[PackageKey, set[PackageKey]], start: PackageKey, target: PackageKey
) -> int | None:
    queue = deque([(start, 0)])
    seen = {start}
    while queue:
        node, distance = queue.popleft()
        if node == target:
            return distance
        for nxt in forward.get(node, set()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append((nxt, distance + 1))
    return None


def test_random_graph_analysis_matches_bruteforce_oracle() -> None:
    rng = random.Random(20260916)
    for case in range(30):
        count = rng.randint(6, 22)
        keys = [PackageKey("pypi", f"pkg-{case}-{i}") for i in range(count)]
        inventory = Inventory()
        forward: dict[PackageKey, set[PackageKey]] = {key: set() for key in keys}

        for key in keys:
            record = PackageRecord(key, "1")
            for target in keys:
                if rng.random() < 0.09:
                    record.dependencies.append(RequirementEdge(key, target, target.name, ""))
                    if key != target:  # self-edges are deliberately ignored by drix
                        forward[key].add(target)
            inventory.add(record)

        roots = set(rng.sample(keys, rng.randint(1, min(5, count))))
        manifest = Manifest(
            tuple(ManifestRequirement(key, key.name) for key in roots),
            "requirements.txt",
        )
        results, actual_roots, missing = analyze(inventory, manifest)
        assert actual_roots == roots
        assert missing == []

        reverse: dict[PackageKey, set[PackageKey]] = {key: set() for key in keys}
        for parent, targets in forward.items():
            for target in targets:
                reverse[target].add(parent)

        by_key = {row.package: row for row in results}
        for target in keys:
            seen = {target}
            queue = deque([target])
            while queue:
                node = queue.popleft()
                for parent in reverse[node]:
                    if parent not in seen:
                        seen.add(parent)
                        queue.append(parent)
            transitive = seen - {target}
            row = by_key[target]
            assert set(row.direct_dependents) == reverse[target]
            assert row.transitive_count == len(transitive)
            assert set(row.affected_roots) == ((transitive | {target}) & roots)

            assert row.root_paths == ()

        # Focused analysis computes auditable paths only for the requested package.
        for target in rng.sample(keys, min(3, len(keys))):
            focused, _, _ = analyze(inventory, manifest, focus=target.name)
            focused_row = next(row for row in focused if row.package == target)
            assert {path[0] for path in focused_row.root_paths} == set(focused_row.affected_roots)
            for path in focused_row.root_paths:
                assert path[-1] == target
                for parent, child in pairwise(path):
                    assert child in forward[parent]
                distance = _shortest_distance(forward, path[0], target)
                assert distance is not None
                assert len(path) - 1 == distance

        expected_order = sorted(
            keys,
            key=lambda key: (
                -len(by_key[key].affected_roots),
                -by_key[key].transitive_count,
                -len(by_key[key].direct_dependents),
                key.ecosystem,
                key.name,
            ),
        )
        assert [row.package for row in results] == expected_order


def test_conflict_prover_never_rejects_a_sampled_valid_version() -> None:
    rng = random.Random(8675309)
    specs = []
    for major in range(4):
        specs.extend(
            [
                f">={major}",
                f">{major}",
                f"<={major}",
                f"<{major}",
                f"=={major}.*",
                f"!={major}.*",
                f"~={major}.1",
            ]
        )
        for minor in range(3):
            specs.extend([f"=={major}.{minor}", f"!={major}.{minor}"])

    candidates = [
        Version(f"{major}.{minor}.{patch}")
        for major in range(5)
        for minor in range(10)
        for patch in range(3)
    ]

    for _ in range(1000):
        chosen = rng.sample(specs, rng.randint(2, 5))
        if not _python_conflict_provable(chosen):
            continue
        sets = [SpecifierSet(spec) for spec in chosen]
        satisfying = [version for version in candidates if all(version in spec for spec in sets)]
        assert not satisfying, (chosen, satisfying[:5])


def test_random_extra_propagation_matches_independent_fixpoint() -> None:
    rng = random.Random(424242)
    extra_names = ("fast", "gpu", "io")

    for case in range(100):
        count = rng.randint(5, 14)
        keys = [PackageKey("pypi", f"extra-fuzz-{case}-{i}") for i in range(count)]
        inventory = Inventory()
        all_edges: list[RequirementEdge] = []

        for parent in keys:
            record = PackageRecord(parent, "1")
            for target in keys:
                if parent == target or rng.random() >= 0.11:
                    continue
                marker_extra = rng.choice(extra_names) if rng.random() < 0.45 else None
                requested_extra = rng.choice(extra_names) if rng.random() < 0.35 else None
                edge = RequirementEdge(
                    parent,
                    target,
                    target.name,
                    marker=f'extra == "{marker_extra}"' if marker_extra else "",
                    extras=(requested_extra,) if requested_extra else (),
                )
                record.dependencies.append(edge)
                all_edges.append(edge)
            inventory.add(record)

        roots = set(rng.sample(keys, rng.randint(1, min(4, count))))
        root_extras = {
            root: ({rng.choice(extra_names)} if rng.random() < 0.7 else set())
            for root in roots
        }
        manifest = Manifest(
            tuple(
                ManifestRequirement(
                    root,
                    root.name,
                    extras=tuple(sorted(root_extras[root])),
                )
                for root in roots
            ),
            "requirements.txt",
        )

        # Independent, deliberately inefficient fixpoint oracle.
        selected = {key: set() for key in keys}
        for root, extras in root_extras.items():
            selected[root].update(extras)
        active: set[tuple[PackageKey, PackageKey]] = set()
        changed = True
        while changed:
            changed = False
            for edge in all_edges:
                if edge.marker:
                    required = edge.marker.split('"')[1]
                    if required not in selected[edge.parent]:
                        continue
                pair = (edge.parent, edge.target)
                if pair not in active:
                    active.add(pair)
                    changed = True
                for extra in edge.extras:
                    if extra not in selected[edge.target]:
                        selected[edge.target].add(extra)
                        changed = True

        forward = {key: set() for key in keys}
        reverse = {key: set() for key in keys}
        for parent, target in active:
            forward[parent].add(target)
            reverse[target].add(parent)

        results, actual_roots, missing = analyze(inventory, manifest)
        assert actual_roots == roots
        assert missing == []
        by_key = {row.package: row for row in results}

        for target in keys:
            seen = {target}
            queue = deque([target])
            while queue:
                current = queue.popleft()
                for parent in reverse[current]:
                    if parent not in seen:
                        seen.add(parent)
                        queue.append(parent)
            expected_transitive = seen - {target}
            assert set(by_key[target].direct_dependents) == reverse[target]
            assert by_key[target].transitive_count == len(expected_transitive)
            assert set(by_key[target].affected_roots) == ((seen) & roots)


def test_common_pep440_pair_conflicts_match_bounded_exhaustive_oracle() -> None:
    from itertools import combinations

    specs: list[str] = []
    for major in range(4):
        specs.extend(
            [
                f">={major}",
                f">{major}",
                f"<={major}",
                f"<{major}",
                f"=={major}.*",
                f"!={major}.*",
            ]
        )
        for minor in range(3):
            specs.extend([f"=={major}.{minor}", f"!={major}.{minor}"])

    candidates = [
        Version(f"{major}.{minor}.{patch}")
        for major in range(4)
        for minor in range(5)
        for patch in range(3)
    ]
    bound = SpecifierSet(">=0,<4")

    for left, right in combinations(specs, 2):
        sets = (bound, SpecifierSet(left), SpecifierSet(right))
        satisfiable = any(all(version in spec for spec in sets) for version in candidates)
        assert _python_conflict_provable([">=0,<4", left, right]) is (not satisfiable)
