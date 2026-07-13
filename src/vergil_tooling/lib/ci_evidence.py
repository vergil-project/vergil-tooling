"""CI evidence bundle core — manifest builder and bundle assembler.

Pure, network-free shaping of a release's CI-evidence bundle: hash files,
build the ``manifest.json`` index (schema 1.0, spec §8), write the raw
``checks.json`` snapshot and the human-orientation ``README.md``, copy the
already-built SBOM into the tree, and tar the ``evidence/`` tree into the
release asset.

Determinism: every function is a pure function of its inputs. Timestamps
(``generated_at``) are injected by the caller, never read from the clock here,
so the logic stays unit-testable and byte-reproducible.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from vergil_tooling.lib import github
from vergil_tooling.lib.config import read_config
from vergil_tooling.lib.github_config import ghas_available, required_evidence_gates
from vergil_tooling.lib.linkage import extract_merge_pr

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from vergil_tooling.lib.github_config import EvidenceGate

# Schema version of the manifest object (spec §8).
SCHEMA_VERSION = "1.0"

# Fixed human-orientation text written into ``evidence/README.md``. A module
# constant (not inline) so the archive's README is defined in exactly one place.
_README_TEXT = """\
# CI Evidence Archive

This archive is the durable, machine-verifiable evidence bundle for a release.
It captures the complete output of every required CI gate at release time.

## Layout

- `manifest.json` — top-level machine index (schema 1.0): release identity,
  provenance (release PR, validated head SHA, CI run URLs), per-gate tools,
  metrics, and a `sha256` for every file.
- `checks.json` — the raw check-run snapshot (name → conclusion).
- `gates/<gate>/` — each required gate's report files plus its `evidence.json`
  fragment. The `gates/sbom/` entry holds the release SBOM.

## Verifying integrity

Each file listed in `manifest.json` carries a `sha256`. Recompute it over the
extracted file and compare to prove the archive is intact. The bundle is also
covered by a build-provenance attestation on the GitHub Release.
"""


@dataclass(frozen=True)
class GateEvidence:
    """Harvested evidence for one CI gate, staged under ``gates/<name>/``."""

    name: str
    conclusion: str
    tools: tuple[dict[str, Any], ...]
    metrics: dict[str, Any]
    files: tuple[Path, ...]


@dataclass(frozen=True)
class HarvestContext:
    """Release identity and provenance for the manifest."""

    repo: str
    version: str
    tag: str
    released_commit: str
    release_pr: int
    validated_head_sha: str
    ci_run_urls: tuple[str, ...]


def _evidence_root(staging_dir: Path) -> Path:
    """The ``evidence/`` subtree root under a staging directory.

    The single join for "path under ``evidence/``" so every writer and the
    manifest's relative-path computation agree on the tree root.
    """
    return staging_dir / "evidence"


def sha256_file(path: Path) -> str:
    """Return the hex SHA-256 digest of a file's contents."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _gate_manifest_entry(gate: GateEvidence, evidence_root: Path) -> dict[str, Any]:
    """Render one gate's manifest entry, hashing each staged file once."""
    return {
        "name": gate.name,
        "conclusion": gate.conclusion,
        "tools": [dict(tool) for tool in gate.tools],
        "metrics": dict(gate.metrics),
        "files": [
            {
                "path": file.relative_to(evidence_root).as_posix(),
                "sha256": sha256_file(file),
            }
            for file in gate.files
        ],
    }


def build_manifest(
    ctx: HarvestContext,
    gates: Sequence[GateEvidence],
    *,
    generated_at: str,
    missing_gates: Sequence[str],
    staging_dir: Path,
) -> dict[str, Any]:
    """Build the schema-1.0 manifest object (spec §8).

    Every gate file is hashed via :func:`sha256_file`; ``missing_gates`` is
    recorded explicitly so a gate that produced no evidence is data, never a
    silent drop. ``generated_at`` is injected by the caller.
    """
    evidence_root = _evidence_root(staging_dir)
    return {
        "schema_version": SCHEMA_VERSION,
        "repo": ctx.repo,
        "release": {
            "version": ctx.version,
            "tag": ctx.tag,
            "released_commit": ctx.released_commit,
        },
        "provenance": {
            "release_pr": ctx.release_pr,
            "validated_head_sha": ctx.validated_head_sha,
            "ci_run_urls": list(ctx.ci_run_urls),
        },
        "generated_at": generated_at,
        "gates": [_gate_manifest_entry(gate, evidence_root) for gate in gates],
        "missing_gates": list(missing_gates),
    }


def write_checks_json(conclusions: Mapping[str, str], staging_dir: Path) -> Path:
    """Write the raw check-run snapshot to ``evidence/checks.json``."""
    path = _evidence_root(staging_dir) / "checks.json"
    path.write_text(
        json.dumps(dict(conclusions), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def write_readme(staging_dir: Path) -> Path:
    """Write the fixed human-orientation README to ``evidence/README.md``."""
    path = _evidence_root(staging_dir) / "README.md"
    path.write_text(_README_TEXT, encoding="utf-8")
    return path


def write_manifest(manifest: Mapping[str, Any], staging_dir: Path) -> Path:
    """Write the manifest object to ``evidence/manifest.json`` (schema §8).

    The manifest is the archive's top-level index, so it is staged inside the
    ``evidence/`` tree (and thus tarred into the bundle). ``sort_keys`` keeps the
    on-disk bytes deterministic for a byte-reproducible archive.
    """
    path = _evidence_root(staging_dir) / "manifest.json"
    path.write_text(
        json.dumps(dict(manifest), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def copy_sbom(sbom_file: Path, staging_dir: Path) -> Path:
    """Copy an already-built SBOM into ``evidence/gates/sbom/``."""
    dest_dir = _evidence_root(staging_dir) / "gates" / "sbom"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / sbom_file.name
    shutil.copyfile(sbom_file, dest)
    return dest


def assemble_bundle(staging_dir: Path, out_tarball: Path) -> Path:
    """tar.gz the ``evidence/`` tree at ``staging_dir`` into ``out_tarball``.

    Members are added in sorted order for a deterministic archive layout.
    """
    evidence_root = _evidence_root(staging_dir)
    members = sorted(path for path in evidence_root.rglob("*") if path.is_file())
    with tarfile.open(out_tarball, "w:gz") as tar:
        for member in members:
            tar.add(member, arcname=member.relative_to(staging_dir).as_posix())
    return out_tarball


# --- Completeness validation ---------------------------------------------
#
# The publish invariant, as a pure function: cross-check the required
# evidence-gate set (derived from branch protection, T1) against what was
# actually harvested (T3). A required gate with no harvested evidence is a
# substantive failure — terminal in enforcing mode (spec §9.2).


class IncompleteEvidenceError(Exception):
    """A required evidence gate produced no harvested evidence.

    Thin, data-carrying: ``.missing`` lists every required gate name with no
    evidence. The message format is defined here once so the CLI (T5) can reuse
    it via ``emit_error``. Substantive failure — terminal in enforcing mode.
    """

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(f"missing evidence for required gates: {missing}")


def validate_completeness(
    required: Sequence[EvidenceGate],
    harvested: Mapping[str, GateEvidence],
) -> None:
    """Raise :class:`IncompleteEvidenceError` for required gates lacking evidence.

    A pure set-difference of required gate names against harvested keys. The
    ``missing`` list preserves ``required`` order for a stable, readable report.
    """
    missing = [gate.name for gate in required if gate.name not in harvested]
    if missing:
        raise IncompleteEvidenceError(missing)


# --- GitHub harvest layer ------------------------------------------------
#
# All GitHub I/O for the evidence bundle, isolated behind mockable functions so
# the CLI and the bundle core stay network-free. Every call routes through
# ``vergil_tooling.lib.github``, whose ``_run_with_retry`` already retries
# transient API failures — no bespoke retry loop is needed here.

# Prefix a gate workflow uploads its evidence artifact under
# (``ci-evidence-<gate>``); the producer convention that decouples gate
# workflows from this harvester (spec §7).
_EVIDENCE_ARTIFACT_PREFIX = "ci-evidence-"


class NoQualifyingRunError(Exception):
    """No COMPLETED + SUCCESS CI run exists for the release head SHA.

    The one substantive-failure boundary of the harvest layer (spec §5.2):
    unlike a transient API error, it means the release commit was never
    validated by a green CI run, so no evidence can be harvested.
    """

    def __init__(self, head_sha: str) -> None:
        super().__init__(f"no completed+success CI run for head SHA {head_sha}")
        self.head_sha = head_sha


def _pr_from_commit_api(repo: str, merge_sha: str) -> int | None:
    """Return the PR number ``merge_sha`` closed, via ``/commits/{sha}/pulls``.

    Prefers a merged PR; falls back to the first associated PR. Returns None
    when the API associates no PR with the commit.
    """
    raw = github.read_json("api", f"repos/{repo}/commits/{merge_sha}/pulls")
    prs = cast("list[dict[str, Any]]", raw) if isinstance(raw, list) else []
    merged = [pr for pr in prs if pr.get("merged_at")]
    chosen = merged or prs
    if not chosen:
        return None
    return int(chosen[0]["number"])


def resolve_release_pr(repo: str, merge_sha: str) -> int:
    """Resolve the release PR a merge commit closed.

    Primary: the ``/commits/{sha}/pulls`` API. Fallback: the merge/squash
    commit subject (:func:`~vergil_tooling.lib.linkage.extract_merge_pr`) when
    the API associates no PR. Raises ``ValueError`` when neither resolves a PR.
    """
    pr = _pr_from_commit_api(repo, merge_sha)
    if pr is not None:
        return pr
    message = github.read_output(
        "api", f"repos/{repo}/commits/{merge_sha}", "--jq", ".commit.message"
    )
    subject = message.splitlines()[0] if message else ""
    pr = extract_merge_pr(subject)
    if pr is not None:
        return pr
    msg = f"cannot resolve release PR for {merge_sha} in {repo}"
    raise ValueError(msg)


def _is_qualifying_run(run: Mapping[str, Any], workflow: str) -> bool:
    """True when *run* is the named workflow, COMPLETED, and SUCCESS (spec §5.2).

    Cancelled and in-progress runs are excluded: only a run that completed
    successfully attests the release commit.
    """
    return (
        run.get("name") == workflow
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
    )


def select_ci_run(repo: str, head_sha: str, *, workflow: str = "CI") -> dict[str, Any]:
    """Return the latest COMPLETED + SUCCESS *workflow* run for ``head_sha``.

    Cancelled and in-progress runs are ignored. Raises
    :class:`NoQualifyingRunError` when no run qualifies (spec §5.2).
    """
    raw = github.read_json(
        "api",
        f"repos/{repo}/actions/runs",
        "-f",
        f"head_sha={head_sha}",
        "--jq",
        ".workflow_runs",
    )
    runs = cast("list[dict[str, Any]]", raw)
    qualifying = [run for run in runs if _is_qualifying_run(run, workflow)]
    if not qualifying:
        raise NoQualifyingRunError(head_sha)
    return max(qualifying, key=lambda run: run["run_started_at"])


def download_evidence_artifacts(repo: str, run_id: int, dest: Path) -> list[Path]:
    """Download every ``ci-evidence-*`` artifact of *run_id* into ``dest/<gate>/``.

    Non-evidence artifacts are ignored. The gate name is the artifact name with
    the ``ci-evidence-`` prefix stripped; each is downloaded into its own gate
    directory. Returns the created gate directories, sorted for determinism.
    """
    raw = github.read_json(
        "api", f"repos/{repo}/actions/runs/{run_id}/artifacts", "--jq", ".artifacts"
    )
    artifacts = cast("list[dict[str, Any]]", raw)
    gate_dirs: list[Path] = []
    for artifact in artifacts:
        name = str(artifact.get("name", ""))
        if not name.startswith(_EVIDENCE_ARTIFACT_PREFIX):
            continue
        gate = name[len(_EVIDENCE_ARTIFACT_PREFIX) :]
        gate_dir = dest / gate
        gate_dir.mkdir(parents=True, exist_ok=True)
        github.run(
            "run", "download", str(run_id), "--repo", repo, "--name", name, "--dir", str(gate_dir)
        )
        gate_dirs.append(gate_dir)
    return sorted(gate_dirs)


def read_gate_conclusions(repo: str, head_sha: str) -> dict[str, str]:
    """Return a check-run name → conclusion map for ``head_sha``.

    Feeds both the manifest's ``checks.json`` snapshot and completeness
    validation. A null conclusion (e.g. a still-neutral check) maps to the
    empty string.
    """
    raw = github.read_json(
        "api", f"repos/{repo}/commits/{head_sha}/check-runs", "--jq", ".check_runs"
    )
    check_runs = cast("list[dict[str, Any]]", raw)
    return {str(run["name"]): str(run.get("conclusion") or "") for run in check_runs}


def resolve_required_gates(repo: str, repo_root: Path) -> tuple[EvidenceGate, ...]:
    """Derive the evidence gates *repo* MUST emit, from its ``vergil.toml``.

    Reads the repo's config and resolves its GHAS posture (a declared
    ``[project].ghas`` wins, else it is inferred from repo visibility via the
    GitHub API), then defers to
    :func:`~vergil_tooling.lib.github_config.required_evidence_gates` — the same
    required-status-check computation that drives branch protection, so the
    enforced gates and the evidence-required gates cannot drift apart.
    """
    config = read_config(repo_root)
    ghas = ghas_available(config, visibility=github.repo_visibility(repo))
    return required_evidence_gates(config.project, config.ci, ghas=ghas)


def _gate_conclusion(checks: Sequence[str], conclusions: Mapping[str, str]) -> str:
    """Aggregate a gate's required-check conclusions into one gate conclusion.

    Reports ``success`` only when every required check succeeded; otherwise the
    first non-``success`` conclusion, so a failed check surfaces rather than
    being silently rolled up as success. A null/absent conclusion reports as
    ``unknown``.
    """
    for name in checks:
        result = conclusions.get(name, "")
        if result != "success":
            return result or "unknown"
    return "success"


def load_gate_evidence(gate_dir: Path, *, conclusion: str) -> GateEvidence:
    """Parse ``gate_dir/evidence.json`` into a :class:`GateEvidence`.

    The gate name is the directory name (the ``ci-evidence-`` prefix already
    stripped by :func:`download_evidence_artifacts`). ``tools`` and ``metrics``
    come from the fragment (spec §7); ``conclusion`` is injected by the caller
    (derived from the check-runs API, not the fragment); ``files`` is every
    staged file in the gate directory, sorted for a deterministic manifest.
    """
    fragment = json.loads((gate_dir / "evidence.json").read_text(encoding="utf-8"))
    files = tuple(sorted(path for path in gate_dir.rglob("*") if path.is_file()))
    return GateEvidence(
        name=gate_dir.name,
        conclusion=conclusion,
        tools=tuple(dict(tool) for tool in fragment.get("tools", [])),
        metrics=dict(fragment.get("metrics", {})),
        files=files,
    )


def load_harvested_gates(
    gate_dirs: Sequence[Path],
    required: Sequence[EvidenceGate],
    conclusions: Mapping[str, str],
) -> dict[str, GateEvidence]:
    """Parse each downloaded gate directory into a name → :class:`GateEvidence` map.

    Each gate's conclusion is derived from its required checks' conclusions
    (:func:`_gate_conclusion`); a downloaded gate with no matching required gate
    derives from an empty check set (``success``) rather than being dropped.
    """
    checks_by_gate = {gate.name: gate.checks for gate in required}
    harvested: dict[str, GateEvidence] = {}
    for gate_dir in gate_dirs:
        conclusion = _gate_conclusion(checks_by_gate.get(gate_dir.name, ()), conclusions)
        evidence = load_gate_evidence(gate_dir, conclusion=conclusion)
        harvested[evidence.name] = evidence
    return harvested
