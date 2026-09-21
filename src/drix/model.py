from __future__ import annotations

from dataclasses import dataclass, field

from packaging.markers import default_environment
from packaging.utils import canonicalize_name


def default_marker_environment() -> dict[str, str]:
    """Return packaging marker values as an ordinary mutable string mapping."""

    return {key: str(value) for key, value in default_environment().items()}


def normalize_name(name: str) -> str:
    """Return the canonical PyPI/distribution spelling used by drix."""

    return canonicalize_name(name.strip())


def normalize_conda_name(name: str) -> str:
    """Normalize Conda identity without folding valid separator characters."""

    return name.strip().lower()


def normalize_apt_name(name: str) -> str:
    """Normalize Debian/APT package identity without changing architecture qualifiers."""

    return name.strip().lower()


def apt_base_name(name: str) -> str:
    """Return an APT binary package name without a ``:architecture`` qualifier."""

    return normalize_apt_name(name).split(":", 1)[0]


def name_matches(key: PackageKey, query: str) -> bool:
    if key.ecosystem == "conda":
        return key.name == normalize_conda_name(query)
    if key.ecosystem == "apt":
        normalized = normalize_apt_name(query)
        return key.name == normalized or (
            ":" not in normalized and apt_base_name(key.name) == normalized
        )
    return key.name == normalize_name(query)


@dataclass(frozen=True, order=True)
class PackageKey:
    ecosystem: str
    name: str

    def __post_init__(self) -> None:
        ecosystem = self.ecosystem.strip().lower()
        object.__setattr__(self, "ecosystem", ecosystem)
        if ecosystem == "conda":
            normalizer = normalize_conda_name
        elif ecosystem == "apt":
            normalizer = normalize_apt_name
        else:
            normalizer = normalize_name
        object.__setattr__(self, "name", normalizer(self.name))

    def label(self) -> str:
        return self.name


@dataclass(frozen=True)
class RequirementEdge:
    parent: PackageKey
    target: PackageKey
    raw: str
    specifier: str = ""
    marker: str = ""
    extras: tuple[str, ...] = ()
    constraint_ecosystem: str = ""

    def __post_init__(self) -> None:
        if not self.constraint_ecosystem:
            object.__setattr__(self, "constraint_ecosystem", self.target.ecosystem)
        else:
            object.__setattr__(self, "constraint_ecosystem", self.constraint_ecosystem.lower())


@dataclass
class PackageRecord:
    key: PackageKey
    version: str | None = None
    dependencies: list[RequirementEdge] = field(default_factory=list)
    installed: bool = True
    python_names: set[str] = field(default_factory=set)
    metadata_diagnostics: list[str] = field(default_factory=list)
    provided_names: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PlannedChange:
    action: str
    candidate_version: str | None = None
    raw: str = ""


@dataclass
class Inventory:
    packages: dict[PackageKey, PackageRecord] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)
    source: str = "current environment"
    marker_environment: dict[str, str] = field(default_factory=dict)
    declared_roots: set[PackageKey] = field(default_factory=set)
    held_packages: set[PackageKey] = field(default_factory=set)
    planned_changes: dict[PackageKey, PlannedChange] = field(default_factory=dict)

    def add(self, record: PackageRecord) -> None:
        existing = self.packages.get(record.key)
        if existing is None:
            self.packages[record.key] = record
            return

        if not existing.installed and record.installed:
            self.packages[record.key] = record
            return
        if existing.installed and not record.installed:
            return

        if existing.installed and record.installed:
            merged = list(dict.fromkeys([*existing.dependencies, *record.dependencies]))
            existing.dependencies = merged
            existing.python_names.update(record.python_names)
            existing.provided_names.update(record.provided_names)
            if existing.version != record.version:
                versions = sorted({str(existing.version), str(record.version)})
                existing.version = None
                message = (
                    f"multiple installed versions for {record.key.ecosystem}:{record.key.name}: "
                    + ", ".join(versions)
                )
                if message not in self.diagnostics:
                    self.diagnostics.append(message)
            return

        self.packages[record.key] = record


@dataclass(frozen=True)
class ManifestRequirement:
    package: PackageKey
    raw: str
    specifier: str = ""
    extras: tuple[str, ...] = ()
    is_root: bool = True


@dataclass(frozen=True)
class Manifest:
    requirements: tuple[ManifestRequirement, ...]
    source: str

    @property
    def roots(self) -> frozenset[PackageKey]:
        return frozenset(req.package for req in self.requirements if req.is_root)


@dataclass(frozen=True)
class ConstraintContributor:
    package: PackageKey
    raw: str
    specifier: str
    ecosystem: str = ""


@dataclass(frozen=True)
class PackageRisk:
    package: PackageKey
    version: str | None
    installed: bool
    affected_roots: tuple[PackageKey, ...]
    transitive_dependents: tuple[PackageKey, ...]
    transitive_count: int
    direct_dependents: tuple[PackageKey, ...]
    root_paths: tuple[tuple[PackageKey, ...], ...]
    version_state: str
    contributors: tuple[ConstraintContributor, ...]
    violated_by: tuple[ConstraintContributor, ...] = ()
    conflict_by: tuple[ConstraintContributor, ...] = ()
    planned_action: str | None = None
    candidate_version: str | None = None
    held: bool = False

    @property
    def rank(self) -> tuple[int, int, int]:
        return (
            len(self.affected_roots),
            self.transitive_count,
            len(self.direct_dependents),
        )
