from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from drix import __version__
from drix.analysis import analyze
from drix.apt import load_apt_inventory
from drix.inventory import load_inventory
from drix.manifests import load_manifest
from drix.model import name_matches
from drix.render import render_json, render_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="drix",
        description="Show which installed packages are risky to change, and expose version conflicts.",
        epilog=(
            "examples: drix | drix numpy | drix environment.yml [package] | "
            "drix apt [package]"
        ),
    )
    parser.add_argument(
        "target",
        nargs="?",
        help=(
            "manifest/environment prefix, package name, or the reserved source 'apt' "
            "for the host Debian/Ubuntu package system"
        ),
    )
    parser.add_argument("package", nargs="?", help="show one package only")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--version", action="version", version=f"drix {__version__}")
    return parser


def _looks_like_path(text: str) -> bool:
    """Return True when *text* clearly expresses a path/manifest, not a package name."""

    if "/" in text or "\\" in text or text.startswith((".", "~")):
        return True
    candidate = Path(text)
    name = candidate.name.lower()
    if name == "pyproject.toml":
        return True
    if candidate.suffix.lower() in {".yml", ".yaml"}:
        return True
    return name.startswith("requirements") and candidate.suffix.lower() in {".txt", ".in"}


def _interpret(
    args: argparse.Namespace,
) -> tuple[str, Path | None, Path | None, str | None]:
    prefix: Path | None = None
    manifest_path: Path | None = None
    focus: str | None = args.package
    mode = "environment"

    if args.target == "apt":
        return "apt", None, None, focus

    if args.target:
        candidate = Path(args.target).expanduser()
        if candidate.exists():
            if candidate.is_dir():
                prefix = candidate
            else:
                manifest_path = candidate
        elif focus is None and not _looks_like_path(args.target):
            focus = args.target
        else:
            raise ValueError(f"target does not exist: {args.target}")
    return mode, prefix, manifest_path, focus


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        mode, prefix, manifest_path, focus = _interpret(args)
        manifest = load_manifest(manifest_path) if manifest_path else None
        inventory = load_apt_inventory() if mode == "apt" else load_inventory(prefix)
        results, roots, missing_roots = analyze(inventory, manifest, focus=focus)
        if args.json:
            print(render_json(inventory, results, roots, missing_roots, focus))
        else:
            print(render_text(inventory, results, roots, missing_roots, focus))
        if focus and not any(name_matches(row.package, focus) for row in results):
            return 1
        return 0
    except (OSError, subprocess.SubprocessError, TypeError, ValueError) as error:
        print(f"drix: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
