"""Tests for vergil_tooling.lib.trivy."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.lib.trivy import (
    _run_trivy_command,
    build_docker_args,
    build_sbom_args,
    generate_sbom,
    run_scan,
)

if TYPE_CHECKING:
    from pathlib import Path

_MOD = "vergil_tooling.lib.trivy"


class TestBuildDockerArgs:
    def test_filesystem_scan(self) -> None:
        args = build_docker_args("filesystem", "/project", "/out")
        assert "-v" in args
        assert "/project:/scan:ro" in args
        assert "aquasec/trivy:latest" in args

    def test_image_scan(self) -> None:
        args = build_docker_args("image", "myimage:latest", "/out")
        assert "/var/run/docker.sock:/var/run/docker.sock" in args
        assert "/project:/scan:ro" not in list(args)

    def test_trivyignore(self) -> None:
        args = build_docker_args(
            "filesystem",
            "/project",
            "/out",
            trivyignore="/path/.trivyignore",
        )
        assert "/path/.trivyignore:/trivyignore:ro" in args
        assert "TRIVY_IGNOREFILE=/trivyignore" in args

    def test_custom_severity(self) -> None:
        args = build_docker_args(
            "filesystem",
            "/project",
            "/out",
            severity="HIGH,CRITICAL",
        )
        idx = args.index("--severity")
        assert args[idx + 1] == "HIGH,CRITICAL"

    def test_default_severity(self) -> None:
        args = build_docker_args("filesystem", "/project", "/out")
        idx = args.index("--severity")
        assert args[idx + 1] == "MEDIUM,HIGH,CRITICAL"

    def test_output_volume(self) -> None:
        args = build_docker_args("filesystem", "/project", "/out")
        assert "/out:/output" in args


class TestBuildSbomArgs:
    def test_structure(self) -> None:
        args = build_sbom_args("/project", "sbom.json")
        assert "aquasec/trivy:latest" in args
        assert "cyclonedx" in args
        assert "/project:/scan:ro" in args


class TestRunScan:
    def test_successful_scan(self, tmp_path: Path) -> None:
        with patch(f"{_MOD}._run_trivy_command") as mock_run:
            mock_run.return_value.returncode = 0
            result = run_scan("filesystem", "/project", tmp_path)

        assert result.returncode == 0
        assert mock_run.call_count == 3

    def test_scan_failure(self, tmp_path: Path) -> None:
        with patch(f"{_MOD}._run_trivy_command") as mock_run:
            mock_run.return_value.returncode = 2
            result = run_scan("filesystem", "/project", tmp_path)

        assert result.returncode == 2
        mock_run.assert_called_once()

    def test_creates_output_dir(self, tmp_path: Path) -> None:
        out = tmp_path / "subdir" / "output"
        with patch(f"{_MOD}._run_trivy_command") as mock_run:
            mock_run.return_value.returncode = 0
            run_scan("filesystem", "/project", out)

        assert out.is_dir()

    def test_filesystem_uses_scan_target(self, tmp_path: Path) -> None:
        with patch(f"{_MOD}._run_trivy_command") as mock_run:
            mock_run.return_value.returncode = 0
            run_scan("filesystem", "/project", tmp_path)

        scan_cmd = mock_run.call_args_list[0][0][0]
        assert scan_cmd[-1] == "/scan"

    def test_image_uses_target_name(self, tmp_path: Path) -> None:
        with patch(f"{_MOD}._run_trivy_command") as mock_run:
            mock_run.return_value.returncode = 0
            run_scan("image", "myimage:latest", tmp_path)

        scan_cmd = mock_run.call_args_list[0][0][0]
        assert scan_cmd[-1] == "myimage:latest"

    def test_result_paths(self, tmp_path: Path) -> None:
        with patch(f"{_MOD}._run_trivy_command") as mock_run:
            mock_run.return_value.returncode = 0
            result = run_scan("filesystem", "/project", tmp_path)

        assert result.sarif_path == str(tmp_path / "trivy-results.sarif")
        assert result.table_path == str(tmp_path / "trivy-results.table")


class TestRunTrivyCommand:
    def test_delegates_to_subprocess(self) -> None:
        with patch(f"{_MOD}.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            result = _run_trivy_command(["trivy", "--version"])

        mock_run.assert_called_once_with(["trivy", "--version"], capture_output=True)
        assert result.returncode == 0


class TestGenerateSbom:
    def test_calls_docker(self, tmp_path: Path) -> None:
        output = tmp_path / "sbom.json"
        with patch(f"{_MOD}._run_trivy_command") as mock_run:
            mock_run.return_value.returncode = 0
            rc = generate_sbom("/project", output)

        assert rc == 0
        mock_run.assert_called_once()

    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        output = tmp_path / "sub" / "sbom.json"
        with patch(f"{_MOD}._run_trivy_command") as mock_run:
            mock_run.return_value.returncode = 0
            generate_sbom("/project", output)

        assert output.parent.is_dir()
