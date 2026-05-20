from __future__ import annotations

from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_github_repo_init import main, parse_args


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

    def test_no_args_is_error(self) -> None:
        with pytest.raises(SystemExit):
            parse_args([])


class TestMain:
    def test_adopt_mode_success(self) -> None:
        with (
            patch("vergil_tooling.lib.github.current_repo", return_value="org/repo"),
            patch(
                "vergil_tooling.bin.vrg_github_repo_init.prompt_yes_no",
                return_value=True,
            ),
            patch("vergil_tooling.bin.vrg_github_repo_init.run_wizard"),
        ):
            result = main(["--adopt"])

        assert result == 0

    def test_adopt_mode_abort(self) -> None:
        with (
            patch("vergil_tooling.lib.github.current_repo", return_value="org/repo"),
            patch(
                "vergil_tooling.bin.vrg_github_repo_init.prompt_yes_no",
                return_value=False,
            ),
        ):
            result = main(["--adopt"])

        assert result == 1

    def test_new_repo_with_visibility_arg(self) -> None:
        with (
            patch("vergil_tooling.bin.vrg_github_repo_init.prompt_free_text", return_value="desc"),
            patch("vergil_tooling.bin.vrg_github_repo_init.run_wizard") as mock_wizard,
        ):
            result = main(["org/repo", "--visibility", "private"])

        assert result == 0
        ctx = mock_wizard.call_args[0][0]
        assert ctx.visibility == "private"
        assert ctx.description == "desc"

    def test_new_repo_prompts_visibility(self) -> None:
        with (
            patch(
                "vergil_tooling.bin.vrg_github_repo_init.prompt_choice",
                return_value="public",
            ),
            patch("vergil_tooling.bin.vrg_github_repo_init.prompt_free_text", return_value="desc"),
            patch("vergil_tooling.bin.vrg_github_repo_init.run_wizard") as mock_wizard,
        ):
            result = main(["org/repo"])

        assert result == 0
        ctx = mock_wizard.call_args[0][0]
        assert ctx.visibility == "public"
