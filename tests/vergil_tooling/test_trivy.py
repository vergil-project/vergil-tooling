"""Tests for vergil_tooling.lib.trivy."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.lib.trivy import (
    _run_trivy_command,
    generate_sbom,
    run_scan,
)

if TYPE_CHECKING:
    from pathlib import Path

_MOD = "vergil_tooling.lib.trivy"


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

    def test_filesystem_uses_target_directly(self, tmp_path: Path) -> None:
        with patch(f"{_MOD}._run_trivy_command") as mock_run:
            mock_run.return_value.returncode = 0
            run_scan("filesystem", "/project", tmp_path)

        scan_cmd = mock_run.call_args_list[0][0][0]
        assert scan_cmd[-1] == "/project"
        assert scan_cmd[0] == "trivy"
        assert scan_cmd[1] == "filesystem"

    def test_image_uses_target_name(self, tmp_path: Path) -> None:
        with patch(f"{_MOD}._run_trivy_command") as mock_run:
            mock_run.return_value.returncode = 0
            run_scan("image", "myimage:latest", tmp_path)

        scan_cmd = mock_run.call_args_list[0][0][0]
        assert scan_cmd[-1] == "myimage:latest"
        assert scan_cmd[1] == "image"

    def test_result_paths(self, tmp_path: Path) -> None:
        with patch(f"{_MOD}._run_trivy_command") as mock_run:
            mock_run.return_value.returncode = 0
            result = run_scan("filesystem", "/project", tmp_path)

        assert result.sarif_path == str(tmp_path / "trivy-results.sarif")
        assert result.table_path == str(tmp_path / "trivy-results.table")

    def test_default_severity(self, tmp_path: Path) -> None:
        with patch(f"{_MOD}._run_trivy_command") as mock_run:
            mock_run.return_value.returncode = 0
            run_scan("filesystem", "/project", tmp_path)

        scan_cmd = mock_run.call_args_list[0][0][0]
        idx = scan_cmd.index("--severity")
        assert scan_cmd[idx + 1] == "MEDIUM,HIGH,CRITICAL"

    def test_custom_severity(self, tmp_path: Path) -> None:
        with patch(f"{_MOD}._run_trivy_command") as mock_run:
            mock_run.return_value.returncode = 0
            run_scan(
                "filesystem",
                "/project",
                tmp_path,
                severity="HIGH,CRITICAL",
            )

        scan_cmd = mock_run.call_args_list[0][0][0]
        idx = scan_cmd.index("--severity")
        assert scan_cmd[idx + 1] == "HIGH,CRITICAL"

    def test_trivyignore(self, tmp_path: Path) -> None:
        with patch(f"{_MOD}._run_trivy_command") as mock_run:
            mock_run.return_value.returncode = 0
            run_scan(
                "filesystem",
                "/project",
                tmp_path,
                trivyignore="/path/.trivyignore",
            )

        scan_cmd = mock_run.call_args_list[0][0][0]
        idx = scan_cmd.index("--ignorefile")
        assert scan_cmd[idx + 1] == "/path/.trivyignore"

    def test_no_docker_in_commands(self, tmp_path: Path) -> None:
        with patch(f"{_MOD}._run_trivy_command") as mock_run:
            mock_run.return_value.returncode = 0
            run_scan("filesystem", "/project", tmp_path)

        for call in mock_run.call_args_list:
            cmd = call[0][0]
            assert cmd[0] == "trivy"
            assert "docker" not in cmd

    def test_convert_commands_use_host_paths(self, tmp_path: Path) -> None:
        with patch(f"{_MOD}._run_trivy_command") as mock_run:
            mock_run.return_value.returncode = 0
            run_scan("filesystem", "/project", tmp_path)

        table_cmd = mock_run.call_args_list[1][0][0]
        assert table_cmd == [
            "trivy",
            "convert",
            "--format",
            "table",
            "--output",
            str(tmp_path / "trivy-results.table"),
            str(tmp_path / "trivy-results.json"),
        ]

        sarif_cmd = mock_run.call_args_list[2][0][0]
        assert sarif_cmd == [
            "trivy",
            "convert",
            "--format",
            "sarif",
            "--output",
            str(tmp_path / "trivy-results.sarif"),
            str(tmp_path / "trivy-results.json"),
        ]


class TestRunTrivyCommand:
    def test_delegates_to_subprocess(self) -> None:
        with patch(f"{_MOD}.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            result = _run_trivy_command(["trivy", "--version"])

        mock_run.assert_called_once_with(["trivy", "--version"], capture_output=True)
        assert result.returncode == 0


class TestGenerateSbom:
    def test_calls_trivy_directly(self, tmp_path: Path) -> None:
        output = tmp_path / "sbom.json"
        with patch(f"{_MOD}._run_trivy_command") as mock_run:
            mock_run.return_value.returncode = 0
            rc = generate_sbom("/project", output)

        assert rc == 0
        mock_run.assert_called_once_with(
            [
                "trivy",
                "filesystem",
                "--format",
                "cyclonedx",
                "--output",
                str(output),
                "/project",
            ]
        )

    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        output = tmp_path / "sub" / "sbom.json"
        with patch(f"{_MOD}._run_trivy_command") as mock_run:
            mock_run.return_value.returncode = 0
            generate_sbom("/project", output)

        assert output.parent.is_dir()
