from pathlib import Path

from drix.manifests import load_manifest
from drix.model import PackageKey


def test_conda_yaml_roots_and_constraints(tmp_path: Path) -> None:
    path = tmp_path / "environment.yml"
    path.write_text(
        "dependencies:\n  - python=3.12\n  - samtools\n  - pip:\n      - requests>=2\n",
        encoding="utf-8",
    )
    manifest = load_manifest(path)
    assert PackageKey("conda", "python") in manifest.roots
    assert PackageKey("conda", "samtools") in manifest.roots
    assert PackageKey("pypi", "requests") in manifest.roots
    python_req = next(r for r in manifest.requirements if r.package == PackageKey("conda", "python"))
    requests_req = next(r for r in manifest.requirements if r.package == PackageKey("pypi", "requests"))
    assert python_req.specifier == "=3.12"
    assert requests_req.specifier == ">=2"
