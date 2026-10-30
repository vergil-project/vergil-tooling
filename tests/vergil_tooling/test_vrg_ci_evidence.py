"""Tests for the ``vrg-ci-evidence`` CLI (``harvest`` / ``assemble`` / ``bundle``).

The CLI is a thin orchestrator over :mod:`vergil_tooling.lib.ci_evidence`, so the
tests mock only the GitHub-touching stages (PR resolution, head SHA, run
selection, required-gate derivation, check conclusions, artifact download) to
fixtures and let the pure bundle-core stages run for real. The happy paths assert
the tarball + standalone manifest land; the failure paths assert a missing gate
and an unqualified run both exit non-zero with no tarball. The split path
additionally proves ``assemble`` needs no network and is byte-identical to the
atomic ``bundle`` (issue #2330).
"""

from __future__ import annotations

import base64
import json
import tarfile
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.bin import vrg_ci_evidence
from vergil_tooling.bin.vrg_ci_evidence import main
from vergil_tooling.lib import ci_evidence, git, github
from vergil_tooling.lib.ci_evidence import NoQualifyingRunError
from vergil_tooling.lib.github import GitHubAPIError
from vergil_tooling.lib.github_config import EvidenceGate
from vergil_tooling.lib.versions import ResolutionError

if TYPE_CHECKING:
    from pathlib import Path

    from tests.vergil_tooling.conftest import FakeApp


# The real config-resolution stage, captured before any test stubs it.
_REAL_RESOLVE_REQUIRED_GATES = ci_evidence.resolve_required_gates


def _wire_github_stages(
    monkeypatch: pytest.MonkeyPatch,
    *,
    required: tuple[EvidenceGate, ...],
    conclusions: dict[str, str],
    download: Any,
) -> None:
    """Mock every GitHub-touching stage the CLI invokes to in-memory fixtures."""
    monkeypatch.setattr(ci_evidence, "resolve_release_pr", lambda _repo, _sha: 2281)
    monkeypatch.setattr(github, "head_sha", lambda _pr: "cafef00d")
    monkeypatch.setattr(
        ci_evidence,
        "select_ci_run",
        lambda _repo, _sha: {"id": 123, "html_url": "https://github.com/o/r/actions/runs/123"},
    )
    monkeypatch.setattr(ci_evidence, "resolve_required_gates", lambda _repo, _ref: required)
    monkeypatch.setattr(ci_evidence, "read_gate_conclusions", lambda _repo, _sha: conclusions)
    monkeypatch.setattr(ci_evidence, "download_evidence_artifacts", download)


def _full_download(gates: tuple[str, ...]) -> Any:
    """A download fixture that stages ``evidence.json`` + a report for each gate."""

    def _download(
        _repo: str, _run_id: int, dest: Path, _required: tuple[EvidenceGate, ...]
    ) -> list[Path]:
        gate_dirs: list[Path] = []
        for gate in gates:
            gate_dir = dest / gate
            gate_dir.mkdir(parents=True)
            (gate_dir / "evidence.json").write_text(
                json.dumps({"gate": gate, "tools": [], "metrics": {}}), encoding="utf-8"
            )
            (gate_dir / f"{gate}.report").write_text("report-data", encoding="utf-8")
            gate_dirs.append(gate_dir)
        return sorted(gate_dirs)

    return _download


def _argv(out_dir: Path, *, sbom: Path | None = None) -> list[str]:
    argv = [
        "bundle",
        "--repo",
        "o/r",
        "--version",
        "2.1.129",
        "--merge-sha",
        "deadbeef",
        "--generated-at",
        "2026-07-13T00:00:00Z",
        "--out-dir",
        str(out_dir),
    ]
    if sbom is not None:
        argv += ["--sbom-file", str(sbom)]
    return argv


def test_bundle_writes_tarball_and_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    required = (
        EvidenceGate(name="security", checks=("CodeQL",)),
        EvidenceGate(name="test", checks=("test / unit",)),
    )
    _wire_github_stages(
        monkeypatch,
        required=required,
        conclusions={"CodeQL": "success", "test / unit": "success"},
        download=_full_download(("security", "test")),
    )
    out_dir = tmp_path / "out"

    rc = main(_argv(out_dir))

    assert rc == 0
    tarball = out_dir / "v2.1.129-ci-evidence.tar.gz"
    manifest_path = out_dir / "v2.1.129-ci-evidence-manifest.json"
    assert tarball.exists()
    assert manifest_path.exists()

    manifest = json.loads(manifest_path.read_text())
    assert manifest["schema_version"] == "1.0"
    assert manifest["repo"] == "o/r"
    assert manifest["release"] == {
        "version": "2.1.129",
        "tag": "v2.1.129",
        "released_commit": "deadbeef",
    }
    assert manifest["provenance"]["release_pr"] == 2281
    assert manifest["provenance"]["validated_head_sha"] == "cafef00d"
    assert manifest["generated_at"] == "2026-07-13T00:00:00Z"
    assert {gate["name"] for gate in manifest["gates"]} == {"security", "test"}
    assert manifest["missing_gates"] == []

    with tarfile.open(tarball) as tar:
        names = tar.getnames()
    assert "evidence/manifest.json" in names
    assert "evidence/checks.json" in names
    assert "evidence/README.md" in names
    assert "evidence/gates/test/test.report" in names


def test_bundle_includes_sbom_when_given(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _wire_github_stages(
        monkeypatch,
        required=(EvidenceGate(name="test", checks=("test / unit",)),),
        conclusions={"test / unit": "success"},
        download=_full_download(("test",)),
    )
    out_dir = tmp_path / "out"
    sbom = tmp_path / "sbom.cdx.json"
    sbom.write_text("{}", encoding="utf-8")

    rc = main(_argv(out_dir, sbom=sbom))

    assert rc == 0
    with tarfile.open(out_dir / "v2.1.129-ci-evidence.tar.gz") as tar:
        names = tar.getnames()
    assert "evidence/gates/sbom/sbom.cdx.json" in names


def test_bundle_missing_required_gate_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    required = (
        EvidenceGate(name="security", checks=("CodeQL",)),
        EvidenceGate(name="test", checks=("test / unit",)),
    )
    _wire_github_stages(
        monkeypatch,
        required=required,
        conclusions={"CodeQL": "success", "test / unit": "success"},
        download=_full_download(("test",)),  # security missing
    )
    out_dir = tmp_path / "out"

    rc = main(_argv(out_dir))

    assert rc == 1
    err = capsys.readouterr().err
    assert "no evidence artifact" in err
    assert "security" in err
    assert not (out_dir / "v2.1.129-ci-evidence.tar.gz").exists()


def test_bundle_no_qualifying_run_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ci_evidence, "resolve_release_pr", lambda _repo, _sha: 2281)
    monkeypatch.setattr(github, "head_sha", lambda _pr: "cafef00d")

    def _raise(_repo: str, _sha: str) -> Any:
        raise NoQualifyingRunError("cafef00d")

    monkeypatch.setattr(ci_evidence, "select_ci_run", _raise)
    out_dir = tmp_path / "out"

    rc = main(_argv(out_dir))

    assert rc == 1
    err = capsys.readouterr().err
    assert "no completed+success CI run" in err
    assert not (out_dir / "v2.1.129-ci-evidence.tar.gz").exists()


@pytest.mark.parametrize("command", ["harvest", "bundle"])
def test_version_window_error_is_one_line_exit_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """A [ci].versions pin outside the window is one ERROR line, not a traceback (#3670)."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    _wire_github_stages(
        monkeypatch,
        required=(),
        conclusions={},
        download=_full_download(()),
    )
    message = "[ci].versions pins 3.11, which is not in the python window (3.14, 3.13)"

    def _raise(_repo: str, _ref: str) -> Any:
        raise ResolutionError(message)

    monkeypatch.setattr(ci_evidence, "resolve_required_gates", _raise)
    out_dir = tmp_path / "out"
    argv = _harvest_argv(out_dir) if command == "harvest" else _argv(out_dir)

    rc = main(argv)

    assert rc == 1
    err = capsys.readouterr().err
    assert err == f"ERROR: {message}\n"
    assert not (out_dir / "harvest-state.json").exists()
    assert not (out_dir / "v2.1.129-ci-evidence.tar.gz").exists()


def test_bundle_expired_evidence_artifact_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every copy of a gate's artifact expired → one-line error, exit 1 (#3171)."""

    def _download(*_a: Any) -> list[Path]:
        raise ci_evidence.EvidenceArtifactError("every evidence artifact for gate 'test' expired")

    _wire_github_stages(
        monkeypatch,
        required=(EvidenceGate(name="test", checks=("test / unit",)),),
        conclusions={"test / unit": "success"},
        download=_download,
    )
    out_dir = tmp_path / "out"

    rc = main(_argv(out_dir))

    assert rc == 1
    assert "expired" in capsys.readouterr().err
    assert not (out_dir / "v2.1.129-ci-evidence.tar.gz").exists()


def test_bundle_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        vrg_ci_evidence.main(["bundle", "--help"])
    assert excinfo.value.code == 0
    assert "--repo" in capsys.readouterr().out


# --- harvest / assemble split (issue #2330) -----------------------------


def _harvest_argv(staging: Path) -> list[str]:
    return ["harvest", "--repo", "o/r", "--merge-sha", "deadbeef", "--out-dir", str(staging)]


def _assemble_argv(staging: Path, out_dir: Path, *, sbom: Path | None = None) -> list[str]:
    argv = [
        "assemble",
        "--staging",
        str(staging),
        "--version",
        "2.1.129",
        "--generated-at",
        "2026-07-13T00:00:00Z",
        "--out-dir",
        str(out_dir),
    ]
    if sbom is not None:
        argv += ["--sbom-file", str(sbom)]
    return argv


def _break_github_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repoint every GitHub-touching stage at a raiser to prove no network use."""

    def _boom(*_a: Any, **_k: Any) -> Any:
        msg = "assemble must not touch the network"
        raise AssertionError(msg)

    for name in (
        "resolve_release_pr",
        "select_ci_run",
        "resolve_required_gates",
        "read_gate_conclusions",
        "download_evidence_artifacts",
    ):
        monkeypatch.setattr(ci_evidence, name, _boom)
    monkeypatch.setattr(github, "head_sha", _boom)


def test_harvest_persists_tree_and_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    required = (
        EvidenceGate(name="security", checks=("CodeQL",)),
        EvidenceGate(name="test", checks=("test / unit",)),
    )
    _wire_github_stages(
        monkeypatch,
        required=required,
        conclusions={"CodeQL": "success", "test / unit": "success"},
        download=_full_download(("security", "test")),
    )
    staging = tmp_path / "staging"

    rc = main(_harvest_argv(staging))

    assert rc == 0
    # The harvested tree is persisted, but no assemble outputs exist yet.
    assert (staging / "evidence" / "gates" / "test" / "test.report").exists()
    assert not (staging / "evidence" / "manifest.json").exists()
    assert not (staging / "evidence" / "checks.json").exists()

    state = json.loads((staging / "harvest-state.json").read_text())
    assert state["schema_version"] == "1.0"
    assert state["repo"] == "o/r"
    assert state["released_commit"] == "deadbeef"
    assert state["release_pr"] == 2281
    assert state["validated_head_sha"] == "cafef00d"
    assert state["ci_run_urls"] == ["https://github.com/o/r/actions/runs/123"]
    assert state["checks"] == {"CodeQL": "success", "test / unit": "success"}
    assert state["gate_conclusions"] == {"security": "success", "test": "success"}


def test_harvest_missing_required_gate_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    required = (
        EvidenceGate(name="security", checks=("CodeQL",)),
        EvidenceGate(name="test", checks=("test / unit",)),
    )
    _wire_github_stages(
        monkeypatch,
        required=required,
        conclusions={"CodeQL": "success", "test / unit": "success"},
        download=_full_download(("test",)),  # security missing
    )
    staging = tmp_path / "staging"

    rc = main(_harvest_argv(staging))

    assert rc == 1
    err = capsys.readouterr().err
    assert "no evidence artifact" in err
    assert "security" in err
    # A failed gate persists no state file (the raise precedes the write).
    assert not (staging / "harvest-state.json").exists()


def test_assemble_consumes_harvest_without_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    required = (
        EvidenceGate(name="security", checks=("CodeQL",)),
        EvidenceGate(name="test", checks=("test / unit",)),
    )
    _wire_github_stages(
        monkeypatch,
        required=required,
        conclusions={"CodeQL": "success", "test / unit": "success"},
        download=_full_download(("security", "test")),
    )
    staging = tmp_path / "staging"
    assert main(_harvest_argv(staging)) == 0

    # Break every GitHub stage: a passing assemble proves it is network-free.
    _break_github_stages(monkeypatch)
    out_dir = tmp_path / "out"

    rc = main(_assemble_argv(staging, out_dir))

    assert rc == 0
    tarball = out_dir / "v2.1.129-ci-evidence.tar.gz"
    manifest_path = out_dir / "v2.1.129-ci-evidence-manifest.json"
    assert tarball.exists()
    assert manifest_path.exists()

    manifest = json.loads(manifest_path.read_text())
    assert manifest["provenance"]["release_pr"] == 2281
    assert manifest["provenance"]["validated_head_sha"] == "cafef00d"
    assert manifest["generated_at"] == "2026-07-13T00:00:00Z"
    assert {gate["name"] for gate in manifest["gates"]} == {"security", "test"}

    with tarfile.open(tarball) as tar:
        names = tar.getnames()
    assert "evidence/checks.json" in names
    assert "evidence/manifest.json" in names
    assert "evidence/gates/test/test.report" in names
    # The state file is a sibling of evidence/, so it is never tarred in.
    assert "harvest-state.json" not in names


def test_assemble_includes_sbom_when_given(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _wire_github_stages(
        monkeypatch,
        required=(EvidenceGate(name="test", checks=("test / unit",)),),
        conclusions={"test / unit": "success"},
        download=_full_download(("test",)),
    )
    staging = tmp_path / "staging"
    assert main(_harvest_argv(staging)) == 0

    out_dir = tmp_path / "out"
    sbom = tmp_path / "sbom.cdx.json"
    sbom.write_text("{}", encoding="utf-8")

    rc = main(_assemble_argv(staging, out_dir, sbom=sbom))

    assert rc == 0
    with tarfile.open(out_dir / "v2.1.129-ci-evidence.tar.gz") as tar:
        names = tar.getnames()
    assert "evidence/gates/sbom/sbom.cdx.json" in names


def test_harvest_then_assemble_matches_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    required = (
        EvidenceGate(name="security", checks=("CodeQL",)),
        EvidenceGate(name="test", checks=("test / unit",)),
    )
    _wire_github_stages(
        monkeypatch,
        required=required,
        conclusions={"CodeQL": "success", "test / unit": "success"},
        download=_full_download(("security", "test")),
    )

    # Atomic bundle.
    bundle_out = tmp_path / "bundle-out"
    assert main(_argv(bundle_out)) == 0
    bundle_manifest = (bundle_out / "v2.1.129-ci-evidence-manifest.json").read_text()

    # Split harvest + assemble with the same injected generated-at.
    staging = tmp_path / "staging"
    split_out = tmp_path / "split-out"
    assert main(_harvest_argv(staging)) == 0
    assert main(_assemble_argv(staging, split_out)) == 0
    split_manifest = (split_out / "v2.1.129-ci-evidence-manifest.json").read_text()

    # The split path is byte-for-byte identical to the atomic path.
    assert split_manifest == bundle_manifest


def test_assemble_missing_state_errors_without_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Issue #3119: the pre-publish harvest failed, so no harvest-state.json exists.
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    staging = tmp_path / "staging"
    staging.mkdir()

    rc = main(_assemble_argv(staging, tmp_path / "out"))

    assert rc == 1
    err = capsys.readouterr().err
    assert err == (
        f"ERROR: harvest state not found in {staging}: "
        "the pre-publish evidence gate did not complete\n"
    )
    assert not (tmp_path / "out").exists()


def test_assemble_missing_state_emits_gha_annotation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    staging = tmp_path / "staging"

    rc = main(_assemble_argv(staging, tmp_path / "out"))

    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("::error")
    assert f"harvest state not found in {staging}" in err
    assert "Traceback" not in err


def test_assemble_corrupt_state_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "harvest-state.json").write_text("{not json", encoding="utf-8")

    rc = main(_assemble_argv(staging, tmp_path / "out"))

    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("ERROR: harvest state is not valid JSON at")
    assert err.count("\n") == 1


def test_assemble_wrong_shape_state_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "harvest-state.json").write_text("[]", encoding="utf-8")

    rc = main(_assemble_argv(staging, tmp_path / "out"))

    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("ERROR: harvest state malformed at")
    assert err.count("\n") == 1


def test_harvest_unresolvable_release_pr_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(github, "read_json_paginated", lambda *a, **k: [])
    monkeypatch.setattr(github, "read_output", lambda *a, **k: "")
    staging = tmp_path / "staging"

    rc = main(_harvest_argv(staging))

    assert rc == 1
    err = capsys.readouterr().err
    assert err == "ERROR: cannot resolve release PR for deadbeef in o/r\n"


def test_harvest_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        vrg_ci_evidence.main(["harvest", "--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "--merge-sha" in out
    assert "--run-grace-secs" in out


@pytest.mark.parametrize(
    ("extra", "expected"),
    [([], ci_evidence.RUN_VISIBILITY_GRACE_SECS), (["--run-grace-secs", "45"], 45.0)],
)
def test_harvest_passes_run_grace_to_library(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extra: list[str], expected: float
) -> None:
    """``--run-grace-secs`` reaches ``run_harvest`` (default 300 s; #3434)."""
    seen: dict[str, float] = {}

    def _harvest(_repo: str, _sha: str, _staging: Path, *, run_grace: float) -> None:
        seen["run_grace"] = run_grace

    monkeypatch.setattr(ci_evidence, "run_harvest", _harvest)

    assert main(_harvest_argv(tmp_path / "staging") + extra) == 0
    assert seen["run_grace"] == expected


def test_assemble_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        vrg_ci_evidence.main(["assemble", "--help"])
    assert excinfo.value.code == 0
    assert "--staging" in capsys.readouterr().out


# -- App-token scoping (#3220) ------------------------------------------------


def test_harvest_reads_release_pr_head_from_the_target_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The PR head lookup names --repo; a bare number would hit the cwd repo."""
    _wire_github_stages(
        monkeypatch,
        required=(EvidenceGate(name="test", checks=("test / unit",)),),
        conclusions={"test / unit": "success"},
        download=_full_download(("test",)),
    )
    seen: list[str] = []

    def _head_sha(pr: str) -> str:
        seen.append(pr)
        return "cafef00d"

    monkeypatch.setattr(github, "head_sha", _head_sha)

    assert main(_harvest_argv(tmp_path / "staging")) == 0
    assert seen == ["https://github.com/o/r/pull/2281"]


def _scoped_argv(command: str, repo: str, tmp_path: Path) -> list[str]:
    argv = [command, "--repo", repo, "--merge-sha", "deadbeef", "--out-dir", str(tmp_path)]
    if command == "bundle":
        argv += ["--version", "2.1.129", "--generated-at", "2026-07-13T00:00:00Z"]
    return argv


@pytest.mark.parametrize("command", ["harvest", "bundle"])
@pytest.mark.parametrize("cwd_org", [None, "cwd-org"], ids=["outside-repo", "other-org"])
def test_token_minted_for_repo_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_app: FakeApp,
    command: str,
    cwd_org: str | None,
) -> None:
    """Every harvest-time GitHub call runs under the --repo owner's installation."""
    fake_app.cwd_org = cwd_org
    owners: list[str] = []

    def _record(repo: str, *_args: object, **_kwargs: object) -> None:
        assert repo == "target-org/r"
        owners.append(fake_app.token_owner())

    monkeypatch.setattr(ci_evidence, f"run_{command}", _record)

    assert main(_scoped_argv(command, "target-org/r", tmp_path)) == 0
    assert owners == ["target-org"]


@pytest.mark.parametrize("command", ["harvest", "bundle"])
def test_missing_installation_for_repo_owner_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_app: FakeApp,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """An uninstalled --repo owner stops the command; no ambient-auth fallback."""
    fake_app.cwd_org = "cwd-org"

    def _first_gh_call(_repo: str, *_args: object, **_kwargs: object) -> None:
        fake_app.token_owner()

    monkeypatch.setattr(ci_evidence, f"run_{command}", _first_gh_call)

    with pytest.raises(SystemExit) as excinfo:
        main(_scoped_argv(command, "lonely-org/r", tmp_path))
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "vrg-ci-evidence:" in err
    assert "no installation for owner 'lonely-org'" in err


# -- Config source: the --repo target's vergil.toml, never the cwd's (#3226) --


def _vergil_toml(language: str) -> bytes:
    return (
        "[project]\n"
        'repository-type = "library"\n'
        'versioning-scheme = "semver"\n'
        'branching-model = "library-release"\n'
        'release-model = "tagged-release"\n'
        f'primary-language = "{language}"\n'
        "\n[dependencies]\n"
        'vergil = "v2.0"\n'
        "\n[ci]\n"
        'versions = ["3.14"]\n'
    ).encode()


def _wire_remote_config(monkeypatch: pytest.MonkeyPatch, response: Any) -> dict[str, Any]:
    """Wire every stage except config resolution, which runs for real.

    The contents API answers with *response* (raised when an exception);
    ``required_evidence_gates`` records the language of the config it was
    handed, so a test can tell which repo's ``vergil.toml`` was evaluated.
    """
    required = (EvidenceGate(name="test", checks=("test / unit",)),)
    _wire_github_stages(
        monkeypatch,
        required=required,
        conclusions={"test / unit": "success"},
        download=_full_download(("test",)),
    )
    monkeypatch.setattr(ci_evidence, "resolve_required_gates", _REAL_RESOLVE_REQUIRED_GATES)
    monkeypatch.setattr(github, "repo_visibility", lambda _repo: "PUBLIC")
    seen: dict[str, Any] = {"contents": []}

    def _read_json(*args: str) -> Any:
        seen["contents"].append(args)
        if isinstance(response, Exception):
            raise response
        return response

    def _required(project: Any, _ci: Any, **_kwargs: Any) -> tuple[EvidenceGate, ...]:
        seen["language"] = project.primary_language
        return required

    monkeypatch.setattr(github, "read_json", _read_json)
    monkeypatch.setattr(ci_evidence, "required_evidence_gates", _required)
    return seen


def _contents_response(language: str) -> dict[str, object]:
    return {"content": base64.b64encode(_vergil_toml(language)).decode()}


_CONTENTS_AT_HEAD = ("api", "repos/o/r/contents/vergil.toml?ref=cafef00d")


@pytest.mark.parametrize("command", ["harvest", "bundle"])
def test_config_read_from_target_repo_outside_any_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str
) -> None:
    """Outside a repo the target's config is fetched at the release PR head."""

    def _no_checkout() -> Path:
        msg = "harvest must not consult the cwd checkout"
        raise AssertionError(msg)

    monkeypatch.setattr(git, "repo_root", _no_checkout)
    monkeypatch.chdir(tmp_path)
    seen = _wire_remote_config(monkeypatch, _contents_response("python"))

    assert main(_scoped_argv(command, "o/r", tmp_path / "out")) == 0
    assert seen["contents"] == [_CONTENTS_AT_HEAD]
    assert seen["language"] == "python"


def test_config_ignores_mismatched_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A different repo's vergil.toml in the cwd is never evaluated."""
    other_checkout = tmp_path / "other-repo"
    other_checkout.mkdir()
    (other_checkout / "vergil.toml").write_bytes(_vergil_toml("go"))
    monkeypatch.chdir(other_checkout)
    seen = _wire_remote_config(monkeypatch, _contents_response("python"))

    assert main(_harvest_argv(tmp_path / "staging")) == 0
    assert seen["contents"] == [_CONTENTS_AT_HEAD]
    assert seen["language"] == "python"


def test_config_in_target_checkout_uses_target_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The normal CI path: run from the target's own checkout, same config."""
    checkout = tmp_path / "r"
    checkout.mkdir()
    (checkout / "vergil.toml").write_bytes(_vergil_toml("python"))
    monkeypatch.chdir(checkout)
    seen = _wire_remote_config(monkeypatch, _contents_response("python"))
    staging = tmp_path / "staging"

    assert main(_harvest_argv(staging)) == 0
    assert seen["contents"] == [_CONTENTS_AT_HEAD]
    assert seen["language"] == "python"
    assert (staging / "evidence" / "gates" / "test" / "evidence.json").exists()


def test_missing_remote_config_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No vergil.toml at the head → one-line actionable error; no cwd fallback."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    (tmp_path / "vergil.toml").write_bytes(_vergil_toml("python"))
    monkeypatch.chdir(tmp_path)
    not_found = GitHubAPIError(
        1, ("gh", "api", _CONTENTS_AT_HEAD[1]), '{"status":"404"}', "gh: Not Found (HTTP 404)"
    )
    seen = _wire_remote_config(monkeypatch, not_found)
    staging = tmp_path / "staging"

    rc = main(_harvest_argv(staging))

    assert rc == 1
    assert "language" not in seen
    err = capsys.readouterr().err
    assert err.startswith("ERROR: vergil.toml not found in o/r at cafef00d.")
    assert err.count("\n") == 1
    assert not (staging / "harvest-state.json").exists()


def test_unparseable_remote_config_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.chdir(tmp_path)
    _wire_remote_config(monkeypatch, {"content": base64.b64encode(b"[project").decode()})

    rc = main(_harvest_argv(tmp_path / "staging"))

    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("ERROR: vergil.toml of o/r@cafef00d could not be parsed:")
