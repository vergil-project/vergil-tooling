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

    def test_wizard_flags_parse(self) -> None:
        args = parse_args(
            [
                "org/repo",
                "--description",
                "A test repo",
                "--repository-type",
                "tooling",
                "--language",
                "python",
                "--branching-model",
                "library-release",
                "--versioning-scheme",
                "semver",
                "--release-model",
                "tagged-release",
                "--versions",
                "3.12,3.13",
                "--integration-tests",
                "--no-publish-release",
                "--publish-docs",
                "--vergil-version",
                "v2.1",
                "--license",
                "Apache-2.0",
                "--initial-version",
                "1.0.0",
            ]
        )
        assert args.description == "A test repo"
        assert args.repository_type == "tooling"
        assert args.language == "python"
        assert args.branching_model == "library-release"
        assert args.versioning_scheme == "semver"
        assert args.release_model == "tagged-release"
        assert args.versions == "3.12,3.13"
        assert args.integration_tests is True
        assert args.publish_release is False
        assert args.publish_docs is True
        assert args.vergil_version == "v2.1"
        assert args.license == "Apache-2.0"
        assert args.initial_version == "1.0.0"

    def test_boolean_flags_default_none(self) -> None:
        # Unset boolean flags stay None so the wizard can tell "not supplied"
        # from an explicit true/false.
        args = parse_args(["org/repo"])
        assert args.integration_tests is None
        assert args.publish_release is None
        assert args.publish_docs is None
        assert args.non_interactive is False

    def test_yes_is_alias_for_non_interactive(self) -> None:
        args = parse_args(["--adopt", "--yes"])
        assert args.non_interactive is True

    def test_invalid_enum_choice_rejected(self) -> None:
        with pytest.raises(SystemExit):
            parse_args(["org/repo", "--repository-type", "not-a-type"])

    def test_non_interactive_requires_flags_for_new_repo(self) -> None:
        with pytest.raises(SystemExit):
            parse_args(["org/repo", "--non-interactive"])

    def test_non_interactive_missing_flags_names_each(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            parse_args(
                [
                    "org/repo",
                    "--non-interactive",
                    "--description",
                    "d",
                    "--repository-type",
                    "tooling",
                ]
            )
        err = capsys.readouterr().err
        # Inspect the error message line, not the usage banner (which lists
        # every flag).
        message = next(line for line in err.splitlines() if "requires these flags" in line)
        assert "--branching-model" in message
        assert "--versioning-scheme" in message
        assert "--release-model" in message
        # Supplied flags are not reported as missing.
        assert "--description" not in message
        assert "--repository-type" not in message

    def test_non_interactive_with_all_required_flags_ok(self) -> None:
        args = parse_args(
            [
                "org/repo",
                "--non-interactive",
                "--description",
                "d",
                "--repository-type",
                "tooling",
                "--branching-model",
                "library-release",
                "--versioning-scheme",
                "semver",
                "--release-model",
                "tagged-release",
            ]
        )
        assert args.non_interactive is True

    def test_adopt_non_interactive_needs_no_required_flags(self) -> None:
        # Adopt draws the required values from the existing vergil.toml, so
        # parse_args does not demand them.
        args = parse_args(["--adopt", "--non-interactive"])
        assert args.non_interactive is True
        assert args.adopt is True


class TestMain:
    def test_aborts_under_agent_identity(self, capsys: pytest.CaptureFixture[str]) -> None:
        # gh repo create + org-level setup need human credentials; an agent
        # identity must hard-abort with a clear message before any side effect,
        # not crash deep in the call stack (issue #2391).
        with (
            patch(
                "vergil_tooling.bin.vrg_github_repo_init.identity_mode.is_agent",
                return_value=True,
            ),
            patch("vergil_tooling.bin.vrg_github_repo_init.run_wizard") as mock_wizard,
            patch("vergil_tooling.lib.github.current_repo") as mock_current_repo,
        ):
            result = main(["org/repo"])

        assert result == 1
        mock_wizard.assert_not_called()
        mock_current_repo.assert_not_called()
        err = capsys.readouterr().err
        assert "human" in err.lower()
        assert "gh repo create" in err

    def test_agent_refusal_precedes_adopt_side_effects(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The refusal fires before adopt resolves the repo or prompts, so an
        # agent hits it in every mode.
        with (
            patch(
                "vergil_tooling.bin.vrg_github_repo_init.identity_mode.is_agent",
                return_value=True,
            ),
            patch("vergil_tooling.lib.github.current_repo") as mock_current_repo,
            patch("vergil_tooling.bin.vrg_github_repo_init.run_wizard") as mock_wizard,
        ):
            result = main(["--adopt"])

        assert result == 1
        mock_current_repo.assert_not_called()
        mock_wizard.assert_not_called()

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

    def test_non_interactive_new_repo_threads_flags_without_prompting(self) -> None:
        argv = [
            "org/repo",
            "--non-interactive",
            "--description",
            "A scripted repo",
            "--repository-type",
            "tooling",
            "--language",
            "python",
            "--branching-model",
            "library-release",
            "--versioning-scheme",
            "semver",
            "--release-model",
            "tagged-release",
            "--visibility",
            "private",
            "--no-integration-tests",
            "--publish-release",
            "--no-publish-docs",
            "--vergil-version",
            "v2.1",
            "--license",
            "Apache-2.0",
            "--initial-version",
            "1.2.3",
        ]
        with (
            patch("builtins.input", side_effect=AssertionError("prompted in non-interactive mode")),
            patch("vergil_tooling.bin.vrg_github_repo_init.run_wizard") as mock_wizard,
        ):
            result = main(argv)

        assert result == 0
        ctx = mock_wizard.call_args[0][0]
        assert ctx.non_interactive is True
        assert ctx.visibility == "private"
        assert ctx.description == "A scripted repo"
        assert ctx.opt_repository_type == "tooling"
        assert ctx.opt_primary_language == "python"
        assert ctx.opt_branching_model == "library-release"
        assert ctx.opt_versioning_scheme == "semver"
        assert ctx.opt_release_model == "tagged-release"
        assert ctx.opt_integration_tests is False
        assert ctx.opt_publish_release is True
        assert ctx.opt_publish_docs is False
        assert ctx.opt_vergil_version == "v2.1"
        assert ctx.opt_license_type == "Apache-2.0"
        assert ctx.opt_initial_version == "1.2.3"

    def test_non_interactive_defaults_visibility_public(self) -> None:
        argv = [
            "org/repo",
            "--non-interactive",
            "--description",
            "d",
            "--repository-type",
            "tooling",
            "--branching-model",
            "library-release",
            "--versioning-scheme",
            "semver",
            "--release-model",
            "tagged-release",
        ]
        with (
            patch("builtins.input", side_effect=AssertionError("prompted in non-interactive mode")),
            patch("vergil_tooling.bin.vrg_github_repo_init.run_wizard") as mock_wizard,
        ):
            result = main(argv)

        assert result == 0
        ctx = mock_wizard.call_args[0][0]
        assert ctx.visibility == "public"

    def test_adopt_yes_skips_continue_prompt(self) -> None:
        with (
            patch("vergil_tooling.lib.github.current_repo", return_value="org/repo"),
            patch(
                "vergil_tooling.bin.vrg_github_repo_init.prompt_yes_no",
                side_effect=AssertionError("prompted under --yes"),
            ),
            patch("vergil_tooling.bin.vrg_github_repo_init.run_wizard") as mock_wizard,
        ):
            result = main(["--adopt", "--yes"])

        assert result == 0
        ctx = mock_wizard.call_args[0][0]
        assert ctx.non_interactive is True

    def test_adopt_description_flag_overrides_empty_default(self) -> None:
        with (
            patch("vergil_tooling.lib.github.current_repo", return_value="org/repo"),
            patch(
                "vergil_tooling.bin.vrg_github_repo_init.prompt_yes_no",
                return_value=True,
            ),
            patch("vergil_tooling.bin.vrg_github_repo_init.run_wizard") as mock_wizard,
        ):
            result = main(["--adopt", "--description", "Adopted repo"])

        assert result == 0
        ctx = mock_wizard.call_args[0][0]
        assert ctx.description == "Adopted repo"
