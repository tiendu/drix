from __future__ import annotations

import re
import shutil
import subprocess
from collections import defaultdict
from dataclasses import dataclass

from drix.constraints import apt_satisfies
from drix.runtime import external_process_environment
from drix.model import (
    Inventory,
    PackageKey,
    PackageRecord,
    PlannedChange,
    RequirementEdge,
    apt_base_name,
    normalize_apt_name,
)

_APT_ATOM = re.compile(
    r"^\s*([a-z0-9][a-z0-9+.-]*)(?::([a-z0-9-]+))?"
    r"\s*(?:\((<<|<=|=|>=|>>)\s*([^)]+)\))?"
)
_INSTALL = re.compile(r"^Inst\s+(\S+)(?:\s+\[([^]]+)\])?\s+\((\S+)")
_REMOVE = re.compile(r"^Remv\s+(\S+)(?:\s+\[([^]]+)\])?")


@dataclass(frozen=True)
class _AptAtom:
    name: str
    qualifier: str
    specifier: str
    raw: str


@dataclass(frozen=True)
class _ResolvedCandidate:
    key: PackageKey
    apply_specifier: bool


def _command(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        check=check,
        capture_output=True,
        text=True,
        timeout=60,
        env=external_process_environment(),
    )


def _require_apt_tools() -> None:
    missing = [name for name in ("dpkg-query", "dpkg") if shutil.which(name) is None]
    if missing:
        raise OSError("APT/dpkg inspection is unavailable; missing: " + ", ".join(missing))


def _native_architecture() -> str:
    if shutil.which("dpkg") is None:
        return ""
    proc = _command("dpkg", "--print-architecture", check=False)
    return proc.stdout.strip().lower() if proc.returncode == 0 else ""


def _parse_atom(text: str) -> _AptAtom | None:
    match = _APT_ATOM.match(text)
    if not match:
        return None
    name = normalize_apt_name(match.group(1))
    qualifier = (match.group(2) or "").lower()
    operator = match.group(3) or ""
    version = (match.group(4) or "").strip()
    specifier = f"{operator} {version}" if operator and version else ""
    return _AptAtom(name, qualifier, specifier, text.strip())


def _split_dependency_groups(text: str) -> list[list[_AptAtom]]:
    groups: list[list[_AptAtom]] = []
    for group_text in text.split(","):
        atoms = [atom for raw in group_text.split("|") if (atom := _parse_atom(raw))]
        if atoms:
            groups.append(atoms)
    return groups


def _parse_provides(text: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for group in _split_dependency_groups(text):
        for atom in group:
            version = ""
            if atom.specifier.startswith("= "):
                version = atom.specifier[2:].strip()
            result.append((atom.name, version))
    return result


def _installed_indexes(
    inventory: Inventory,
) -> tuple[dict[str, list[PackageKey]], dict[str, list[tuple[PackageKey, str]]]]:
    by_base: dict[str, list[PackageKey]] = defaultdict(list)
    providers: dict[str, list[tuple[PackageKey, str]]] = defaultdict(list)
    for key, record in inventory.packages.items():
        if not record.installed or key.ecosystem != "apt":
            continue
        by_base[apt_base_name(key.name)].append(key)
        for provided_name, provided_version in record.provided_names.items():
            providers[normalize_apt_name(provided_name)].append((key, provided_version))
    return by_base, providers


def _resolve_named_candidates(
    atom: _AptAtom,
    inventory: Inventory,
    by_base: dict[str, list[PackageKey]],
    providers: dict[str, list[tuple[PackageKey, str]]],
    native_arch: str,
) -> list[_ResolvedCandidate]:
    result: list[_ResolvedCandidate] = []
    direct = by_base.get(atom.name, [])

    if atom.qualifier and atom.qualifier not in {"any", "native"}:
        exact = PackageKey("apt", f"{atom.name}:{atom.qualifier}")
        if exact in inventory.packages and inventory.packages[exact].installed:
            direct = [exact]
        else:
            direct = []
    elif atom.qualifier == "native" and native_arch:
        native = PackageKey("apt", f"{atom.name}:{native_arch}")
        if native in inventory.packages and inventory.packages[native].installed:
            direct = [native]
    elif atom.qualifier != "any" and len(direct) > 1 and native_arch:
        native = PackageKey("apt", f"{atom.name}:{native_arch}")
        if native in direct:
            direct = [native]

    for key in direct:
        result.append(_ResolvedCandidate(key, True))

    for key, provided_version in providers.get(atom.name, []):
        if atom.specifier:
            if not provided_version:
                continue
            satisfied = apt_satisfies(provided_version, atom.specifier)
            if satisfied is not True:
                continue
        result.append(_ResolvedCandidate(key, False))

    dedup: dict[PackageKey, _ResolvedCandidate] = {}
    for candidate in result:
        previous = dedup.get(candidate.key)
        if previous is None or candidate.apply_specifier:
            dedup[candidate.key] = candidate
    return list(dedup.values())


def _candidate_satisfies(
    candidate: _ResolvedCandidate,
    atom: _AptAtom,
    inventory: Inventory,
) -> bool | None:
    if not atom.specifier or not candidate.apply_specifier:
        return True
    version = inventory.packages[candidate.key].version
    if version is None:
        return None
    return apt_satisfies(version, atom.specifier)


def _edge_for_group(
    parent: PackageKey,
    atoms: list[_AptAtom],
    inventory: Inventory,
    by_base: dict[str, list[PackageKey]],
    providers: dict[str, list[tuple[PackageKey, str]]],
    native_arch: str,
) -> RequirementEdge | None:
    resolved: list[tuple[_AptAtom, _ResolvedCandidate, bool | None]] = []
    for atom in atoms:
        for candidate in _resolve_named_candidates(atom, inventory, by_base, providers, native_arch):
            resolved.append((atom, candidate, _candidate_satisfies(candidate, atom, inventory)))

    viable = [(atom, candidate) for atom, candidate, ok in resolved if ok is True]
    viable_keys = {candidate.key for _, candidate in viable}
    if len(viable_keys) > 1:
        # The dependency has more than one currently-installed way to remain satisfied.
        # No single package is a hard blast edge, so adding all alternatives would
        # overstate risk.
        return None
    if len(viable_keys) == 1:
        key = next(iter(viable_keys))
        atom, candidate = next((atom, candidate) for atom, candidate in viable if candidate.key == key)
        specifier = atom.specifier if candidate.apply_specifier else ""
        raw = " | ".join(item.raw for item in atoms)
        return RequirementEdge(parent, key, raw, specifier, constraint_ecosystem="apt")

    resolved_keys = {candidate.key for _, candidate, _ in resolved}
    if len(resolved_keys) == 1:
        key = next(iter(resolved_keys))
        atom, candidate, _ = next(item for item in resolved if item[1].key == key)
        specifier = atom.specifier if candidate.apply_specifier else ""
        raw = " | ".join(item.raw for item in atoms)
        return RequirementEdge(parent, key, raw, specifier, constraint_ecosystem="apt")

    # Nothing installed satisfies any alternative. Preserve the first dependency as a
    # missing edge rather than silently deleting evidence from a broken system.
    first = atoms[0]
    target_name = first.name
    if first.qualifier and first.qualifier not in {"any", "native"}:
        target_name = f"{target_name}:{first.qualifier}"
    return RequirementEdge(
        parent,
        PackageKey("apt", target_name),
        " | ".join(atom.raw for atom in atoms),
        first.specifier,
        constraint_ecosystem="apt",
    )


def _load_dpkg_inventory() -> Inventory:
    _require_apt_tools()
    fmt = "${binary:Package}\\t${Version}\\t${db:Status-Abbrev}\\t${Depends}\\t${Pre-Depends}\\t${Provides}\\n"
    proc = _command("dpkg-query", "-W", f"-f={fmt}")
    inventory = Inventory(source="APT/dpkg installed system")
    raw_dependencies: dict[PackageKey, str] = {}

    for line in proc.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 6:
            inventory.diagnostics.append("could not parse one dpkg-query record; analysis may be incomplete")
            continue
        name, version, status, depends, pre_depends, provides = fields
        if not status.startswith("ii"):
            continue
        key = PackageKey("apt", name)
        record = PackageRecord(key=key, version=version or None)
        for provided_name, provided_version in _parse_provides(provides):
            record.provided_names[provided_name] = provided_version
        inventory.add(record)
        raw_dependencies[key] = ", ".join(part for part in (pre_depends, depends) if part)

    native_arch = _native_architecture()
    by_base, providers = _installed_indexes(inventory)
    for key, raw in raw_dependencies.items():
        record = inventory.packages[key]
        for group in _split_dependency_groups(raw):
            edge = _edge_for_group(key, group, inventory, by_base, providers, native_arch)
            if edge is not None:
                record.dependencies.append(edge)

    return inventory


def _resolve_root_name(name: str, inventory: Inventory, native_arch: str) -> PackageKey | None:
    normalized = normalize_apt_name(name)
    exact = PackageKey("apt", normalized)
    if exact in inventory.packages and inventory.packages[exact].installed:
        return exact
    base_matches = [
        key
        for key, record in inventory.packages.items()
        if key.ecosystem == "apt" and record.installed and apt_base_name(key.name) == normalized
    ]
    if len(base_matches) == 1:
        return base_matches[0]
    if native_arch:
        native = PackageKey("apt", f"{normalized}:{native_arch}")
        if native in base_matches:
            return native
    return None


def _load_marks(inventory: Inventory) -> None:
    if shutil.which("apt-mark") is None:
        inventory.diagnostics.append("apt-mark not found; using structural roots instead of manual packages")
        return
    native_arch = _native_architecture()
    manual = _command("apt-mark", "showmanual", check=False)
    if manual.returncode == 0:
        for name in manual.stdout.splitlines():
            key = _resolve_root_name(name, inventory, native_arch)
            if key is not None:
                inventory.declared_roots.add(key)
    else:
        inventory.diagnostics.append("could not read APT manual-package marks; using structural roots")

    held = _command("apt-mark", "showhold", check=False)
    if held.returncode == 0:
        for name in held.stdout.splitlines():
            key = _resolve_root_name(name, inventory, native_arch)
            if key is not None:
                inventory.held_packages.add(key)


def _resolve_change_key(name: str, inventory: Inventory) -> PackageKey | None:
    normalized = normalize_apt_name(name)
    exact = PackageKey("apt", normalized)
    if exact in inventory.packages:
        return exact
    native_arch = _native_architecture()
    return _resolve_root_name(apt_base_name(normalized), inventory, native_arch)


def _check_apt_consistency(inventory: Inventory) -> None:
    if shutil.which("apt-get") is None:
        return
    proc = _command("apt-get", "-s", "-o", "Debug::NoLocking=true", "check", check=False)
    if proc.returncode == 0:
        return
    details = [line.strip() for line in proc.stderr.splitlines() if line.strip()]
    detail = details[-1] if details else f"exit {proc.returncode}"
    inventory.diagnostics.append(f"APT reports a broken dependency state: {detail}")


def _load_upgrade_simulation(inventory: Inventory) -> None:
    if shutil.which("apt-get") is None:
        inventory.diagnostics.append("apt-get not found; upgrade simulation unavailable")
        return
    proc = _command("apt-get", "-s", "-o", "Debug::NoLocking=true", "upgrade", check=False)
    if proc.returncode != 0:
        message = proc.stderr.strip().splitlines()
        detail = message[-1] if message else f"exit {proc.returncode}"
        inventory.diagnostics.append(f"APT upgrade simulation unavailable: {detail}")
        return

    for line in proc.stdout.splitlines():
        install = _INSTALL.match(line)
        if install:
            name = install.group(1)
            old_version = install.group(2)
            candidate = install.group(3)
            key = _resolve_change_key(name, inventory)
            if key is None:
                key = PackageKey("apt", name)
            action = "UPGRADE" if old_version else "INSTALL"
            inventory.planned_changes[key] = PlannedChange(action, candidate, line)
            continue
        remove = _REMOVE.match(line)
        if remove:
            name = remove.group(1)
            key = _resolve_change_key(name, inventory)
            if key is None:
                key = PackageKey("apt", name)
            inventory.planned_changes[key] = PlannedChange("REMOVE", None, line)


def load_apt_inventory() -> Inventory:
    """Inspect the current Debian/Ubuntu APT system without mutating it."""

    inventory = _load_dpkg_inventory()
    _load_marks(inventory)
    _check_apt_consistency(inventory)
    _load_upgrade_simulation(inventory)
    return inventory
