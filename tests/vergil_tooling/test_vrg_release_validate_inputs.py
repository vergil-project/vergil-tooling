"""Tests for vrg-release-validate-inputs CLI."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_release_validate_inputs import main

# -- New --language flag form --------------------------------------------------


def test_valid_python_release(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["--language", "python"])
    assert rc == 0


def test_valid_python_with_registry_publish(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["--language", "python", "--registry-publish"])
    assert rc == 0


def test_unsupported_language_fails(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["--language", "unknown"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "unsupported" in captured.err.lower() or "unsupported" in captured.out.lower()


def test_go_with_registry_publish_fails(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["--language", "go", "--registry-publish"])
    assert rc == 1


def test_container_tag_accepted(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["--language", "go", "--container-tag", "1.26"])
    assert rc == 0


def test_reports_all_failures(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["--language", "unknown", "--registry-publish"])
    assert rc == 1


# -- Container tag shape (issue #3494) ------------------------------------------
#
# cd-release always passes the resolver's primary-container-tag: the window's
# version with the family prefix dropped (clang-20 -> 20 on prod-cpp-clang,
# node-24 -> 24 on prod-ts-node). Every language accepts it, and the tag must
# be a dotted-numeric version.


@pytest.mark.parametrize(
    ("language", "tag"),
    [
        ("python", "3.14"),
        ("java", "21"),
        ("go", "1.26"),
        ("rust", "1.93"),
        ("cpp", "20"),  # clang-20, suffix cpp-clang
        ("cpp", "14"),  # gcc-14, suffix cpp-gcc
        ("typescript", "24"),  # node-24, suffix ts-node
    ],
)
def test_resolver_container_tag_accepted(
    language: str, tag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["--language", language, "--container-tag", tag])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert captured.err == ""


@pytest.mark.parametrize(("language", "tag"), [("cpp", "20"), ("typescript", "24")])
def test_positional_resolver_container_tag_accepted(
    language: str, tag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # The validate-inputs action passes the language positionally.
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main([language, "--container-tag", tag])
    assert rc == 0


@pytest.mark.parametrize(
    ("language", "tag"),
    [
        ("cpp", "clang-20"),  # the version token, not the image tag
        ("typescript", "node-24"),
        ("python", "latest"),  # language images publish no :latest tag
        ("go", "v1.0.0"),
        ("rust", "1..93"),
        ("java", "21."),
        ("cpp", " 20"),
        ("typescript", "24\n"),
    ],
)
def test_malformed_container_tag_fails(
    language: str, tag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["--language", language, "--container-tag", tag])
    captured = capsys.readouterr()
    assert rc == 1
    assert f"--container-tag {tag!r} is not a {language} image tag" in captured.err
    assert "dotted-numeric" in captured.err


def test_malformed_container_tag_reported_with_registry_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["--language", "go", "--registry-publish", "--container-tag", "latest"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "--registry-publish is not supported for go" in captured.err
    assert "--container-tag 'latest' is not a go image tag" in captured.err


# -- No language ---------------------------------------------------------------


def test_no_language_passes(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main([])
    assert rc == 0


def test_no_language_ignores_registry_publish(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["--registry-publish"])
    assert rc == 0


def test_no_language_ignores_container_tag(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["--container-tag", "latest"])
    assert rc == 0


# -- Deprecated positional form (backward compatibility) -----------------------


def test_positional_python_accepted(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["python"])
    assert rc == 0


def test_positional_python_with_flags(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["python", "--registry-publish"])
    assert rc == 0


def test_positional_go_with_registry_publish_fails(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["go", "--registry-publish"])
    assert rc == 1


def test_positional_base_skips_validation(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["base"])
    assert rc == 0


def test_positional_base_with_container_tag_passes(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["base", "--container-tag", "latest"])
    assert rc == 0


def test_positional_unknown_skips_validation(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["unknown"])
    assert rc == 0
