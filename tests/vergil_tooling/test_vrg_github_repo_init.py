from __future__ import annotations

import pytest

from vergil_tooling.bin.vrg_github_repo_init import parse_args


class TestParseArgs:
    def test_new_repo(self) -> None:
        args = parse_args(["vergil-project/vergil-vm"])
        assert args.repo == "vergil-project/vergil-vm"
        assert args.adopt is False

    def test_adopt_mode(self) -> None:
        args = parse_args(["--adopt"])
        assert args.adopt is True
        assert args.repo is None

    def test_visibility(self) -> None:
        args = parse_args(["vergil-project/vergil-vm", "--visibility", "private"])
        assert args.visibility == "private"

    def test_repo_format_validation(self) -> None:
        with pytest.raises(SystemExit):
            parse_args(["just-a-name"])

    def test_adopt_with_repo_is_error(self) -> None:
        with pytest.raises(SystemExit):
            parse_args(["vergil-project/vergil-vm", "--adopt"])
