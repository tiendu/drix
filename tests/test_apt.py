from __future__ import annotations

import shutil
import subprocess

import pytest

import drix.apt as apt_module
from drix.analysis import analyze
from drix.constraints import analyze_constraints
from drix.model import ConstraintContributor, PackageKey, name_matches


def _completed(args: tuple[str, ...], stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, returncode, stdout, "")


def test_apt_loader_uses_manual_roots_holds_and_upgrade_simulation(monkeypatch) -> None:
    dpkg_output = "\n".join(
        [
            "app\t1.0\tii \tlibfoo (>= 2), virtual-x | fallback\t\t",
            "libfoo:amd64\t2.1\tii \t\t\t",
            "provider:amd64\t1.0\tii \t\t\tvirtual-x",
            "fallback\t1.0\tii \t\t\t",
        ]
    ) + "\n"

    def fake_command(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        del check
        if args[:2] == ("dpkg-query", "-W"):
            return _completed(args, dpkg_output)
        if args == ("dpkg", "--print-architecture"):
            return _completed(args, "amd64\n")
        if args == ("apt-mark", "showmanual"):
            return _completed(args, "app\n")
        if args == ("apt-mark", "showhold"):
            return _completed(args, "libfoo\n")
        if args[0] == "apt-get":
            return _completed(args, "Inst libfoo:amd64 [2.1] (2.2 Debian:stable [amd64])\n")
        raise AssertionError(args)

    monkeypatch.setattr(apt_module, "_command", fake_command)
    monkeypatch.setattr(apt_module.shutil, "which", lambda name: f"/usr/bin/{name}")

    inventory = apt_module.load_apt_inventory()
    app = PackageKey("apt", "app")
    libfoo = PackageKey("apt", "libfoo:amd64")
    provider = PackageKey("apt", "provider:amd64")
    fallback = PackageKey("apt", "fallback")

    assert inventory.declared_roots == {app}
    assert inventory.held_packages == {libfoo}
    assert inventory.planned_changes[libfoo].action == "UPGRADE"
    assert inventory.planned_changes[libfoo].candidate_version == "2.2"

    deps = inventory.packages[app].dependencies
    assert any(edge.target == libfoo and edge.specifier == ">= 2" for edge in deps)
    # Both alternatives are installed, so neither is a hard blast edge.
    assert not any(edge.target in {provider, fallback} for edge in deps)

    results, roots, missing = analyze(inventory, focus="libfoo")
    assert roots == {app}
    assert not missing
    row = next(item for item in results if item.package == libfoo)
    assert row.affected_roots == (app,)
    assert row.held is True
    assert row.planned_action == "UPGRADE"
    assert row.candidate_version == "2.2"


def test_apt_virtual_provider_is_used_when_it_is_the_only_installed_choice(monkeypatch) -> None:
    dpkg_output = "\n".join(
        [
            "app\t1.0\tii \tvirtual-x | fallback\t\t",
            "provider:amd64\t1.0\tii \t\t\tvirtual-x",
        ]
    ) + "\n"

    def fake_command(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        del check
        if args[:2] == ("dpkg-query", "-W"):
            return _completed(args, dpkg_output)
        if args == ("dpkg", "--print-architecture"):
            return _completed(args, "amd64\n")
        if args == ("apt-mark", "showmanual"):
            return _completed(args, "app\n")
        if args == ("apt-mark", "showhold"):
            return _completed(args)
        if args[0] == "apt-get":
            return _completed(args)
        raise AssertionError(args)

    monkeypatch.setattr(apt_module, "_command", fake_command)
    monkeypatch.setattr(apt_module.shutil, "which", lambda name: f"/usr/bin/{name}")

    inventory = apt_module.load_apt_inventory()
    app = PackageKey("apt", "app")
    provider = PackageKey("apt", "provider:amd64")
    assert inventory.packages[app].dependencies[0].target == provider


def test_apt_missing_alternative_is_preserved_as_missing_edge(monkeypatch) -> None:
    dpkg_output = "app\t1.0\tii \tmissing-a | missing-b\t\t\n"

    def fake_command(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        del check
        if args[:2] == ("dpkg-query", "-W"):
            return _completed(args, dpkg_output)
        if args == ("dpkg", "--print-architecture"):
            return _completed(args, "amd64\n")
        if args == ("apt-mark", "showmanual"):
            return _completed(args, "app\n")
        if args == ("apt-mark", "showhold") or args[0] == "apt-get":
            return _completed(args)
        raise AssertionError(args)

    monkeypatch.setattr(apt_module, "_command", fake_command)
    monkeypatch.setattr(apt_module.shutil, "which", lambda name: f"/usr/bin/{name}")

    inventory = apt_module.load_apt_inventory()
    results, _, _ = analyze(inventory)
    missing = next(row for row in results if row.package == PackageKey("apt", "missing-a"))
    assert missing.version_state == "MISSING"
    assert missing.transitive_count == 1


@pytest.mark.skipif(shutil.which("dpkg") is None, reason="dpkg is required for Debian version semantics")
def test_apt_conflict_uses_dpkg_version_ordering() -> None:
    target = PackageKey("apt", "libfoo")
    contributors = (
        ConstraintContributor(PackageKey("apt", "a"), "libfoo (>= 2)", ">= 2", "apt"),
        ConstraintContributor(PackageKey("apt", "b"), "libfoo (<< 2)", "<< 2", "apt"),
    )
    result = analyze_constraints("apt", "1:2.0~rc1-1", contributors)
    assert result.state == "CONFLICT"
    assert len(result.conflict_by) == 2
    assert target.name == "libfoo"


def test_apt_name_match_ignores_arch_when_query_is_unqualified() -> None:
    key = PackageKey("apt", "libssl3t64:amd64")
    assert name_matches(key, "libssl3t64")
    assert name_matches(key, "libssl3t64:amd64")
    assert not name_matches(key, "libssl3t64:arm64")


def test_versioned_virtual_dependency_rejects_unversioned_provider(monkeypatch) -> None:
    dpkg_output = "\n".join(
        [
            "app\t1.0\tii \tvirtual-x (>= 2)\t\t",
            "provider:amd64\t9.0\tii \t\t\tvirtual-x",
        ]
    ) + "\n"

    def fake_command(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        del check
        if args[:2] == ("dpkg-query", "-W"):
            return _completed(args, dpkg_output)
        if args == ("dpkg", "--print-architecture"):
            return _completed(args, "amd64\n")
        if args == ("apt-mark", "showmanual"):
            return _completed(args, "app\n")
        if args == ("apt-mark", "showhold") or args[0] == "apt-get":
            return _completed(args)
        raise AssertionError(args)

    monkeypatch.setattr(apt_module, "_command", fake_command)
    monkeypatch.setattr(apt_module.shutil, "which", lambda name: f"/usr/bin/{name}")

    inventory = apt_module.load_apt_inventory()
    edge = inventory.packages[PackageKey("apt", "app")].dependencies[0]
    assert edge.target == PackageKey("apt", "virtual-x")


def test_multiarch_any_keeps_redundant_alternatives_from_becoming_hard_edge(monkeypatch) -> None:
    dpkg_output = "\n".join(
        [
            "app\t1.0\tii \tlibfoo:any\t\t",
            "libfoo:amd64\t1.0\tii \t\t\t",
            "libfoo:arm64\t1.0\tii \t\t\t",
        ]
    ) + "\n"

    def fake_command(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        del check
        if args[:2] == ("dpkg-query", "-W"):
            return _completed(args, dpkg_output)
        if args == ("dpkg", "--print-architecture"):
            return _completed(args, "amd64\n")
        if args == ("apt-mark", "showmanual"):
            return _completed(args, "app\n")
        if args == ("apt-mark", "showhold") or args[0] == "apt-get":
            return _completed(args)
        raise AssertionError(args)

    monkeypatch.setattr(apt_module, "_command", fake_command)
    monkeypatch.setattr(apt_module.shutil, "which", lambda name: f"/usr/bin/{name}")

    inventory = apt_module.load_apt_inventory()
    assert inventory.packages[PackageKey("apt", "app")].dependencies == []


def test_apt_check_failure_is_reported_without_aborting(monkeypatch) -> None:
    dpkg_output = "app\t1.0\tii \t\t\t\n"

    def fake_command(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        del check
        if args[:2] == ("dpkg-query", "-W"):
            return _completed(args, dpkg_output)
        if args == ("dpkg", "--print-architecture"):
            return _completed(args, "amd64\n")
        if args == ("apt-mark", "showmanual"):
            return _completed(args, "app\n")
        if args == ("apt-mark", "showhold"):
            return _completed(args)
        if args[-1] == "check":
            return subprocess.CompletedProcess(args, 100, "", "E: Unmet dependencies\n")
        if args[-1] == "upgrade":
            return _completed(args)
        raise AssertionError(args)

    monkeypatch.setattr(apt_module, "_command", fake_command)
    monkeypatch.setattr(apt_module.shutil, "which", lambda name: f"/usr/bin/{name}")

    inventory = apt_module.load_apt_inventory()
    assert any("APT reports a broken dependency state" in item for item in inventory.diagnostics)


def test_apt_subprocess_uses_sanitized_external_environment(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr(apt_module, "external_process_environment", lambda: {"SAFE": "1"})
    monkeypatch.setattr(apt_module.subprocess, "run", fake_run)

    apt_module._command("dpkg", "--print-architecture")
    assert captured["env"] == {"SAFE": "1"}
