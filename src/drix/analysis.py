from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterator
from pathlib import Path

from packaging.markers import Marker

from drix.constraints import analyze_constraints
from drix.model import (
    ConstraintContributor,
    Inventory,
    Manifest,
    PackageKey,
    PackageRecord,
    PackageRisk,
    RequirementEdge,
    default_marker_environment,
    name_matches,
)


def _graph(
    inventory: Inventory,
    active_edges: dict[PackageKey, tuple[RequirementEdge, ...]],
) -> tuple[dict[PackageKey, set[PackageKey]], dict[PackageKey, set[PackageKey]]]:
    forward: dict[PackageKey, set[PackageKey]] = {key: set() for key in inventory.packages}
    reverse: dict[PackageKey, set[PackageKey]] = defaultdict(set)
    for edges in active_edges.values():
        for edge in edges:
            if edge.parent == edge.target:
                continue
            forward.setdefault(edge.parent, set()).add(edge.target)
            forward.setdefault(edge.target, set())
            reverse[edge.target].add(edge.parent)
    return forward, reverse


def _edge_is_active(
    edge: RequirementEdge,
    selected_extras: set[str],
    marker_environment: dict[str, str],
    diagnostics: list[str],
) -> bool:
    if not edge.marker:
        return True
    try:
        marker = Marker(edge.marker)
        candidates = ["", *sorted(selected_extras)]
        for extra in candidates:
            environment = dict(marker_environment)
            environment["extra"] = extra
            if marker.evaluate(environment):
                return True
        return False
    except Exception as error:  # noqa: BLE001 - external marker metadata must fail conservative.
        message = f"could not evaluate dependency marker {edge.marker!r} from {edge.parent.name}: {error}"
        if message not in diagnostics:
            diagnostics.append(message)
        # Risk inspection should fail conservative: an unreadable marker must not
        # silently erase a dependency edge and understate blast radius.
        return True


def _active_edges(
    inventory: Inventory,
    manifest: Manifest | None,
    resolved_roots: dict[PackageKey, PackageKey],
) -> dict[PackageKey, tuple[RequirementEdge, ...]]:
    """Activate markers/extras with an incremental fixpoint.

    Every package is evaluated once initially. A package is revisited only when it
    receives a newly selected extra. This makes extra propagation independent of
    metadata iteration order and avoids repeatedly rescanning the whole graph.
    """

    marker_environment = (
        dict(inventory.marker_environment)
        if inventory.marker_environment
        else default_marker_environment()
    )
    marker_environment["extra"] = ""
    selected_extras: dict[PackageKey, set[str]] = defaultdict(set)
    if manifest is not None:
        for requirement in manifest.requirements:
            if not requirement.is_root:
                continue
            resolved = resolved_roots.get(requirement.package)
            if resolved is not None:
                selected_extras[resolved].update(requirement.extras)

    active: dict[PackageKey, list[RequirementEdge]] = defaultdict(list)
    active_set: set[RequirementEdge] = set()

    queue = deque(inventory.packages)
    queued = set(inventory.packages)
    while queue:
        parent = queue.popleft()
        queued.discard(parent)
        record = inventory.packages.get(parent)
        if record is None:
            continue
        extras = selected_extras.get(parent, set())
        for edge in record.dependencies:
            if edge in active_set:
                continue
            if not _edge_is_active(
                edge, extras, marker_environment, inventory.diagnostics
            ):
                continue
            active_set.add(edge)
            active[edge.parent].append(edge)

            if not edge.extras:
                continue
            target_extras = selected_extras[edge.target]
            before = len(target_extras)
            target_extras.update(edge.extras)
            if (
                len(target_extras) != before
                and edge.target in inventory.packages
                and edge.target not in queued
            ):
                queue.append(edge.target)
                queued.add(edge.target)

    return {parent: tuple(edges) for parent, edges in active.items()}


def _materialize_active_missing(
    inventory: Inventory, active_edges: dict[PackageKey, tuple[RequirementEdge, ...]]
) -> None:
    for edges in active_edges.values():
        for edge in edges:
            inventory.packages.setdefault(
                edge.target,
                PackageRecord(key=edge.target, installed=False),
            )


def _scc_data(
    forward: dict[PackageKey, set[PackageKey]],
) -> tuple[
    list[set[PackageKey]],
    dict[PackageKey, int],
    list[set[int]],
    list[int],
]:
    """Condense the graph into SCCs using iterative Kosaraju traversal."""

    nodes = tuple(sorted(forward))
    neighbors = {node: tuple(sorted(forward.get(node, set()))) for node in nodes}

    # First pass: finishing order in the original graph. Iterative traversal avoids
    # crashing on dependency chains deeper than Python's recursion limit.
    visited: set[PackageKey] = set()
    finish: list[PackageKey] = []
    for start in nodes:
        if start in visited:
            continue
        visited.add(start)
        stack: list[tuple[PackageKey, Iterator[PackageKey]]] = [
            (start, iter(neighbors[start]))
        ]
        while stack:
            node, iterator = stack[-1]
            try:
                target = next(iterator)
            except StopIteration:
                finish.append(node)
                stack.pop()
                continue
            if target not in visited:
                visited.add(target)
                stack.append((target, iter(neighbors.get(target, ()))))

    transpose: dict[PackageKey, set[PackageKey]] = {node: set() for node in nodes}
    for parent, targets in forward.items():
        for target in targets:
            transpose.setdefault(target, set()).add(parent)

    # Second pass: SCC membership in reverse finishing order.
    component_of: dict[PackageKey, int] = {}
    components: list[set[PackageKey]] = []
    for start in reversed(finish):
        if start in component_of:
            continue
        component_id = len(components)
        component: set[PackageKey] = set()
        component_stack: list[PackageKey] = [start]
        component_of[start] = component_id
        while component_stack:
            component_node = component_stack.pop()
            component.add(component_node)
            for target in transpose.get(component_node, set()):
                if target not in component_of:
                    component_of[target] = component_id
                    component_stack.append(target)
        components.append(component)

    dag: list[set[int]] = [set() for _ in components]
    indegree = [0] * len(components)
    for parent, targets in forward.items():
        parent_component = component_of[parent]
        for target in targets:
            target_component = component_of[target]
            if parent_component == target_component:
                continue
            if target_component not in dag[parent_component]:
                dag[parent_component].add(target_component)
                indegree[target_component] += 1

    return components, component_of, dag, indegree


def _reachability(
    forward: dict[PackageKey, set[PackageKey]],
) -> tuple[
    tuple[PackageKey, ...],
    dict[PackageKey, int],
    dict[PackageKey, int],
    list[int],
    set[PackageKey],
]:
    """Return ancestor bitsets for every SCC plus structural roots.

    For an edge ``A -> B`` (A requires B), the ancestor set of B is exactly the set
    of packages potentially affected when B changes. SCC condensation lets us compute
    those sets once over a DAG instead of doing one reverse BFS per package.
    """

    nodes = tuple(sorted(forward))
    node_index = {node: index for index, node in enumerate(nodes)}
    components, component_of, dag, indegree = _scc_data(forward)

    member_bits = [0] * len(components)
    for component_id, component in enumerate(components):
        bits = 0
        for node in component:
            bits |= 1 << node_index[node]
        member_bits[component_id] = bits

    ancestor_bits = member_bits[:]
    remaining = indegree[:]
    queue = deque(component_id for component_id, degree in enumerate(remaining) if degree == 0)
    structural_roots: set[PackageKey] = set()
    for component_id in queue:
        structural_roots.update(components[component_id])

    processed = 0
    while queue:
        component_id = queue.popleft()
        processed += 1
        for target_component in dag[component_id]:
            ancestor_bits[target_component] |= ancestor_bits[component_id]
            remaining[target_component] -= 1
            if remaining[target_component] == 0:
                queue.append(target_component)

    if processed != len(components):  # defensive: condensation must always be a DAG
        raise RuntimeError("internal error: SCC condensation produced a cycle")

    return nodes, node_index, component_of, ancestor_bits, structural_roots


def _keys_from_bits(bits: int, nodes: tuple[PackageKey, ...]) -> tuple[PackageKey, ...]:
    result: list[PackageKey] = []
    while bits:
        lowest = bits & -bits
        index = lowest.bit_length() - 1
        result.append(nodes[index])
        bits ^= lowest
    return tuple(result)


def infer_roots(inventory: Inventory) -> set[PackageKey]:
    active_edges = _active_edges(inventory, None, {})
    _materialize_active_missing(inventory, active_edges)
    forward, _ = _graph(inventory, active_edges)
    _, _, _, _, structural = _reachability(forward)
    return structural


def _reverse_walk(
    target: PackageKey,
    reverse: dict[PackageKey, set[PackageKey]],
) -> dict[PackageKey, PackageKey]:
    """Return one shortest next hop toward target for every reverse-reachable node."""

    seen: set[PackageKey] = {target}
    next_hop: dict[PackageKey, PackageKey] = {}
    queue = deque([target])
    while queue:
        current = queue.popleft()
        for parent in sorted(reverse.get(current, set())):
            if parent in seen:
                continue
            seen.add(parent)
            next_hop[parent] = current
            queue.append(parent)
    return next_hop


def _root_paths(
    target: PackageKey,
    affected_roots: set[PackageKey],
    next_hop: dict[PackageKey, PackageKey],
) -> tuple[tuple[PackageKey, ...], ...]:
    paths: list[tuple[PackageKey, ...]] = []
    for root in sorted(affected_roots):
        if root == target:
            paths.append((root,))
            continue
        path = [root]
        current = root
        visited = {root}
        while current != target:
            next_node = next_hop.get(current)
            if next_node is None or next_node in visited:
                path = []
                break
            current = next_node
            path.append(current)
            visited.add(current)
        if path:
            paths.append(tuple(path))
    return tuple(paths)


def _contributors_by_target(
    active_edges: dict[PackageKey, tuple[RequirementEdge, ...]],
) -> dict[PackageKey, list[ConstraintContributor]]:
    contributors: dict[PackageKey, list[ConstraintContributor]] = defaultdict(list)
    for edges in active_edges.values():
        for edge in edges:
            if edge.parent == edge.target:
                continue
            contributors[edge.target].append(
                ConstraintContributor(
                    package=edge.parent,
                    raw=edge.raw,
                    specifier=edge.specifier,
                    ecosystem=edge.constraint_ecosystem,
                )
            )
    return contributors


def _materialize_missing_manifest_roots(inventory: Inventory, manifest: Manifest | None) -> None:
    if manifest is None:
        return
    installed = {key for key, record in inventory.packages.items() if record.installed}
    conda_by_python_name: dict[str, list[PackageKey]] = defaultdict(list)
    for key in installed:
        if key.ecosystem == "conda":
            for python_name in inventory.packages[key].python_names:
                conda_by_python_name[python_name].append(key)

    for requested in manifest.roots:
        if requested in installed:
            continue
        # A Python distribution can legitimately be supplied by Conda. The reverse is
        # not true: a Conda dependency/root is not satisfied by a same-named pip wheel.
        if requested.ecosystem == "pypi":
            conda_matches = conda_by_python_name.get(requested.name, [])
            if len(conda_matches) == 1:
                continue
        inventory.packages.setdefault(
            requested, PackageRecord(key=requested, installed=False)
        )


def _resolve_manifest_roots(
    inventory: Inventory, manifest: Manifest
) -> tuple[set[PackageKey], list[str], dict[PackageKey, PackageKey]]:
    roots: set[PackageKey] = set()
    missing: list[str] = []
    resolved: dict[PackageKey, PackageKey] = {}
    installed = {key for key, record in inventory.packages.items() if record.installed}
    conda_by_python_name: dict[str, list[PackageKey]] = defaultdict(list)
    for key in installed:
        if key.ecosystem == "conda":
            for python_name in inventory.packages[key].python_names:
                conda_by_python_name[python_name].append(key)

    def resolve(requested: PackageKey) -> PackageKey:
        if requested in installed:
            return requested
        if requested.ecosystem == "pypi":
            conda_matches = conda_by_python_name.get(requested.name, [])
            if len(conda_matches) == 1:
                return conda_matches[0]
        return requested

    for requirement in manifest.requirements:
        resolved.setdefault(requirement.package, resolve(requirement.package))

    for requested in manifest.roots:
        target = resolved.get(requested, requested)
        roots.add(target)
        if target not in installed:
            missing.append(f"{requested.ecosystem}:{requested.name}")
    return roots, missing, resolved


def analyze(
    inventory: Inventory,
    manifest: Manifest | None = None,
    *,
    focus: str | None = None,
) -> tuple[list[PackageRisk], set[PackageKey], list[str]]:
    _materialize_missing_manifest_roots(inventory, manifest)
    if manifest is None:
        missing_roots: list[str] = []
        resolved_roots: dict[PackageKey, PackageKey] = {}
    else:
        roots, missing_roots, resolved_roots = _resolve_manifest_roots(inventory, manifest)

    active_edges = _active_edges(inventory, manifest, resolved_roots)
    _materialize_active_missing(inventory, active_edges)
    forward, reverse = _graph(inventory, active_edges)
    nodes, node_index, component_of, ancestor_bits, structural_roots = _reachability(forward)

    if manifest is None:
        roots = set(inventory.declared_roots) if inventory.declared_roots else structural_roots

    root_bits = 0
    for root in roots:
        root_bits |= 1 << node_index[root]

    manifest_source = PackageKey("manifest", Path(manifest.source).name) if manifest else None
    focused_name = focus
    edge_contributors = _contributors_by_target(active_edges)
    results: list[PackageRisk] = []

    for key, record in inventory.packages.items():
        component_bits = ancestor_bits[component_of[key]]
        transitive_bits = component_bits & ~(1 << node_index[key])
        affected_bits = component_bits & root_bits
        is_focused = focused_name is not None and name_matches(key, focused_name)
        transitive_count = transitive_bits.bit_count()
        transitive = _keys_from_bits(transitive_bits, nodes) if is_focused else ()
        affected_roots = _keys_from_bits(affected_bits, nodes)
        direct = tuple(sorted(reverse.get(key, set())))

        root_paths: tuple[tuple[PackageKey, ...], ...] = ()
        if is_focused:
            next_hop = _reverse_walk(key, reverse)
            root_paths = _root_paths(key, set(affected_roots), next_hop)

        contributors = list(edge_contributors.get(key, ()))
        if manifest and manifest_source:
            for requirement in manifest.requirements:
                if resolved_roots.get(requirement.package) == key:
                    contributors.append(
                        ConstraintContributor(
                            package=manifest_source,
                            raw=requirement.raw,
                            specifier=requirement.specifier,
                            ecosystem=requirement.package.ecosystem,
                        )
                    )
        contributors_tuple = tuple(
            sorted(contributors, key=lambda c: (c.package.ecosystem, c.package.name, c.raw))
        )
        constraint = analyze_constraints(
            key.ecosystem,
            record.version,
            contributors_tuple,
            installed=record.installed,
        )
        planned_change = inventory.planned_changes.get(key)
        results.append(
            PackageRisk(
                package=key,
                version=record.version,
                installed=record.installed,
                affected_roots=affected_roots,
                transitive_dependents=transitive,
                transitive_count=transitive_count,
                direct_dependents=direct,
                root_paths=root_paths,
                version_state=constraint.state,
                contributors=contributors_tuple,
                violated_by=constraint.violated_by,
                conflict_by=constraint.conflict_by,
                planned_action=planned_change.action if planned_change else None,
                candidate_version=planned_change.candidate_version if planned_change else None,
                held=key in inventory.held_packages,
            )
        )

    results.sort(
        key=lambda row: (
            -len(row.affected_roots),
            -row.transitive_count,
            -len(row.direct_dependents),
            row.package.ecosystem,
            row.package.name,
        )
    )
    return results, roots, missing_roots
