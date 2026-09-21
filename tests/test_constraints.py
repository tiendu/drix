from drix.constraints import analyze_constraints
from drix.model import ConstraintContributor, PackageKey


def c(parent: str, spec: str) -> ConstraintContributor:
    return ConstraintContributor(PackageKey("pypi", parent), f"target{spec}", spec)


def test_current_version_ok_proves_nonempty_intersection() -> None:
    result = analyze_constraints("pypi", "1.26.4", (c("a", ">=1.24,<2"), c("b", ">=1.26"), c("c", "<1.27")))
    assert result.state == "OK"


def test_conflicting_exact_and_bound_constraints() -> None:
    result = analyze_constraints("pypi", "1.25", (c("a", "<1.26"), c("b", ">=2")))
    assert result.state == "CONFLICT"


def test_invalid_is_not_mislabeled_conflict() -> None:
    result = analyze_constraints("pypi", "2.0", (c("a", ">=1.24,<2"), c("b", ">=1.26")))
    assert result.state == "INVALID"


def test_conflict_reports_minimal_witness() -> None:
    result = analyze_constraints(
        "pypi",
        "1.5",
        (c("a", ">=1"), c("b", "<2"), c("c", ">=2"), c("d", "!=1.5")),
    )
    assert result.state == "CONFLICT"
    assert len(result.conflict_by) == 2
    assert {item.package.name for item in result.conflict_by} == {"b", "c"}


def test_missing_is_not_reported_ok() -> None:
    result = analyze_constraints("pypi", None, (c("a", ">=1"),), installed=False)
    assert result.state == "MISSING"


def test_unknown_version_with_constraint_is_unknown() -> None:
    result = analyze_constraints("pypi", None, (c("a", ">=1"),))
    assert result.state == "UNKNOWN"
