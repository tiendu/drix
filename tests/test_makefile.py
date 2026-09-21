from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _copy_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    shutil.copytree(
        ROOT,
        repo,
        ignore=shutil.ignore_patterns(
            ".build",
            ".git",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            "__pycache__",
            "build",
            "dist",
        ),
    )
    return repo


def _fake_up_to_date_binary(root: Path) -> Path:
    binary = root / "dist" / "drix"
    binary.parent.mkdir(exist_ok=True)
    binary.write_text("#!/bin/sh\necho drix-test\n", encoding="utf-8")
    binary.chmod(0o755)

    inputs = [root / "pyproject.toml"]
    inputs.extend((root / "src").rglob("*.py"))
    inputs.extend((root / "scripts").rglob("*.py"))
    newest = max(path.stat().st_mtime for path in inputs)
    os.utime(binary, (newest + 10, newest + 10))
    return binary


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
def test_make_install_and_uninstall_respect_prefix(tmp_path: Path) -> None:
    repo = _copy_repo(tmp_path)
    binary = _fake_up_to_date_binary(repo)
    prefix = tmp_path / "prefix"

    subprocess.run(
        ["make", "install", f"PREFIX={prefix}"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    installed = prefix / "bin" / "drix"
    assert installed.read_bytes() == binary.read_bytes()
    assert installed.stat().st_mode & stat.S_IXUSR

    subprocess.run(
        ["make", "uninstall", f"PREFIX={prefix}"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert not installed.exists()


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
def test_make_install_respects_destdir(tmp_path: Path) -> None:
    repo = _copy_repo(tmp_path)
    binary = _fake_up_to_date_binary(repo)
    stage = tmp_path / "stage"

    subprocess.run(
        ["make", "install", "PREFIX=/usr/local", f"DESTDIR={stage}"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    installed = stage / "usr" / "local" / "bin" / "drix"
    assert installed.read_bytes() == binary.read_bytes()


def _write_fake_bootstrap_python(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "python-calls.log"
    bootstrap = tmp_path / "bootstrap-python"
    child_source = f'''#!{shutil.which("python3") or "/usr/bin/python3"}
from pathlib import Path
import sys

log = Path({str(log)!r})
with log.open("a", encoding="utf-8") as handle:
    handle.write("child " + repr(sys.argv[1:]) + "\\n")

args = sys.argv[1:]
if args[:3] == ["-m", "pip", "--version"]:
    print("pip fake")
elif args[:3] == ["-m", "pip", "install"]:
    pass
elif args[:2] in (["-m", "pytest"], ["-m", "ruff"], ["-m", "mypy"], ["-m", "compileall"]):
    pass
elif args == ["scripts/build_binary.py"]:
    binary = Path.cwd() / "dist" / "drix"
    binary.parent.mkdir(exist_ok=True)
    binary.write_text("#!/bin/sh\\necho drix-fake\\n", encoding="utf-8")
    binary.chmod(0o755)
else:
    raise SystemExit("unexpected child python invocation: " + repr(args))
'''
    bootstrap.write_text(
        f'''#!{shutil.which("python3") or "/usr/bin/python3"}
from pathlib import Path
import sys

log = Path({str(log)!r})
with log.open("a", encoding="utf-8") as handle:
    handle.write("bootstrap " + repr(sys.argv[1:]) + "\\n")

args = sys.argv[1:]
if len(args) == 3 and args[:2] == ["-m", "venv"]:
    env = Path(args[2])
    child = env / "bin" / "python"
    child.parent.mkdir(parents=True, exist_ok=True)
    child.write_text({child_source!r}, encoding="utf-8")
    child.chmod(0o755)
else:
    raise SystemExit("unexpected bootstrap python invocation: " + repr(args))
''',
        encoding="utf-8",
    )
    bootstrap.chmod(0o755)
    return bootstrap, log


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
def test_make_check_bootstraps_dev_tools_without_host_pytest(tmp_path: Path) -> None:
    repo = _copy_repo(tmp_path)
    bootstrap, log = _write_fake_bootstrap_python(tmp_path)
    build_env = tmp_path / "check-env"

    subprocess.run(
        ["make", "check", f"PYTHON={bootstrap}", f"BUILD_ENV={build_env}"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )

    calls = log.read_text(encoding="utf-8")
    assert "bootstrap ['-m', 'venv'" in calls
    assert "child ['-m', 'pip', 'install', '-e', '.[dev]']" in calls
    assert "child ['-m', 'pytest', '-q']" in calls
    assert "child ['-m', 'ruff', 'check', '.']" in calls
    assert "child ['-m', 'mypy', 'src']" in calls
    assert "child ['-m', 'compileall', '-q', 'src']" in calls


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
def test_make_install_bootstraps_pyinstaller_without_host_dependency(tmp_path: Path) -> None:
    repo = _copy_repo(tmp_path)
    bootstrap, log = _write_fake_bootstrap_python(tmp_path)
    build_env = tmp_path / "binary-env"
    prefix = tmp_path / "prefix"

    subprocess.run(
        [
            "make",
            "install",
            f"PYTHON={bootstrap}",
            f"BUILD_ENV={build_env}",
            f"PREFIX={prefix}",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )

    installed = prefix / "bin" / "drix"
    assert installed.exists()
    assert installed.stat().st_mode & stat.S_IXUSR
    calls = log.read_text(encoding="utf-8")
    assert "child ['-m', 'pip', 'install', '-e', '.[binary]']" in calls
    assert "child ['scripts/build_binary.py']" in calls
