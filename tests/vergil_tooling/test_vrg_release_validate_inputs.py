"""Tests for vrg-release-validate-inputs CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    import pytest

from vergil_tooling.bin.vrg_release_validate_inputs import main


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
        rc = main(["--language", "go", "--container-tag", "v1.0.0"])
    assert rc == 0


def test_reports_all_failures(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["--language", "unknown", "--registry-publish"])
    assert rc == 1


def test_container_tag_unsupported_language_fails(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("vergil_tooling.lib.output.is_ci", return_value=False),
        patch(
            "vergil_tooling.bin.vrg_release_validate_inputs._CONTAINER_LANGUAGES",
            frozenset(),
        ),
    ):
        rc = main(["--language", "python", "--container-tag", "v1.0.0"])
    assert rc == 1


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
