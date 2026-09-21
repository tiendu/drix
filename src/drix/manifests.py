from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml
from packaging.requirements import InvalidRequirement, Requirement

from drix.model import (
    Manifest,
    ManifestRequirement,
    PackageKey,
    default_marker_environment,
    normalize_conda_name,
)

_CONDA_NAME = re.compile(r"^(?:(?:[^:\s]+)::)?([A-Za-z0-9_.-]+)(.*)$")


def _pip_requirement(text: str, *, is_root: bool = True) -> ManifestRequirement | None:
    text = re.sub(r"\s+#.*$", "", text).strip()
    # pip-tools commonly appends one or more hashes to a logical requirement.
    # Hashes verify artifacts; they do not change dependency/version semantics.
    text = re.sub(r"\s+--hash=\S+", "", text).strip()
    if not text or text.startswith("#"):
        return None
    try:
        req = Requirement(text)
    except InvalidRequirement as error:
        raise ValueError(f"unsupported or invalid Python requirement: {text!r}") from error
    if req.marker is not None:
        environment = default_marker_environment()
        environment["extra"] = ""
        try:
            if not req.marker.evaluate(environment):
                return None
        except Exception as error:
            raise ValueError(f"could not evaluate requirement marker: {text!r}") from error
    return ManifestRequirement(
        PackageKey("pypi", req.name),
        text,
        str(req.specifier),
        tuple(sorted(req.extras)),
        is_root,
    )


def _conda_requirement(text: str) -> ManifestRequirement | None:
    text = text.strip()
    if not text or text.startswith("#"):
        return None
    match = _CONDA_NAME.match(text)
    if not match:
        raise ValueError(f"unsupported or invalid Conda requirement: {text!r}")
    name = normalize_conda_name(match.group(1))
    tail = match.group(2).strip()
    return ManifestRequirement(PackageKey("conda", name), text, tail)


def _logical_requirement_lines(path: Path) -> list[str]:
    logical: list[str] = []
    buffer = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if buffer:
            stripped = buffer + stripped
        if stripped.endswith("\\"):
            buffer = stripped[:-1].rstrip() + " "
            continue
        logical.append(stripped)
        buffer = ""
    if buffer:
        logical.append(buffer.rstrip())
    return logical


def _include_target(line: str, short: str, long: str) -> str | None:
    line = re.sub(r"\s+#.*$", "", line).strip()
    if line.startswith(long + "="):
        return line.split("=", 1)[1].strip()
    if line.startswith(long + " "):
        return line.split(maxsplit=1)[1]
    if line.startswith(short) and line != short:
        return line[len(short):].strip()
    return None


def _requirements(
    path: Path,
    seen: set[tuple[Path, bool]] | None = None,
    *,
    is_root: bool = True,
) -> list[ManifestRequirement]:
    seen = seen or set()
    path = path.resolve()
    marker = (path, is_root)
    if marker in seen:
        return []
    seen.add(marker)
    result: list[ManifestRequirement] = []
    for line in _logical_requirement_lines(path):
        if not line or line.startswith("#"):
            continue
        include = _include_target(line, "-r", "--requirement")
        if include is not None:
            result.extend(_requirements(path.parent / include, seen, is_root=is_root))
            continue
        constraint = _include_target(line, "-c", "--constraint")
        if constraint is not None:
            result.extend(_requirements(path.parent / constraint, seen, is_root=False))
            continue
        if line == "-e" or line.startswith(("-e ", "--editable ", "--editable=")):
            raise ValueError(
                f"editable requirement is not supported safely: {line!r}; "
                "use a named requirement or pyproject.toml"
            )
        if line.startswith("-"):
            continue
        req = _pip_requirement(line, is_root=is_root)
        if req:
            result.append(req)
    return result


def _conda_yaml(path: Path) -> list[ManifestRequirement]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise TypeError("environment file must contain a mapping")
    deps = data.get("dependencies", [])
    if not isinstance(deps, list):
        raise TypeError("environment dependencies must be a list")
    result: list[ManifestRequirement] = []
    for item in deps:
        if isinstance(item, str):
            req = _conda_requirement(item)
            if req:
                result.append(req)
        elif isinstance(item, dict):
            if set(item) != {"pip"}:
                raise ValueError(
                    "unsupported environment dependency mapping; only 'pip:' is supported"
                )
            pip_items = item["pip"]
            if not isinstance(pip_items, list):
                raise TypeError("environment pip dependencies must be a list")
            for pip_item in pip_items:
                if not isinstance(pip_item, str):
                    raise TypeError("environment pip dependencies must be strings")
                req = _pip_requirement(pip_item)
                if req:
                    result.append(req)
        else:
            raise TypeError("environment dependency entries must be strings or a pip mapping")
    return result


def _pyproject(path: Path) -> list[ManifestRequirement]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    project = data.get("project", {})
    deps = project.get("dependencies", []) if isinstance(project, dict) else []
    if not isinstance(deps, list):
        raise TypeError("pyproject project.dependencies must be a list")
    result: list[ManifestRequirement] = []
    for item in deps:
        if not isinstance(item, str):
            raise TypeError("pyproject project.dependencies entries must be strings")
        req = _pip_requirement(item)
        if req:
            result.append(req)
    return result


def load_manifest(path: Path) -> Manifest:
    name = path.name.lower()
    try:
        if name == "pyproject.toml":
            requirements = _pyproject(path)
        elif path.suffix.lower() in {".yml", ".yaml"}:
            requirements = _conda_yaml(path)
        elif name.startswith("requirements") and path.suffix.lower() in {".txt", ".in"}:
            requirements = _requirements(path)
        else:
            raise ValueError(
                f"unsupported manifest {path}; expected environment.yml, "
                "requirements.txt/.in, or pyproject.toml"
            )
    except (yaml.YAMLError, tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ValueError(f"could not parse manifest {path}: {error}") from error
    return Manifest(tuple(requirements), str(path))
