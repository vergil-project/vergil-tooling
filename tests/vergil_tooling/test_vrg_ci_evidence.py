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

import json
import tarfile
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.bin import vrg_ci_evidence
from vergil_tooling.bin.vrg_ci_evidence import main
from vergil_tooling.lib import ci_evidence, git, github
from vergil_tooling.lib.ci_evidence import NoQualifyingRunError
from vergil_tooling.lib.github_config import EvidenceGate

if TYPE_CHECKING:
    from pathlib import Path


def _wire_github_stages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    required: tuple[EvidenceGate, ...],
    conclusions: dict[str, str],
    download: Any,
) -> None:
    """Mock every GitHub-touching stage the CLI invokes to in-memory fixtures."""
    monkeypatch.setattr(git, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(ci_evidence, "resolve_release_pr", lambda _repo, _sha: 2281)
    monkeypatch.setattr(github, "head_sha", lambda _pr: "cafef00d")
    monkeypatch.setattr(
        ci_evidence,
        "select_ci_run",
        lambda _repo, _sha: {"id": 123, "html_url": "https://github.com/o/r/actions/runs/123"},
    )
    monkeypatch.setattr(ci_evidence, "resolve_required_gates", lambda _repo, _root: required)
    monkeypatch.setattr(ci_evidence, "read_gate_conclusions", lambda _repo, _sha: conclusions)
    monkeypatch.setattr(ci_evidence, "download_evidence_artifacts", download)


def _full_download(gates: tuple[str, ...]) -> Any:
    """A download fixture that stages ``evidence.json`` + a report for each gate."""

    def _download(_repo: str, _run_id: int, dest: Path) -> list[Path]:
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
        tmp_path,
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
        tmp_path,
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
        tmp_path,
        required=required,
        conclusions={"CodeQL": "success", "test / unit": "success"},
        download=_full_download(("test",)),  # security missing
    )
    out_dir = tmp_path / "out"

    rc = main(_argv(out_dir))

    assert rc == 1
    err = capsys.readouterr().err
    assert "missing evidence" in err
    assert "security" in err
    assert not (out_dir / "v2.1.129-ci-evidence.tar.gz").exists()


def test_bundle_no_qualifying_run_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(git, "repo_root", lambda: tmp_path)
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
        tmp_path,
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
        tmp_path,
        required=required,
        conclusions={"CodeQL": "success", "test / unit": "success"},
        download=_full_download(("test",)),  # security missing
    )
    staging = tmp_path / "staging"

    rc = main(_harvest_argv(staging))

    assert rc == 1
    err = capsys.readouterr().err
    assert "missing evidence" in err
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
        tmp_path,
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
        tmp_path,
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
        tmp_path,
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


def test_harvest_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        vrg_ci_evidence.main(["harvest", "--help"])
    assert excinfo.value.code == 0
    assert "--merge-sha" in capsys.readouterr().out


def test_assemble_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        vrg_ci_evidence.main(["assemble", "--help"])
    assert excinfo.value.code == 0
    assert "--staging" in capsys.readouterr().out
