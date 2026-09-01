"""Tests for the ``ci.yml`` matrix-strip applicator and the ``vrg-ci-sync`` entry.

The applicator (:func:`vergil_tooling.lib.ci_yml_applicator.strip_matrix_inputs`)
edits a real ``.github/workflows/ci.yml`` under a throwaway ``tmp_path`` worktree,
so its filesystem behaviour — strip, idempotent no-op, and the no-silent-failure
``needs_followup`` deferrals — is exercised for real, with no git or GitHub state
touched.

The ``vrg_ci_sync`` entry tests mock :func:`run_sweep` (and, where relevant,
capture the applicator it is handed) so the CLI's spec construction, absolute
``--repos`` resolution (#2979), dry-run passthrough, per-repo reporting, and the
loud ``needs_followup`` follow-up report are asserted without a real sweep.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from vergil_tooling.bin import vrg_ci_sync
from vergil_tooling.lib.ci_yml_applicator import strip_matrix_inputs
from vergil_tooling.lib.fleet_sweep import RepoResult, SweepSpec

# A representative "fat" ci.yml with the hardcoded matrix inputs on multiple
# reusable-workflow calls — the shape the sweep converges onto the thin caller.
_FAT_CI = textwrap.dedent(
    """\
    name: CI

    on:
      pull_request:

    jobs:
      audit:
        uses: vergil-project/vergil-actions/.github/workflows/ci-audit.yml@v2.1
        with:
          language: python
          versions: '["3.12", "3.13", "3.14"]'
          container-tag: '3.14'
          container-suffix: python

      quality:
        uses: vergil-project/vergil-actions/.github/workflows/ci-quality.yml@v2.1
        with:
          language: python
          versions: '["3.12", "3.13", "3.14"]'
          container-tag: '3.14'
          container-suffix: python

      security:
        uses: vergil-project/vergil-actions/.github/workflows/ci-security.yml@v2.1
        with:
          language: python
          container-tag: '3.14'
          container-suffix: python
    """
)

# The same file already reduced to the thin-caller shape (no matrix inputs).
_THIN_CI = textwrap.dedent(
    """\
    name: CI

    on:
      pull_request:

    jobs:
      audit:
        uses: vergil-project/vergil-actions/.github/workflows/ci-audit.yml@v2.1
        with:
          language: python
          container-suffix: python

      quality:
        uses: vergil-project/vergil-actions/.github/workflows/ci-quality.yml@v2.1
        with:
          language: python
          container-suffix: python

      security:
        uses: vergil-project/vergil-actions/.github/workflows/ci-security.yml@v2.1
        with:
          language: python
          container-suffix: python
    """
)


def _write_ci(worktree: Path, content: str) -> Path:
    """Write *content* to ``<worktree>/.github/workflows/ci.yml`` and return it."""
    ci_path = worktree / ".github" / "workflows" / "ci.yml"
    ci_path.parent.mkdir(parents=True, exist_ok=True)
    ci_path.write_text(content, encoding="utf-8")
    return ci_path


# --- applicator ----------------------------------------------------------


def test_strips_both_inputs_from_a_multi_call_ci(tmp_path: Path) -> None:
    """Every reusable-workflow call loses versions:/container-tag:, keeping the rest."""
    ci_path = _write_ci(tmp_path, _FAT_CI)

    result = strip_matrix_inputs(tmp_path)

    assert result.changed is True
    assert result.needs_followup is False
    new_text = ci_path.read_text(encoding="utf-8")
    # Both matrix inputs are gone from all three calls.
    assert "versions:" not in new_text
    assert "container-tag:" not in new_text
    # The thin-caller inputs are preserved on every call (three of each).
    assert new_text.count("language: python") == 3
    assert new_text.count("container-suffix: python") == 3
    # The file converged exactly onto the rendered thin-caller shape.
    assert new_text == _THIN_CI


def test_idempotent_when_already_stripped(tmp_path: Path) -> None:
    """A ci.yml already thin is a no-op: changed=False, no error, file untouched."""
    ci_path = _write_ci(tmp_path, _THIN_CI)

    result = strip_matrix_inputs(tmp_path)

    assert result.changed is False
    assert result.needs_followup is False
    assert ci_path.read_text(encoding="utf-8") == _THIN_CI


def test_unparseable_ci_flags_needs_followup_and_makes_no_edit(tmp_path: Path) -> None:
    """A ci.yml that does not parse as YAML is left untouched and flagged."""
    broken = "jobs:\n  audit:\n    with:\n  - this: ][ is not: valid: yaml\n"
    ci_path = _write_ci(tmp_path, broken)

    result = strip_matrix_inputs(tmp_path)

    assert result.changed is False
    assert result.needs_followup is True
    # No edit was made.
    assert ci_path.read_text(encoding="utf-8") == broken


def test_missing_ci_flags_needs_followup(tmp_path: Path) -> None:
    """A repo with no ci.yml is flagged for follow-up, not a crash or silent skip."""
    result = strip_matrix_inputs(tmp_path)

    assert result.changed is False
    assert result.needs_followup is True
    assert "not found" in result.summary


def test_unrecognized_shape_without_jobs_flags_needs_followup(tmp_path: Path) -> None:
    """Valid YAML that lacks a jobs: mapping is a shape we refuse to edit."""
    ci_path = _write_ci(tmp_path, "name: CI\non:\n  pull_request:\n")

    result = strip_matrix_inputs(tmp_path)

    assert result.changed is False
    assert result.needs_followup is True
    assert ci_path.read_text(encoding="utf-8") == "name: CI\non:\n  pull_request:\n"


# --- vrg_ci_sync entry ---------------------------------------------------


def test_main_builds_ci_sync_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    """main builds the ci-sync SweepSpec (epic #338) and hands it to run_sweep."""
    captured: dict[str, object] = {}

    def fake_run_sweep(spec, applicator, *, dry_run):  # noqa: ANN001, ANN003
        captured["spec"] = spec
        captured["applicator"] = applicator
        captured["dry_run"] = dry_run
        return []

    monkeypatch.setattr(vrg_ci_sync, "run_sweep", fake_run_sweep)

    rc = vrg_ci_sync.main(["--repos", "/clones/x", "/clones/y", "--dry-run"])
    assert rc == 0

    spec = captured["spec"]
    assert isinstance(spec, SweepSpec)
    assert spec.repos == ["/clones/x", "/clones/y"]
    assert spec.branch_slug == "ci-sync"
    assert spec.commit_type == "refactor"
    assert spec.commit_scope == "ci"
    assert spec.epic == "vergil-project/.github#338"
    assert spec.title
    assert spec.body
    assert captured["dry_run"] is True
    # The applicator handed to the driver is the matrix-strip applicator.
    assert captured["applicator"] is strip_matrix_inputs


def test_main_resolves_repos_to_absolute_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Relative --repos are resolved to absolute paths, dodging the #2979 bug."""
    captured: dict[str, object] = {}

    def fake_run_sweep(spec, applicator, *, dry_run):  # noqa: ANN001, ANN003
        captured["spec"] = spec
        return []

    monkeypatch.setattr(vrg_ci_sync, "run_sweep", fake_run_sweep)

    vrg_ci_sync.main(["--repos", "some/relative/repo", "another-repo"])
    spec = captured["spec"]
    assert isinstance(spec, SweepSpec)
    assert all(Path(p).is_absolute() for p in spec.repos)
    assert spec.repos[0] == str(Path("some/relative/repo").resolve())
    assert spec.repos[1] == str(Path("another-repo").resolve())


def test_main_prints_per_repo_status_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """main prints a per-repo status line and exits 0 when nothing errored."""

    def fake_run_sweep(spec, applicator, *, dry_run):  # noqa: ANN001, ANN003
        return [
            RepoResult(repo="/clones/x", status="ready", detail="stripped 2 lines"),
            RepoResult(repo="/clones/y", status="skipped", detail="already thin"),
        ]

    monkeypatch.setattr(vrg_ci_sync, "run_sweep", fake_run_sweep)
    rc = vrg_ci_sync.main(["--repos", "/clones/x", "/clones/y"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "/clones/x" in out
    assert "ready" in out
    assert "skipped" in out


def test_main_reports_needs_followup_repos_prominently(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A needs_followup repo is listed loudly after the per-repo lines (no silent skip)."""

    def fake_run_sweep(spec, applicator, *, dry_run):  # noqa: ANN001, ANN003
        return [
            RepoResult(repo="/clones/x", status="ready", detail="stripped 2 lines"),
            RepoResult(
                repo="/clones/y",
                status="skipped",
                detail="ci.yml does not parse as YAML",
                needs_followup=True,
            ),
        ]

    monkeypatch.setattr(vrg_ci_sync, "run_sweep", fake_run_sweep)
    rc = vrg_ci_sync.main(["--repos", "/clones/x", "/clones/y"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "FOLLOW-UP" in out
    # The follow-up repo is named in the loud report.
    followup_section = out.split("FOLLOW-UP", 1)[1]
    assert "/clones/y" in followup_section
    assert "/clones/x" not in followup_section


def test_main_returns_nonzero_when_a_repo_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """main exits non-zero if any repo ended in error."""

    def fake_run_sweep(spec, applicator, *, dry_run):  # noqa: ANN001, ANN003
        return [RepoResult(repo="/clones/x", status="error", detail="boom")]

    monkeypatch.setattr(vrg_ci_sync, "run_sweep", fake_run_sweep)
    assert vrg_ci_sync.main(["--repos", "/clones/x"]) == 1


def test_main_requires_repos() -> None:
    """main errors (argparse) when no repos are given."""
    with pytest.raises(SystemExit):
        vrg_ci_sync.main([])
