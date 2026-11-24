"""Tests for publishing and checking the Actions copies of the trusted authors.

Every login, slug and org here is invented (``example-*``). The fake GitHub
returns the field shapes of the REST responses the module reads: ``GET
orgs/{org}/actions/variables`` (``name``, ``value``, ``created_at``,
``updated_at``, ``visibility``), ``GET orgs/{org}/repos`` (``full_name`` among
the repo fields) and ``GET repos/{owner}/{repo}/actions/variables`` (as the org
list, without ``visibility``), flattened as
:func:`~vergil_tooling.lib.github.read_json_paginated` returns them.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import cast

import pytest

from vergil_tooling.bin import vrg_trusted_authors
from vergil_tooling.lib import gh_version, github, trust_publish
from vergil_tooling.lib.identity_mode import IdentityMode
from vergil_tooling.lib.trust_publish import (
    ORG_VARIABLES,
    PublishRefusedError,
    TrustPublishError,
    check,
    expected_values,
    publish,
)
from vergil_tooling.lib.trusted_authors import (
    OrgConfig,
    TrustedAuthors,
    parse_actions_value,
    render_actions_value,
)

ORG = "example-org"
OTHER_ORG = "example-other-org"
AUTHORS = TrustedAuthors(
    human=("example-human",),
    agents=("example-agent", "example-audit-agent"),
    orgs={
        ORG: OrgConfig(automation_app="example-org-automation"),
        OTHER_ORG: OrgConfig(automation_app="example-other-automation"),
    },
)

_IDENTITIES = """\
[trusted_authors]
human = ["example-human"]
agents = ["example-agent", "example-audit-agent"]

[orgs.example-org]
automation_app = "example-org-automation"

[orgs.example-other-org]
automation_app = "example-other-automation"
"""

_STAMP = "2026-11-20T10:00:00Z"


def _org_variable(name: str, value: str, visibility: str = "all") -> dict[str, object]:
    return {
        "name": name,
        "value": value,
        "created_at": _STAMP,
        "updated_at": _STAMP,
        "visibility": visibility,
    }


def _repo_variable(name: str, value: str) -> dict[str, object]:
    return {"name": name, "value": value, "created_at": _STAMP, "updated_at": _STAMP}


def _repo(name: str) -> dict[str, object]:
    return {
        "id": 1,
        "name": name,
        "full_name": f"{ORG}/{name}",
        "private": False,
        "archived": False,
    }


def _published(org: str = ORG) -> list[dict[str, object]]:
    values = expected_values(org, AUTHORS)
    # An unrelated org variable is listed too, and ignored.
    unrelated = _org_variable("EXAMPLE_UNRELATED", "x", "private")
    return [unrelated, *(_org_variable(name, value) for name, value in values.items())]


class FakeGitHub:
    """Answers the module's REST reads from a table and records its writes."""

    def __init__(
        self,
        org_variables: list[dict[str, object]],
        repos: dict[str, list[dict[str, object]]] | None = None,
        *,
        fail: str | None = None,
    ) -> None:
        self.org_variables = org_variables
        self.repos = repos if repos is not None else {"repo-a": [], "repo-b": []}
        self.fail = fail
        self.writes: list[tuple[str, str, dict[str, object]]] = []

    def read_json_paginated(self, endpoint: str, key: str | None = None) -> list[object]:
        if self.fail is not None and endpoint.startswith(self.fail):
            cmd = ("gh", "api", "--paginate", endpoint)
            raise subprocess.CalledProcessError(1, cmd, "", "HTTP 403: Resource not accessible")
        if endpoint == f"orgs/{ORG}/actions/variables":
            assert key == "variables"
            return list(self.org_variables)
        if endpoint == f"orgs/{ORG}/repos?type=all":
            assert key is None
            return [_repo(name) for name in self.repos]
        for name, variables in self.repos.items():
            if endpoint == f"repos/{ORG}/{name}/actions/variables":
                assert key == "variables"
                return list(variables)
        msg = f"unexpected endpoint {endpoint}"
        raise AssertionError(msg)

    def write_json(self, method: str, endpoint: str, body: dict[str, object]) -> None:
        self.writes.append((method, endpoint, body))


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    gh = FakeGitHub(_published())
    monkeypatch.setattr(trust_publish.github, "read_json_paginated", gh.read_json_paginated)
    monkeypatch.setattr(trust_publish.github, "write_json", gh.write_json)
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


class TestPublish:
    def test_publish_sets_both_variables(self, fake: FakeGitHub) -> None:
        fake.org_variables = [_org_variable("VRG_AUTOMATION_APP", "example-org-automation")]
        publish(ORG, AUTHORS)
        assert fake.writes == [
            (
                "POST",
                f"orgs/{ORG}/actions/variables",
                {
                    "name": "VRG_TRUSTED_AUTHORS",
                    "value": render_actions_value(AUTHORS),
                    "visibility": "all",
                },
            ),
            (
                "PATCH",
                f"orgs/{ORG}/actions/variables/VRG_AUTOMATION_APP",
                {
                    "name": "VRG_AUTOMATION_APP",
                    "value": "example-org-automation",
                    "visibility": "all",
                },
            ),
        ]
        # The published value carries every listed org, not only this one.
        published = parse_actions_value(str(fake.writes[0][2]["value"]), where="test")
        assert published == AUTHORS

    def test_publish_refuses_when_repo_shadow_exists(self, fake: FakeGitHub) -> None:
        fake.repos = {"repo-a": [_repo_variable("vrg_trusted_authors", "{}")], "repo-b": []}
        with pytest.raises(PublishRefusedError, match=rf"{ORG}/repo-a: repository variable"):
            publish(ORG, AUTHORS)
        assert fake.writes == []

    def test_publish_refuses_unlisted_org(self, fake: FakeGitHub) -> None:
        with pytest.raises(ValueError, match="not a Vergil org"):
            publish("example-unlisted-org", AUTHORS)
        assert fake.writes == []

    def test_publish_write_error_raises(
        self, fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(method: str, endpoint: str, body: dict[str, object]) -> None:
            raise subprocess.CalledProcessError(1, ("gh", "api", endpoint), "", "HTTP 403")

        monkeypatch.setattr(trust_publish.github, "write_json", _fail)
        with pytest.raises(TrustPublishError, match=rf"PATCH orgs/{ORG}/actions/variables/"):
            publish(ORG, AUTHORS)

    def test_cli_publish_runs_only_as_human(
        self,
        fake: FakeGitHub,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _write_identities(tmp_path)
        for mode in (IdentityMode.USER, IdentityMode.AUDIT, IdentityMode.AUTOMATION):
            _set_mode(monkeypatch, mode)
            assert vrg_trusted_authors.main(["publish", "--org", ORG]) == 1
            assert "runs only as the human" in capsys.readouterr().err
        assert fake.writes == []
        _set_mode(monkeypatch, IdentityMode.HUMAN)
        assert vrg_trusted_authors.main(["publish", "--org", ORG]) == 0
        assert [name for _, _, body in fake.writes for name in [body["name"]]] == list(
            ORG_VARIABLES
        )


class TestCheck:
    def test_check_clean(self, fake: FakeGitHub) -> None:
        result = check(ORG)
        assert result.findings == []
        assert result.shadows == []
        assert result.values == expected_values(ORG, AUTHORS)

    @pytest.mark.parametrize(
        ("variables", "expected"),
        [
            (
                [_org_variable("VRG_AUTOMATION_APP", "example-org-automation")],
                "example-org: org variable VRG_TRUSTED_AUTHORS is missing",
            ),
            (
                [_org_variable("VRG_TRUSTED_AUTHORS", render_actions_value(AUTHORS))],
                "example-org: org variable VRG_AUTOMATION_APP is missing",
            ),
            (
                [
                    _org_variable("VRG_TRUSTED_AUTHORS", "not json"),
                    _org_variable("VRG_AUTOMATION_APP", "example-org-automation"),
                ],
                "example-org: org variable VRG_TRUSTED_AUTHORS is invalid: invalid JSON",
            ),
            (
                [
                    _org_variable("VRG_TRUSTED_AUTHORS", json.dumps({"human": ["example-human"]})),
                    _org_variable("VRG_AUTOMATION_APP", "example-org-automation"),
                ],
                "example-org: org variable VRG_TRUSTED_AUTHORS is invalid: trusted_authors:"
                " 'agents' is missing",
            ),
            (
                [
                    _org_variable("VRG_TRUSTED_AUTHORS", render_actions_value(AUTHORS)),
                    _org_variable("VRG_AUTOMATION_APP", "example-org-automation[bot]"),
                ],
                "example-org: org variable VRG_AUTOMATION_APP is invalid: it has the '[bot]'"
                " suffix",
            ),
            (
                [
                    _org_variable("VRG_TRUSTED_AUTHORS", render_actions_value(AUTHORS)),
                    _org_variable("VRG_AUTOMATION_APP", " "),
                ],
                "example-org: org variable VRG_AUTOMATION_APP is invalid: it is empty",
            ),
            (
                [
                    _org_variable("VRG_TRUSTED_AUTHORS", render_actions_value(AUTHORS)),
                    _org_variable("VRG_AUTOMATION_APP", "example-org-automation "),
                ],
                "example-org: org variable VRG_AUTOMATION_APP is invalid: it has surrounding"
                " whitespace",
            ),
            (
                [
                    _org_variable("VRG_TRUSTED_AUTHORS", render_actions_value(AUTHORS)),
                    _org_variable("VRG_AUTOMATION_APP", "app/example-org-automation"),
                ],
                "example-org: org variable VRG_AUTOMATION_APP is invalid: it has the 'app/' prefix",
            ),
            (
                [
                    _org_variable("VRG_TRUSTED_AUTHORS", render_actions_value(AUTHORS)),
                    _org_variable("VRG_AUTOMATION_APP", "example-other-automation"),
                ],
                "example-org: org variable VRG_AUTOMATION_APP is 'example-other-automation',"
                " but VRG_TRUSTED_AUTHORS names 'example-org-automation' for example-org",
            ),
            (
                [
                    _org_variable(
                        "VRG_TRUSTED_AUTHORS",
                        render_actions_value(
                            TrustedAuthors(
                                human=AUTHORS.human,
                                agents=AUTHORS.agents,
                                orgs={OTHER_ORG: AUTHORS.orgs[OTHER_ORG]},
                            )
                        ),
                    ),
                    _org_variable("VRG_AUTOMATION_APP", "example-org-automation"),
                ],
                "example-org: org variable VRG_TRUSTED_AUTHORS has no stanza for example-org",
            ),
            (
                [
                    _org_variable("VRG_TRUSTED_AUTHORS", render_actions_value(AUTHORS), "private"),
                    _org_variable("VRG_AUTOMATION_APP", "example-org-automation"),
                ],
                "example-org: org variable VRG_TRUSTED_AUTHORS has visibility 'private', not 'all'",
            ),
        ],
    )
    def test_check_flags_missing_or_invalid(
        self, fake: FakeGitHub, variables: list[dict[str, object]], expected: str
    ) -> None:
        fake.org_variables = variables
        result = check(ORG)
        assert len(result.findings) == 1
        assert result.findings[0].startswith(expected)
        assert result.shadows == []

    def test_check_flags_repo_level_shadow(self, fake: FakeGitHub) -> None:
        fake.repos = {
            "repo-a": [_repo_variable("UNRELATED", "x")],
            "repo-b": [
                _repo_variable("VRG_AUTOMATION_APP", "example-shadow"),
                _repo_variable("VRG_TRUSTED_AUTHORS", "{}"),
            ],
        }
        result = check(ORG)
        assert result.shadows == [
            f"{ORG}/repo-b: repository variable VRG_AUTOMATION_APP shadows the org variable"
            " in this repo's jobs",
            f"{ORG}/repo-b: repository variable VRG_TRUSTED_AUTHORS shadows the org variable"
            " in this repo's jobs",
        ]
        assert result.findings == result.shadows

    @pytest.mark.parametrize(
        "failing",
        [f"orgs/{ORG}/actions/variables", f"orgs/{ORG}/repos", f"repos/{ORG}/repo-b/"],
    )
    def test_check_api_error_raises(self, fake: FakeGitHub, failing: str) -> None:
        fake.fail = failing
        with pytest.raises(TrustPublishError, match=rf"(?s)GET {failing}.* failed: .*HTTP 403"):
            check(ORG)

    def test_check_unknown_shape_raises(self, fake: FakeGitHub) -> None:
        fake.org_variables = [{"name": "VRG_TRUSTED_AUTHORS"}]
        with pytest.raises(TrustPublishError, match="no string 'value'"):
            check(ORG)

    def test_check_item_not_an_object_raises(self, fake: FakeGitHub) -> None:
        fake.org_variables = cast("list[dict[str, object]]", ["VRG_TRUSTED_AUTHORS"])
        with pytest.raises(TrustPublishError, match="an item that is not an object"):
            check(ORG)

    def test_cli_check_clean_without_values_out(
        self, fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _set_mode(monkeypatch, IdentityMode.HUMAN)
        assert vrg_trusted_authors.main(["check", "--org", ORG]) == 0
        assert capsys.readouterr().out == "trusted-authors variables are clean in example-org\n"

    def test_check_writes_values_to_summary(
        self,
        fake: FakeGitHub,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _set_mode(monkeypatch, IdentityMode.AUTOMATION)
        fake.org_variables = [_org_variable("VRG_AUTOMATION_APP", "example-org-automation")]
        out = tmp_path / "values.json"
        assert vrg_trusted_authors.main(["check", "--org", ORG, "--values-out", str(out)]) == 1
        assert json.loads(out.read_text()) == {
            ORG: {"VRG_AUTOMATION_APP": "example-org-automation"}
        }
        assert "FINDING: example-org: org variable VRG_TRUSTED_AUTHORS is missing" in (
            capsys.readouterr().out
        )

    @pytest.mark.parametrize("mode", [IdentityMode.USER, IdentityMode.AUDIT])
    def test_check_refuses_agent_mode(
        self,
        fake: FakeGitHub,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        mode: IdentityMode,
    ) -> None:
        _set_mode(monkeypatch, mode)
        reads: list[str] = []
        monkeypatch.setattr(
            trust_publish.github,
            "read_json_paginated",
            lambda endpoint, key=None: reads.append(endpoint) or [],
        )
        assert vrg_trusted_authors.main(["check", "--org", ORG]) == 2
        assert "an agent mode" in capsys.readouterr().err
        assert reads == []

    def test_check_refuses_unresolvable_identity(
        self, fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def _unresolvable() -> IdentityMode:
            msg = "the token belongs to someone else"
            raise vrg_trusted_authors.AutomationIdentityError(msg)

        monkeypatch.setattr(vrg_trusted_authors.identity_mode, "current_mode", _unresolvable)
        assert vrg_trusted_authors.main(["check", "--org", ORG]) == 2
        assert "could not be resolved" in capsys.readouterr().err

    def test_check_runs_as_automation(
        self,
        fake: FakeGitHub,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Under Actions with no configuration at all: --org needs none.
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        _set_mode(monkeypatch, IdentityMode.AUTOMATION)
        out = tmp_path / "values.json"
        assert vrg_trusted_authors.main(["check", "--org", ORG, "--values-out", str(out)]) == 0
        assert json.loads(out.read_text()) == {ORG: expected_values(ORG, AUTHORS)}
        assert "clean in example-org" in capsys.readouterr().out

    def test_check_api_error_exits_2(
        self, fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _set_mode(monkeypatch, IdentityMode.AUTOMATION)
        fake.fail = f"orgs/{ORG}/repos"
        assert vrg_trusted_authors.main(["check", "--org", ORG]) == 2
        assert "HTTP 403" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (
                github.NoInstallationError(ORG, ["other-org"]),
                f"no installation for owner '{ORG}'. Known installations: other-org.",
            ),
            (
                github.AppAuthError(ORG, "123", Path("/key.pem"), "the key was rejected"),
                "the key was rejected",
            ),
            (
                github.MissingGitHubTokenError(4, ["gh", "api"], "", "no token"),
                f"GET orgs/{ORG}/actions/variables failed",
            ),
            (gh_version.GhVersionError("gh 2.0.0 is older than 2.97.0"), "older than 2.97.0"),
        ],
    )
    def test_check_auth_or_setup_error_exits_2(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        error: Exception,
        expected: str,
    ) -> None:
        # issue #5079: a check that read nothing "could not run" (2), never
        # "found a problem" (1), the code cli_main would give these.
        _set_mode(monkeypatch, IdentityMode.AUTOMATION)

        def _fail(*_args: object, **_kwargs: object) -> list[object]:
            raise error

        monkeypatch.setattr(trust_publish.github, "read_json_paginated", _fail)
        assert vrg_trusted_authors.main(["check", "--org", ORG]) == 2
        captured = capsys.readouterr()
        assert expected in captured.err
        assert "FINDING" not in captured.out

    def test_check_missing_token_names_the_fix(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A missing token that escapes the reads unwrapped is "could not run"
        # too, with the configuration fix in its message.
        _set_mode(monkeypatch, IdentityMode.AUTOMATION)

        def _fail(_org: str) -> object:
            raise github.MissingGitHubTokenError(4, ["gh", "api"], "", "no token")

        monkeypatch.setattr(vrg_trusted_authors.trust_publish, "check", _fail)
        assert vrg_trusted_authors.main(["check", "--org", ORG]) == 2
        assert "vrg-trusted-authors: no GitHub token available" in capsys.readouterr().err


class TestRender:
    def test_render_matches_publish(
        self,
        fake: FakeGitHub,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _write_identities(tmp_path)
        _set_mode(monkeypatch, IdentityMode.HUMAN)
        fake.org_variables = []
        assert vrg_trusted_authors.main(["publish", "--org", ORG]) == 0
        capsys.readouterr()
        assert vrg_trusted_authors.main(["render", "--org", ORG]) == 0
        rendered = json.loads(capsys.readouterr().out)
        published = {str(body["name"]): str(body["value"]) for _, _, body in fake.writes}
        assert rendered == {ORG: published}

    def test_render_every_org_without_github(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def _no_github(*args: object, **kwargs: object) -> list[object]:
            msg = "render must not call GitHub"
            raise AssertionError(msg)

        monkeypatch.setattr(trust_publish.github, "read_json_paginated", _no_github)
        _write_identities(tmp_path)
        assert vrg_trusted_authors.main(["render"]) == 0
        rendered = json.loads(capsys.readouterr().out)
        assert rendered == {
            ORG: expected_values(ORG, AUTHORS),
            OTHER_ORG: expected_values(OTHER_ORG, AUTHORS),
        }
        assert rendered[OTHER_ORG]["VRG_AUTOMATION_APP"] == "example-other-automation"

    def test_render_unlisted_org_exits_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_identities(tmp_path)
        assert vrg_trusted_authors.main(["render", "--org", "example-unlisted-org"]) == 1
        assert "not a Vergil org" in capsys.readouterr().err
