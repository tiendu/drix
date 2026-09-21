from drix.main import build_parser


def test_cli_has_no_subcommands() -> None:
    parser = build_parser()
    args = parser.parse_args(["requirements.txt", "numpy", "--json"])
    assert args.target == "requirements.txt"
    assert args.package == "numpy"
    assert args.json is True


def test_missing_manifest_like_target_is_an_error(capsys) -> None:
    from drix.main import main

    code = main(["missing-environment.yml"])

    captured = capsys.readouterr()
    assert code == 2
    assert "target does not exist: missing-environment.yml" in captured.err
    assert "Traceback" not in captured.err


def test_missing_explicit_path_is_an_error(capsys, tmp_path) -> None:
    from drix.main import main

    missing = tmp_path / "no-such-env"
    code = main([str(missing)])

    captured = capsys.readouterr()
    assert code == 2
    assert f"target does not exist: {missing}" in captured.err


def test_malformed_yaml_is_clean_cli_error(capsys, tmp_path) -> None:
    from drix.main import main

    manifest = tmp_path / "environment.yml"
    manifest.write_text("dependencies:\n  - numpy\n    broken: [\n", encoding="utf-8")

    code = main([str(manifest)])

    captured = capsys.readouterr()
    assert code == 2
    assert "drix:" in captured.err
    assert "could not parse manifest" in captured.err
    assert "Traceback" not in captured.err


def test_apt_is_reserved_source_keyword() -> None:
    from drix.main import _interpret, build_parser

    args = build_parser().parse_args(["apt", "libssl3"])
    mode, prefix, manifest, focus = _interpret(args)
    assert mode == "apt"
    assert prefix is None
    assert manifest is None
    assert focus == "libssl3"
