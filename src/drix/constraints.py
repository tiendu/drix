from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from importlib import import_module
import re
import shutil
import subprocess

from packaging.specifiers import InvalidSpecifier, Specifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from drix.model import ConstraintContributor
from drix.runtime import external_process_environment


@dataclass(frozen=True)
class ConstraintResult:
    state: str
    violated_by: tuple[ConstraintContributor, ...] = ()
    conflict_by: tuple[ConstraintContributor, ...] = ()


_APT_SPEC = re.compile(r"^\s*(<<|<=|=|>=|>>)\s*(\S(?:.*\S)?)\s*$")


@lru_cache(maxsize=8192)
def _apt_compare(left: str, operator: str, right: str) -> bool | None:
    """Compare Debian versions using dpkg's canonical ordering semantics."""

    if shutil.which("dpkg") is None:
        return None
    op_map = {"<<": "lt", "<=": "le", "=": "eq", ">=": "ge", ">>": "gt"}
    dpkg_op = op_map.get(operator)
    if dpkg_op is None:
        return None
    proc = subprocess.run(
        ["dpkg", "--compare-versions", left, dpkg_op, right],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=external_process_environment(),
    )
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None


def _parse_apt_specifier(specifier: str) -> tuple[str, str] | None:
    if not specifier:
        return None
    match = _APT_SPEC.match(specifier)
    if not match:
        return None
    return match.group(1), match.group(2)


def apt_satisfies(version: str, specifier: str) -> bool | None:
    if not specifier:
        return True
    parsed = _parse_apt_specifier(specifier)
    if parsed is None:
        return None
    operator, required = parsed
    return _apt_compare(version, operator, required)


def _apt_version_cmp(left: str, right: str) -> int | None:
    equal = _apt_compare(left, "=", right)
    if equal is None:
        return None
    if equal:
        return 0
    less = _apt_compare(left, "<<", right)
    if less is None:
        return None
    return -1 if less else 1


def _apt_conflict_provable(specifiers: Iterable[str]) -> bool:
    """Prove contradictions among simple Debian dependency relations.

    Debian relations have one comparator per dependency atom.  We use dpkg itself for
    version ordering, so epochs, tildes, and Debian revisions retain native semantics.
    Unparseable relations are ignored for proof and therefore can only yield UNKNOWN,
    never a false conflict.
    """

    parsed = [item for raw in specifiers if (item := _parse_apt_specifier(raw))]
    if not parsed:
        return False

    exact = [version for op, version in parsed if op == "="]
    if exact:
        for candidate in exact:
            if all(apt_satisfies(candidate, f"{op} {version}") is True for op, version in parsed):
                return False
        return True

    lower: tuple[str, bool] | None = None
    upper: tuple[str, bool] | None = None
    for op, version in parsed:
        if op in {">=", ">>"}:
            inclusive = op == ">="
            if lower is None:
                lower = (version, inclusive)
            else:
                cmp = _apt_version_cmp(version, lower[0])
                if cmp is None:
                    return False
                if cmp > 0 or (cmp == 0 and not inclusive and lower[1]):
                    lower = (version, inclusive)
        elif op in {"<=", "<<"}:
            inclusive = op == "<="
            if upper is None:
                upper = (version, inclusive)
            else:
                cmp = _apt_version_cmp(version, upper[0])
                if cmp is None:
                    return False
                if cmp < 0 or (cmp == 0 and not inclusive and upper[1]):
                    upper = (version, inclusive)

    if lower is None or upper is None:
        return False
    cmp = _apt_version_cmp(lower[0], upper[0])
    if cmp is None:
        return False
    if cmp > 0:
        return True
    return cmp == 0 and (not lower[1] or not upper[1])


def _python_satisfies(version: str, specifier: str) -> bool | None:
    if not specifier:
        return True
    try:
        return Version(version) in SpecifierSet(specifier)
    except (InvalidVersion, InvalidSpecifier):
        return None


def _conda_native_satisfies(version: str, specifier: str) -> bool | None:
    """Use Conda's own MatchSpec semantics when Conda is importable."""

    try:
        version_module = import_module("conda.models.version")
        version_spec = version_module.VersionSpec(specifier)
        return bool(version_spec.match(version))
    except (ImportError, AttributeError):
        return None
    except Exception:  # noqa: BLE001 - optional Conda API may raise package-specific errors.
        # Invalid/unsupported MatchSpec must not be turned into a false OK.
        return None


def _conda_fallback_satisfies(version: str, specifier: str) -> bool | None:
    """Conservative subset of Conda version matching.

    Only comparator/prefix expressions with no build-string syntax are handled. Any
    syntax outside that subset is UNKNOWN rather than being interpreted as PEP 440.
    """

    if not specifier:
        return True
    if any(char.isspace() for char in specifier) or "|" in specifier:
        return None
    try:
        parsed = Version(version)
    except InvalidVersion:
        # Prefix equality still has useful exact string semantics for versions that are
        # legal in Conda but not PEP 440. Ordered comparison does not.
        parsed = None

    for token in (part.strip() for part in specifier.split(",")):
        if not token:
            continue
        op = ""
        rhs = token
        for candidate in (">=", "<=", "!=", "==", ">", "<", "="):
            if token.startswith(candidate):
                op, rhs = candidate, token[len(candidate):].strip()
                break
        if not rhs:
            return None
        # A second '=' is Conda build-string syntax (e.g. =3.12=cpython_0).
        # Version-only fallback matching cannot evaluate that safely.
        if "=" in rhs:
            return None

        if op in {"", "="}:
            prefix = rhs.rstrip("*").rstrip(".")
            if not (version == prefix or version.startswith(prefix + ".")):
                return False
            continue

        if rhs.endswith("*"):
            if op not in {"==", "!="}:
                return None
            prefix = rhs[:-1].rstrip(".")
            equal = version == prefix or version.startswith(prefix + ".")
            if op == "==" and not equal:
                return False
            if op == "!=" and equal:
                return False
            continue

        if parsed is None:
            return None
        try:
            other = Version(rhs)
        except InvalidVersion:
            return None
        if op == ">=" and not parsed >= other:
            return False
        if op == "<=" and not parsed <= other:
            return False
        if op == ">" and not parsed > other:
            return False
        if op == "<" and not parsed < other:
            return False
        if op == "==" and parsed != other:
            return False
        if op == "!=" and parsed == other:
            return False
    return True


def _conda_satisfies(version: str, specifier: str) -> bool | None:
    if not specifier:
        return True
    native = _conda_native_satisfies(version, specifier)
    if native is not None:
        return native
    return _conda_fallback_satisfies(version, specifier)


def _satisfies(ecosystem: str, version: str, specifier: str) -> bool | None:
    if ecosystem == "pypi":
        return _python_satisfies(version, specifier)
    if ecosystem == "conda":
        return _conda_satisfies(version, specifier)
    if ecosystem == "apt":
        return apt_satisfies(version, specifier)
    return None


def _prefix_interval(raw: str) -> tuple[Version, Version] | None:
    prefix = raw.removesuffix(".*")
    pieces = prefix.split(".")
    if not pieces or not all(piece.isdigit() for piece in pieces):
        return None
    nums = [int(piece) for piece in pieces]
    low = Version(".".join(map(str, nums)))
    high_parts = nums[:]
    high_parts[-1] += 1
    high = Version(".".join(map(str, high_parts)))
    return low, high


def _python_conflict_provable(specifiers: Iterable[str]) -> bool:
    """Prove common PEP 440 contradictions without consulting a package index.

    Unsupported/invalid constraints are ignored *for proof purposes*: if the remaining
    known subset is already contradictory, the complete set is necessarily
    contradictory too. When the known subset is satisfiable we stay conservative.
    """

    sets: list[SpecifierSet] = []
    specs: list[Specifier] = []
    for raw in specifiers:
        if not raw:
            continue
        try:
            specifier_set = SpecifierSet(raw)
        except InvalidSpecifier:
            continue
        sets.append(specifier_set)
        specs.extend(list(specifier_set))

    if not sets:
        return False

    exact: list[Version] = []
    for spec in specs:
        if spec.operator == "==" and not spec.version.endswith(".*"):
            try:
                exact.append(Version(spec.version))
            except InvalidVersion:
                pass
    if exact:
        return not any(all(candidate in specifier_set for specifier_set in sets) for candidate in exact)

    lower: tuple[Version, bool] | None = None
    upper: tuple[Version, bool] | None = None
    excluded_prefixes: list[tuple[Version, Version]] = []

    def set_lower(version: Version, inclusive: bool) -> None:
        nonlocal lower
        if lower is None or version > lower[0] or (
            version == lower[0] and not inclusive and lower[1]
        ):
            lower = (version, inclusive)

    def set_upper(version: Version, inclusive: bool) -> None:
        nonlocal upper
        if upper is None or version < upper[0] or (
            version == upper[0] and not inclusive and upper[1]
        ):
            upper = (version, inclusive)

    for spec in specs:
        op, raw = spec.operator, spec.version
        if op in {">", ">=", "<", "<="}:
            try:
                version = Version(raw)
            except InvalidVersion:
                continue
            if op == ">":
                set_lower(version, False)
            elif op == ">=":
                set_lower(version, True)
            elif op == "<":
                set_upper(version, False)
            else:
                set_upper(version, True)
        elif op == "==" and raw.endswith(".*"):
            interval = _prefix_interval(raw)
            if interval:
                set_lower(interval[0], True)
                set_upper(interval[1], False)
        elif op == "!=" and raw.endswith(".*"):
            interval = _prefix_interval(raw)
            if interval:
                excluded_prefixes.append(interval)
        elif op == "~=":
            try:
                version = Version(raw)
            except InvalidVersion:
                continue
            set_lower(version, True)
            release = list(version.release)
            if len(release) <= 1:
                continue
            if len(release) == 2:
                high = Version(f"{release[0] + 1}.0")
            else:
                high_parts = release[:-1]
                high_parts[-1] += 1
                high = Version(".".join(map(str, high_parts)))
            set_upper(high, False)

    if lower and upper:
        if lower[0] > upper[0]:
            return True
        if lower[0] == upper[0] and (not lower[1] or not upper[1]):
            return True
        if lower[0] == upper[0] and lower[1] and upper[1]:
            candidate = lower[0]
            return not all(candidate in specifier_set for specifier_set in sets)

        # Prefix exclusions can erase a whole otherwise-valid interval. Merge them and
        # see whether they cover [lower, upper] completely. This proves cases such as
        # >=1,<3 together with !=1.* and !=2.* without inventing package-index data.
        if excluded_prefixes:
            merged: list[list[Version]] = []
            for start, end in sorted(excluded_prefixes):
                if not merged or start > merged[-1][1]:
                    merged.append([start, end])
                elif end > merged[-1][1]:
                    merged[-1][1] = end
            for start, end in merged:
                lower_covered = lower[0] >= start
                upper_covered = upper[0] < end or (upper[0] == end and not upper[1])
                if lower_covered and upper_covered:
                    return True
    return False


def _conda_to_pep440_subset(specifier: str) -> str | None:
    if not specifier:
        return ""
    if any(char.isspace() for char in specifier) or "|" in specifier:
        return None
    tokens: list[str] = []
    for token in specifier.split(","):
        token = token.strip()
        if not token:
            continue
        if token.startswith("=") and not token.startswith("=="):
            value = token[1:]
            if not value or "=" in value:
                return None
            tokens.append(f"=={value}.*" if "*" not in value else f"=={value}")
        elif token[0].isdigit():
            tokens.append(f"=={token}.*")
        elif token.startswith((">=", "<=", "!=", "==", ">", "<")):
            tokens.append(token)
        else:
            return None
    converted = ",".join(tokens)
    try:
        SpecifierSet(converted)
    except InvalidSpecifier:
        return None
    return converted


def _conda_conflict_provable(specifiers: Iterable[str]) -> bool:
    converted: list[str] = []
    for raw in specifiers:
        if not raw:
            continue
        pep = _conda_to_pep440_subset(raw)
        if pep is None:
            continue
        converted.append(pep)
    return _python_conflict_provable(converted)


def _conflict_provable(ecosystem: str, specifiers: Iterable[str]) -> bool:
    if ecosystem == "pypi":
        return _python_conflict_provable(specifiers)
    if ecosystem == "conda":
        return _conda_conflict_provable(specifiers)
    if ecosystem == "apt":
        return _apt_conflict_provable(specifiers)
    return False


def _contributors_conflict_provable(
    fallback_ecosystem: str,
    contributors: Iterable[ConstraintContributor],
) -> bool:
    contributors = tuple(contributors)
    if not contributors:
        return False

    ecosystems = {item.ecosystem or fallback_ecosystem for item in contributors}
    if len(ecosystems) == 1:
        ecosystem = next(iter(ecosystems))
        return _conflict_provable(ecosystem, (item.specifier for item in contributors))

    # Mixed pip/Conda constraints can still be compared safely when their known
    # version-only subset maps to PEP 440. Unsupported Conda syntax is ignored only
    # for proof: a contradiction in the convertible subset remains a contradiction.
    converted: list[str] = []
    for item in contributors:
        ecosystem = item.ecosystem or fallback_ecosystem
        if ecosystem == "pypi":
            try:
                SpecifierSet(item.specifier)
            except InvalidSpecifier:
                continue
            converted.append(item.specifier)
        elif ecosystem == "conda":
            pep = _conda_to_pep440_subset(item.specifier)
            if pep is not None:
                converted.append(pep)
    return _python_conflict_provable(converted)


def _minimal_conflict_witness(
    fallback_ecosystem: str,
    contributors: tuple[ConstraintContributor, ...],
) -> tuple[ConstraintContributor, ...]:
    """Return a 1-minimal set of contributors that still proves the conflict.

    This avoids combinatorial subset search while ensuring every contributor in the
    returned witness is necessary for the contradiction under drix's conservative
    prover.
    """

    witness = [item for item in contributors if item.specifier]
    if not _contributors_conflict_provable(fallback_ecosystem, witness):
        return ()

    index = 0
    while index < len(witness):
        candidate = witness[:index] + witness[index + 1 :]
        if candidate and _contributors_conflict_provable(fallback_ecosystem, candidate):
            witness = candidate
            continue
        index += 1
    return tuple(witness)


def analyze_constraints(
    ecosystem: str,
    installed_version: str | None,
    contributors: tuple[ConstraintContributor, ...],
    *,
    installed: bool = True,
) -> ConstraintResult:
    if not installed:
        return ConstraintResult("MISSING")

    constrained = tuple(contributor for contributor in contributors if contributor.specifier)
    if not constrained:
        return ConstraintResult("OK" if installed_version is not None else "UNKNOWN")

    violated: list[ConstraintContributor] = []
    unknown = installed_version is None
    if installed_version is not None:
        for contributor in constrained:
            semantics = contributor.ecosystem or ecosystem
            result = _satisfies(semantics, installed_version, contributor.specifier)
            if result is False:
                violated.append(contributor)
            elif result is None:
                unknown = True

    conflict = _contributors_conflict_provable(ecosystem, constrained)
    if conflict:
        return ConstraintResult(
            "CONFLICT",
            tuple(violated),
            _minimal_conflict_witness(ecosystem, constrained),
        )
    if violated:
        return ConstraintResult("INVALID", tuple(violated))
    if unknown:
        return ConstraintResult("UNKNOWN")
    return ConstraintResult("OK")
