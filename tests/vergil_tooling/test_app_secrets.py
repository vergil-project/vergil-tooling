"""Tests for the App secrets check, ``vrg-trusted-authors app-secrets``.

Every org, repo and login here is invented (``example-*``). The fake GitHub
answers the REST reads the module makes, flattened as
:func:`~vergil_tooling.lib.github.read_json_paginated` returns them: ``GET
orgs/{org}/repos`` (``full_name`` and ``archived`` among the repo fields),
``GET repos/{owner}/{repo}/actions/workflows`` (``path`` among the workflow
fields), and ``GET repos/{owner}/{repo}/actions/secrets``,
``.../actions/organization-secrets`` and
``.../environments/automation/secrets``, after the ``--jq`` projection that
keeps only each secret's ``name``. A repo with no ``automation`` environment
answers the last with gh's ``Not Found (HTTP 404)``. The secrets APIs return
no value; the fake holds none either.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vergil_tooling.bin import vrg_trusted_authors
from vergil_tooling.lib import app_secrets, gh_version, github
from vergil_tooling.lib.app_secrets import (
    ENVIRONMENT,
    MISSING,
    ORG,
    REPO,
    AppSecretsError,
    check_repo,
    in_scope_repos,
)
from vergil_tooling.lib.identity_mode import IdentityMode

OWNER = "example-org"
OTHER_OWNER = "example-other-org"

_IDENTITIES = """\
[trusted_authors]
human = ["example-human"]
agents = ["example-agent"]

[orgs.example-org]
automation_app = "example-org-automation"

[orgs.example-other-org]
automation_app = "example-other-automation"
"""

_BOTH = ["APP_CLIENT_ID", "APP_PRIVATE_KEY"]
_NO_ENVIRONMENT = None


class FakeRepo:
    def __init__(
        self,
        *,
        workflows: list[str] | None = None,
        environment: bool = True,
        repo_secrets: list[str] | None = None,
        org_secrets: list[str] | None = None,
        archived: bool = False,
    ) -> None:
        self.workflows = workflows if workflows is not None else [".github/workflows/cd.yml"]
        # None: the repo has no automation environment.
        self.env_secrets: list[str] | None = list(_BOTH) if environment else _NO_ENVIRONMENT
        self.repo_secrets = repo_secrets if repo_secrets is not None else []
        self.org_secrets = org_secrets if org_secrets is not None else []
        self.archived = archived


def _gh_error(endpoint: str, stderr: str) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(1, ("gh", "api", "--paginate", endpoint), "", stderr)


class FakeGitHub:
    """Answers the module's REST reads from a table of repos."""

    def __init__(self, repos: dict[str, FakeRepo], *, fail: str | None = None) -> None:
        self.repos = repos
        self.fail = fail
        self.fail_stderr = "gh: Resource not accessible by integration (HTTP 403)"
        self.reads: list[str] = []

    def read_json_paginated(
        self, endpoint: str, key: str | None = None, *, jq: str | None = None
    ) -> list[object]:
        self.reads.append(endpoint)
        if self.fail is not None and endpoint.startswith(self.fail):
            raise _gh_error(endpoint, self.fail_stderr)
        for org in (OWNER, OTHER_OWNER):
            if endpoint == f"orgs/{org}/repos?type=all":
                assert key is None
                assert jq is None
                return [
                    {"id": 1, "name": full.split("/")[1], "full_name": full, "archived": r.archived}
                    for full, r in self.repos.items()
                    if full.startswith(f"{org}/")
                ]
        for full, repo in self.repos.items():
            if endpoint == f"repos/{full}/actions/workflows":
                assert key == "workflows"
                return [{"id": 1, "name": p, "path": p, "state": "active"} for p in repo.workflows]
            listings = {
                f"repos/{full}/actions/secrets": repo.repo_secrets,
                f"repos/{full}/actions/organization-secrets": repo.org_secrets,
                f"repos/{full}/environments/automation/secrets": repo.env_secrets,
            }
            if endpoint in listings:
                assert key == "secrets"
                # Names only, projected at the source.
                assert jq == "{secrets: [.secrets[] | {name}]}"
                names = listings[endpoint]
                if names is None:
                    raise _gh_error(endpoint, "gh: Not Found (HTTP 404)")
                return [{"name": name} for name in names]
        msg = f"unexpected endpoint {endpoint}"
        raise AssertionError(msg)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    gh = FakeGitHub(
        {
            f"{OWNER}/repo-a": FakeRepo(),
            f"{OWNER}/repo-b": FakeRepo(workflows=[".github/workflows/ops.yml"]),
            # No in-scope workflow: not checked.
            f"{OWNER}/repo-c": FakeRepo(workflows=[".github/workflows/ci.yml"], environment=False),
            # Archived: not checked, and its workflows are not read.
            f"{OWNER}/repo-d": FakeRepo(archived=True, environment=False),
        }
    )
    monkeypatch.setattr(app_secrets.github, "read_json_paginated", gh.read_json_paginated)
    return gh


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("VRG_TRUSTED_AUTHORS", raising=False)


def _write_identities(tmp_path: Path) -> None:
    d = tmp_path / ".config" / "vergil"
    d.mkdir(parents=True, exist_ok=True)
    (d / "identities.toml").write_text(_IDENTITIES)


def _set_mode(monkeypatch: pytest.MonkeyPatch, mode: IdentityMode) -> None:
    monkeypatch.setattr(vrg_trusted_authors.identity_mode, "current_mode", lambda: mode)


class TestLibrary:
    def test_in_scope_repos_skip_archived_and_out_of_scope(self, fake: FakeGitHub) -> None:
        assert in_scope_repos(OWNER) == [f"{OWNER}/repo-a", f"{OWNER}/repo-b"]
        assert f"repos/{OWNER}/repo-d/actions/workflows" not in fake.reads

    def test_check_repo_environment_present(self, fake: FakeGitHub) -> None:
        result = check_repo(f"{OWNER}/repo-a")
        assert result.sources == {"APP_CLIENT_ID": ENVIRONMENT, "APP_PRIVATE_KEY": ENVIRONMENT}
        assert result.has_environment
        assert result.missing == []
        assert result.forbidden == []

    def test_check_repo_environment_missing_one(self, fake: FakeGitHub) -> None:
        fake.repos[f"{OWNER}/repo-a"].env_secrets = ["APP_CLIENT_ID"]
        result = check_repo(f"{OWNER}/repo-a")
        assert result.sources == {"APP_CLIENT_ID": ENVIRONMENT, "APP_PRIVATE_KEY": MISSING}
        assert result.has_environment
        assert result.missing == ["APP_PRIVATE_KEY"]

    def test_check_repo_no_environment_misses_both(self, fake: FakeGitHub) -> None:
        result = check_repo(f"{OWNER}/repo-c")
        assert result.sources == {"APP_CLIENT_ID": MISSING, "APP_PRIVATE_KEY": MISSING}
        assert not result.has_environment
        assert result.missing == _BOTH

    def test_repo_or_org_copy_is_forbidden_not_a_source(self, fake: FakeGitHub) -> None:
        # A repo- or org-level copy never counts as present, even with the
        # environment absent, and each is reported.
        repo = fake.repos[f"{OWNER}/repo-c"]
        repo.repo_secrets = ["APP_PRIVATE_KEY"]
        repo.org_secrets = list(_BOTH)
        result = check_repo(f"{OWNER}/repo-c")
        assert result.missing == _BOTH
        assert result.forbidden == [
            ("APP_CLIENT_ID", ORG),
            ("APP_PRIVATE_KEY", REPO),
            ("APP_PRIVATE_KEY", ORG),
        ]

    def test_secret_name_case_is_ignored(self, fake: FakeGitHub) -> None:
        repo = fake.repos[f"{OWNER}/repo-a"]
        repo.env_secrets = ["app_client_id", "App_Private_Key"]
        repo.org_secrets = ["app_client_id"]
        result = check_repo(f"{OWNER}/repo-a")
        assert result.missing == []
        assert result.forbidden == [("APP_CLIENT_ID", ORG)]

    def test_environment_read_after_the_repo_secrets(self, fake: FakeGitHub) -> None:
        # The 404 is read as "no environment" only once the repo's own
        # secrets list has been read, so it cannot be the repo's.
        check_repo(f"{OWNER}/repo-c")
        assert fake.reads == [
            f"repos/{OWNER}/repo-c/actions/secrets",
            f"repos/{OWNER}/repo-c/actions/organization-secrets",
            f"repos/{OWNER}/repo-c/environments/automation/secrets",
        ]

    @pytest.mark.parametrize(
        "failing",
        [
            f"orgs/{OWNER}/repos",
            f"repos/{OWNER}/repo-a/actions/workflows",
        ],
    )
    def test_in_scope_read_error_raises(self, fake: FakeGitHub, failing: str) -> None:
        fake.fail = failing
        with pytest.raises(AppSecretsError, match=rf"(?s)GET {failing}.* failed: .*HTTP 403"):
            in_scope_repos(OWNER)

    @pytest.mark.parametrize(
        "failing", ["actions/secrets", "actions/organization-secrets", "environments/automation"]
    )
    def test_secrets_read_error_raises(self, fake: FakeGitHub, failing: str) -> None:
        fake.fail = f"repos/{OWNER}/repo-a/{failing}"
        with pytest.raises(AppSecretsError, match="HTTP 403"):
            check_repo(f"{OWNER}/repo-a")

    @pytest.mark.parametrize("failing", ["actions/secrets", "actions/organization-secrets"])
    def test_a_404_on_a_repo_or_org_listing_is_an_error(
        self, fake: FakeGitHub, failing: str
    ) -> None:
        # Only the environment's 404 means absence; the repo's is a failure.
        fake.fail = f"repos/{OWNER}/repo-a/{failing}"
        fake.fail_stderr = "gh: Not Found (HTTP 404)"
        with pytest.raises(AppSecretsError, match="HTTP 404"):
            check_repo(f"{OWNER}/repo-a")

    def test_a_404_elsewhere_in_the_output_is_an_error(self, fake: FakeGitHub) -> None:
        fake.fail = f"repos/{OWNER}/repo-a/environments/automation"
        fake.fail_stderr = "gh: Server Error (HTTP 500)\nrequest id 404"
        with pytest.raises(AppSecretsError, match="HTTP 500"):
            check_repo(f"{OWNER}/repo-a")

    def test_unknown_shape_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            app_secrets.github,
            "read_json_paginated",
            lambda endpoint, key=None, *, jq=None: [{"value": "x"}],
        )
        with pytest.raises(AppSecretsError, match="no string 'name'"):
            check_repo(f"{OWNER}/repo-a")

    def test_item_not_an_object_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            app_secrets.github,
            "read_json_paginated",
            lambda endpoint, key=None, *, jq=None: ["APP_CLIENT_ID"],
        )
        with pytest.raises(AppSecretsError, match="not an object"):
            check_repo(f"{OWNER}/repo-a")

    @pytest.mark.parametrize("bad", ["secrets", "environments/automation/secrets"])
    def test_bad_paginated_page_raises(self, monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
        def _bad_page(
            endpoint: str, key: str | None = None, *, jq: str | None = None
        ) -> list[object]:
            if endpoint.endswith(bad):
                msg = f"paginated response from {endpoint!r} has no 'secrets' list"
                raise ValueError(msg)
            return []

        monkeypatch.setattr(app_secrets.github, "read_json_paginated", _bad_page)
        with pytest.raises(AppSecretsError, match="has no 'secrets' list"):
            check_repo(f"{OWNER}/repo-a")

    def test_repo_without_archived_flag_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            app_secrets.github,
            "read_json_paginated",
            lambda endpoint, key=None, *, jq=None: [{"full_name": f"{OWNER}/repo-a"}],
        )
        with pytest.raises(AppSecretsError, match="no boolean 'archived'"):
            in_scope_repos(OWNER)


class TestCli:
    def test_all_present_exits_0(
        self, fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _set_mode(monkeypatch, IdentityMode.HUMAN)
        assert vrg_trusted_authors.main(["app-secrets", "--org", OWNER]) == 0
        assert capsys.readouterr().out == (
            f"{OWNER}/repo-a: APP_CLIENT_ID=environment APP_PRIVATE_KEY=environment\n"
            f"{OWNER}/repo-b: APP_CLIENT_ID=environment APP_PRIVATE_KEY=environment\n"
            "the App secrets reach every repo checked (2), from the automation environment\n"
        )

    def test_missing_exits_1_naming_each(
        self, fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _set_mode(monkeypatch, IdentityMode.AUTOMATION)
        fake.repos[f"{OWNER}/repo-a"].env_secrets = ["APP_CLIENT_ID"]
        fake.repos[f"{OWNER}/repo-b"].env_secrets = _NO_ENVIRONMENT
        assert vrg_trusted_authors.main(["app-secrets", "--org", OWNER]) == 1
        out = capsys.readouterr().out
        assert out == (
            f"{OWNER}/repo-a: APP_CLIENT_ID=environment APP_PRIVATE_KEY=MISSING\n"
            f"{OWNER}/repo-b: APP_CLIENT_ID=MISSING APP_PRIVATE_KEY=MISSING"
            " (no automation environment)\n"
            f"FINDING: {OWNER}/repo-a: the automation environment secret APP_PRIVATE_KEY"
            " is missing\n"
            f"FINDING: {OWNER}/repo-b: the automation environment secret APP_CLIENT_ID"
            " is missing\n"
            f"FINDING: {OWNER}/repo-b: the automation environment secret APP_PRIVATE_KEY"
            " is missing\n"
        )

    def test_forbidden_copy_exits_1_even_when_the_environment_has_both(
        self, fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _set_mode(monkeypatch, IdentityMode.HUMAN)
        fake.repos[f"{OWNER}/repo-a"].repo_secrets = ["APP_CLIENT_ID"]
        fake.repos[f"{OWNER}/repo-b"].org_secrets = ["APP_PRIVATE_KEY"]
        assert vrg_trusted_authors.main(["app-secrets", "--org", OWNER]) == 1
        out = capsys.readouterr().out
        assert f"{OWNER}/repo-a: APP_CLIENT_ID=environment APP_PRIVATE_KEY=environment\n" in out
        assert out.endswith(
            f"FINDING: {OWNER}/repo-a: APP_CLIENT_ID is also a repository secret, which only"
            " the automation environment may hold\n"
            f"FINDING: {OWNER}/repo-b: APP_PRIVATE_KEY is also an organization secret, which"
            " only the automation environment may hold\n"
        )
        assert "reach every repo" not in out

    def test_named_repos_are_checked_whatever_their_workflows(
        self, fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _set_mode(monkeypatch, IdentityMode.HUMAN)
        argv = ["app-secrets", "--repo", f"{OWNER}/repo-c", "--repo", f"{OWNER}/repo-a"]
        assert vrg_trusted_authors.main(argv) == 1
        out = capsys.readouterr().out
        assert (
            f"FINDING: {OWNER}/repo-c: the automation environment secret APP_CLIENT_ID is missing"
            in out
        )
        assert f"{OWNER}/repo-a: APP_CLIENT_ID=environment APP_PRIVATE_KEY=environment" in out
        assert not any(read.endswith("/workflows") or "/repos?" in read for read in fake.reads)

    def test_every_listed_org_by_default(
        self,
        fake: FakeGitHub,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _write_identities(tmp_path)
        _set_mode(monkeypatch, IdentityMode.HUMAN)
        assert vrg_trusted_authors.main(["app-secrets"]) == 0
        out = capsys.readouterr().out
        assert f"{OTHER_OWNER}: no repo runs an in-scope job\n" in out
        assert out.endswith(
            "the App secrets reach every repo checked (2), from the automation environment\n"
        )

    def test_no_configuration_exits_2(
        self, fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _set_mode(monkeypatch, IdentityMode.HUMAN)
        assert vrg_trusted_authors.main(["app-secrets"]) == 2
        assert "ERROR:" in capsys.readouterr().err
        assert fake.reads == []

    @pytest.mark.parametrize(
        "failing",
        [
            f"orgs/{OWNER}/repos",
            f"repos/{OWNER}/repo-b/actions/organization-secrets",
            f"repos/{OWNER}/repo-b/environments/automation/secrets",
        ],
    )
    def test_read_failure_exits_2(
        self,
        fake: FakeGitHub,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        failing: str,
    ) -> None:
        _set_mode(monkeypatch, IdentityMode.AUTOMATION)
        fake.fail = failing
        assert vrg_trusted_authors.main(["app-secrets", "--org", OWNER]) == 2
        captured = capsys.readouterr()
        assert "HTTP 403" in captured.err
        # A failed read is never reported as present or missing.
        assert "FINDING" not in captured.out
        assert "reach every repo" not in captured.out

    @pytest.mark.parametrize("mode", [IdentityMode.USER, IdentityMode.AUDIT])
    def test_refuses_agent_mode(
        self,
        fake: FakeGitHub,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        mode: IdentityMode,
    ) -> None:
        _set_mode(monkeypatch, mode)
        assert vrg_trusted_authors.main(["app-secrets", "--org", OWNER]) == 2
        err = capsys.readouterr().err
        assert "app-secrets runs as the org automation App or the human" in err
        assert "an agent mode, which holds no Secrets permission" in err
        assert fake.reads == []

    def test_refuses_unresolvable_identity(
        self, fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def _unresolvable() -> IdentityMode:
            msg = "the token belongs to someone else"
            raise vrg_trusted_authors.AutomationIdentityError(msg)

        monkeypatch.setattr(vrg_trusted_authors.identity_mode, "current_mode", _unresolvable)
        assert vrg_trusted_authors.main(["app-secrets", "--org", OWNER]) == 2
        assert "could not be resolved" in capsys.readouterr().err
        assert fake.reads == []

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (
                github.NoInstallationError(OWNER, ["other-org"]),
                f"no installation for owner '{OWNER}'. Known installations: other-org.",
            ),
            (
                github.AppAuthError(OWNER, "123", Path("/key.pem"), "the key was rejected"),
                "the key was rejected",
            ),
            (gh_version.GhVersionError("gh 2.0.0 is older than 2.97.0"), "older than 2.97.0"),
        ],
    )
    def test_auth_or_setup_error_exits_2(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        error: Exception,
        expected: str,
    ) -> None:
        # A GitHub login that failed read no secret: "could not run" (2), not
        # "a secret is missing" (1), the code cli_main would give it.
        _set_mode(monkeypatch, IdentityMode.AUTOMATION)

        def _fail(*_args: object, **_kwargs: object) -> list[object]:
            raise error

        monkeypatch.setattr(app_secrets.github, "read_json_paginated", _fail)
        assert vrg_trusted_authors.main(["app-secrets", "--org", OWNER]) == 2
        captured = capsys.readouterr()
        assert expected in captured.err
        assert "FINDING" not in captured.out

    @pytest.mark.parametrize("value", ["repo-a", "/repo-a", f"{OWNER}/", f"{OWNER}/a/b"])
    def test_bad_repo_argument_is_a_usage_error(
        self, value: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            vrg_trusted_authors.main(["app-secrets", "--repo", value])
        assert exc.value.code == 2
        assert "expected OWNER/NAME" in capsys.readouterr().err

    def test_org_and_repo_are_exclusive(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            vrg_trusted_authors.main(["app-secrets", "--org", OWNER, "--repo", f"{OWNER}/repo-a"])
        assert exc.value.code == 2
        assert "not allowed with argument" in capsys.readouterr().err
