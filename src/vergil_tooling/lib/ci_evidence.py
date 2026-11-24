"""CI evidence bundle core — manifest builder and bundle assembler.

Pure, network-free shaping of a release's CI-evidence bundle: hash files,
build the ``manifest.json`` index (schema 1.0, spec §8), write the raw
``checks.json`` snapshot and the human-orientation ``README.md``, copy the
already-built SBOM into the tree, and tar the ``evidence/`` tree into the
release asset.

The end-to-end flow is split into two orchestrators so a release harvests its
evidence exactly once (issue #2330):

- :func:`run_harvest` — the network + validation half. Resolve the release PR,
  select the qualifying CI run, download the required gates' evidence artifacts, read
  the gate conclusions, and enforce completeness; persist the harvested tree
  plus a small JSON state file (:class:`HarvestState`) into a staging directory
  so a later assemble needs no network.
- :func:`run_assemble` — the network-free half. Read the persisted harvest and
  state, write ``checks.json`` / ``README.md`` / the SBOM, build and write the
  manifest, and tar the tree into the release asset + a standalone manifest.

:func:`run_bundle` composes the two (a temp staging dir) so the original atomic
``bundle`` behaviour is preserved for local dogfooding and back-compat.

Determinism: every pure function is a pure function of its inputs. Timestamps
(``generated_at``) are injected by the caller, never read from the clock here,
so the logic stays unit-testable and byte-reproducible.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sys
import tarfile
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from vergil_tooling.lib import github, trust
from vergil_tooling.lib.github_config import (
    GHAS_INTEGRATION_ID,
    GITHUB_ACTIONS_INTEGRATION_ID,
    required_evidence_gates,
    resolved_integration_versions,
)
from vergil_tooling.lib.linkage import extract_merge_pr
from vergil_tooling.lib.remote_config import require_config_at_ref
from vergil_tooling.lib.retry import compute_delay

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from vergil_tooling.lib.github_config import EvidenceGate

log = logging.getLogger(__name__)

# Schema version of the manifest object (spec §8).
SCHEMA_VERSION = "1.0"


def evidence_asset_name(tag: str) -> str:
    """Return the CI-evidence tarball asset name for a release *tag*.

    The single source of truth for the bundle's published filename: both the
    harvester (which writes and uploads the asset) and the doc-site link (which
    points at it) derive the name here, so they cannot drift apart.
    """
    return f"{tag}-ci-evidence.tar.gz"


def evidence_manifest_name(tag: str) -> str:
    """Return the CI-evidence standalone-manifest asset name for a release *tag*."""
    return f"{tag}-ci-evidence-manifest.json"


# The SBOM's directory under ``evidence/gates/``. It sits beside the gate
# directories but is not a gate: it has no ``evidence.json`` and no manifest entry.
_SBOM_DIRNAME = "sbom"

# The parts of ``evidence/README.md`` that never vary. The layout between them is
# rendered from the assembled tree by :func:`render_readme` (issue #4402), so the
# README describes only the entries the archive actually holds.
_README_HEAD = """\
# CI Evidence Archive

This archive is the durable, machine-verifiable evidence bundle for a release.
It captures the complete output of every required CI gate at release time.

## Layout

- `manifest.json` — top-level machine index (schema 1.0): release identity,
  provenance (release PR, validated head SHA, CI run URLs), per-gate tools,
  metrics, and a `sha256` for every gate file.
- `checks.json` — the raw check-run snapshot (name → conclusion).
"""

_README_TAIL = """
## Verifying integrity

Each file listed in `manifest.json` carries a `sha256`. Recompute it over the
extracted file and compare to prove the archive is intact. The bundle is also
covered by a build-provenance attestation on the GitHub Release.
"""


def _code_list(items: Sequence[str]) -> str:
    """Join *items* as a comma-separated list of Markdown code spans."""
    return ", ".join(f"`{item}`" for item in items)


def render_readme(staging_dir: Path) -> str:
    """Render ``evidence/README.md`` from the entries under ``evidence/gates/``.

    Each gate directory present is named, and the SBOM line says whether this
    archive holds ``gates/sbom/``: a release that builds no package has no SBOM,
    so a fixed text naming it would describe an entry that is not there.
    """
    gates_root = _evidence_root(staging_dir) / "gates"
    entries = sorted(p for p in gates_root.iterdir() if p.is_dir()) if gates_root.is_dir() else []
    gate_dirs = [f"gates/{p.name}/" for p in entries if p.name != _SBOM_DIRNAME]
    sbom_dir = gates_root / _SBOM_DIRNAME

    if gate_dirs:
        gates_line = (
            "- `gates/<gate>/` — each required gate's report files plus its\n"
            f"  `evidence.json` fragment. This archive holds {_code_list(gate_dirs)}.\n"
        )
    else:
        gates_line = "- This archive holds no gate directories.\n"

    if sbom_dir.is_dir():
        sbom_files = sorted(
            p.relative_to(sbom_dir).as_posix() for p in sbom_dir.rglob("*") if p.is_file()
        )
        sbom_line = (
            "- `gates/sbom/` — the release's package SBOM "
            f"({_code_list(sbom_files)}), also a standalone\n"
            "  Release asset. It is not a gate and has no `manifest.json` entry.\n"
        )
    else:
        sbom_line = "- This release built no package SBOM, so the archive has no `gates/sbom/`.\n"

    return _README_HEAD + gates_line + sbom_line + _README_TAIL


@dataclass(frozen=True)
class SkipRecord:
    """One gate leg's record that ``vrg-validate`` skipped it (issue #5034).

    vergil-actions' report staging (``actions/ci/evidence/reports``,
    vergil-actions#1294) writes it in place of the leg's reports when the
    repository is not bootstrapped yet: *leg* is the gate the leg ran
    (``test``, ``audit``, or the quality gate's ``lint`` and ``typecheck``),
    *head* the commit it checked out, and *path* the file in the gate
    directory.
    """

    leg: str
    language: str
    version: str
    reason: str
    head: str
    missing_manifests: tuple[str, ...]
    path: Path


@dataclass(frozen=True)
class GateEvidence:
    """Harvested evidence for one CI gate, staged under ``gates/<name>/``.

    *skips* holds the gate's skip records (:class:`SkipRecord`), which are
    also among its *files*.
    """

    name: str
    conclusion: str
    tools: tuple[dict[str, Any], ...]
    metrics: dict[str, Any]
    files: tuple[Path, ...]
    skips: tuple[SkipRecord, ...] = ()


# --- Skip records (issue #5034) ------------------------------------------
#
# A gate leg vrg-validate skipped because the repository is not bootstrapped
# yet writes no report. vergil-actions' report staging (vergil-actions#1294,
# ``actions/ci/evidence/reports/reports.sh``) proves the skip and stages a
# skip record in its place, named ``<leg>-not-bootstrapped-<version>.json``
# (``quality-<leg>-not-bootstrapped-<version>.json`` for a quality leg):
#
#     {"gate": "<leg>", "language": "<language>", "version": "<version>",
#      "skipped": true, "reason": "not-bootstrapped", "head": "<sha>",
#      "missing_manifests": ["<manifest>", ...]}
#
# The harvester recognises each one, refuses one that is not exactly that,
# and records the leg as skipped in the manifest, so the release evidence
# says the gate was skipped instead of passing a skip record off as a report.

#: The reason a skip record carries, the one reason vergil-actions stages.
SKIP_REASON_NOT_BOOTSTRAPPED = "not-bootstrapped"

# The legs whose skip records each evidence gate's directory may hold.
_SKIP_LEGS: dict[str, tuple[str, ...]] = {
    "test": ("test",),
    "audit": ("audit",),
    "quality": ("lint", "typecheck"),
}

# The name marker that makes a gate file a skip record.
_SKIP_MARKER = f"-{SKIP_REASON_NOT_BOOTSTRAPPED}-"

_SKIP_FIELDS = frozenset(
    {"gate", "language", "version", "skipped", "reason", "head", "missing_manifests"}
)


class SkipRecordError(ValueError):
    """A gate file named as a skip record is not a valid one for its gate.

    Substantive harvest failure surfaced by the CLI as a single-line error:
    a skip record that cannot be read, or that names another leg, version or
    commit, is never taken as evidence that a gate was skipped.
    """


def is_skip_record(path: Path) -> bool:
    """True when *path* is named as a skip record (``…-not-bootstrapped-<version>.json``)."""
    return path.suffix == ".json" and _SKIP_MARKER in path.name


def _skip_record_name(leg: str, version: str) -> str:
    """The name vergil-actions gives *leg*'s skip record for *version*."""
    prefix = "quality-" if leg in _SKIP_LEGS["quality"] else ""
    return f"{prefix}{leg}{_SKIP_MARKER}{version}.json"


def load_skip_record(path: Path, gate: str) -> SkipRecord:
    """Read the skip record at *path* in *gate*'s directory.

    Raises :class:`SkipRecordError` unless it is a JSON object with every
    field vergil-actions writes, ``skipped: true``, the ``not-bootstrapped``
    reason, a leg of *gate* and a name that matches its leg and version.
    """
    bad = f"skip record {path.name} of gate {gate!r}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = f"{bad} cannot be read: {exc}"
        raise SkipRecordError(msg) from exc
    if not isinstance(data, dict) or not _SKIP_FIELDS <= set(data):
        msg = f"{bad} must be a JSON object holding {', '.join(sorted(_SKIP_FIELDS))}"
        raise SkipRecordError(msg)
    strings = ("gate", "language", "version", "reason", "head")
    if not all(isinstance(data[key], str) and data[key] for key in strings):
        msg = f"{bad} must give {', '.join(strings)} as non-empty strings"
        raise SkipRecordError(msg)
    manifests = data["missing_manifests"]
    if (
        not isinstance(manifests, list)
        or not manifests
        or not all(isinstance(m, str) and m for m in manifests)
    ):
        msg = f"{bad} must list its missing_manifests"
        raise SkipRecordError(msg)
    if data["skipped"] is not True or data["reason"] != SKIP_REASON_NOT_BOOTSTRAPPED:
        msg = f"{bad} must be skipped: true with reason {SKIP_REASON_NOT_BOOTSTRAPPED!r}"
        raise SkipRecordError(msg)
    leg = data["gate"]
    legs = _SKIP_LEGS.get(gate, ())
    if leg not in legs:
        allowed = ", ".join(legs) if legs else "none"
        msg = f"{bad} is of leg {leg!r}, but gate {gate!r} has skippable legs: {allowed}"
        raise SkipRecordError(msg)
    expected = _skip_record_name(leg, data["version"])
    if path.name != expected:
        msg = f"{bad} records leg {leg!r} version {data['version']!r}, so must be named {expected}"
        raise SkipRecordError(msg)
    return SkipRecord(
        leg=leg,
        language=data["language"],
        version=data["version"],
        reason=data["reason"],
        head=data["head"],
        missing_manifests=tuple(manifests),
        path=path,
    )


def validate_skip_heads(harvested: Mapping[str, GateEvidence], head_sha: str) -> None:
    """Raise :class:`SkipRecordError` for a skip record not of *head_sha*.

    A skip record proves a leg was skipped on the commit it names, so one of
    any commit but the release PR's validated head is no evidence for it.
    """
    stale = [
        f"{gate.name}/{skip.path.name} ({skip.head})"
        for gate in harvested.values()
        for skip in gate.skips
        if skip.head != head_sha
    ]
    if stale:
        msg = f"skip records not of the validated head {head_sha}: {', '.join(stale)}"
        raise SkipRecordError(msg)


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


# --- Persisted harvest state (harvest → assemble hand-off, issue #2330) --
#
# ``run_harvest`` writes a small JSON state file next to the harvested
# ``evidence/`` tree so a later ``run_assemble`` can rebuild the manifest with
# no network. The schema is a stable interface: cd-release (vergil-actions)
# persists it across two workflow steps, and other callers may read it, so it
# is versioned and only extended in a backward-compatible way.

# Filename of the persisted state, written at the staging-dir root (a sibling
# of ``evidence/``, so it is never tarred into the bundle).
HARVEST_STATE_FILENAME = "harvest-state.json"

# Schema version of the persisted state object. Bump only on a
# backward-incompatible change; ``read_harvest_state`` refuses an unknown major.
HARVEST_STATE_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class HarvestState:
    """The network-derived facts a harvest persists for a later assemble.

    Everything :func:`run_assemble` needs that it cannot re-derive from the
    on-disk ``evidence/`` tree, and nothing that assemble supplies itself
    (``version``, ``generated_at``, the SBOM). Serialised to
    ``<staging>/harvest-state.json`` (schema
    :data:`HARVEST_STATE_SCHEMA_VERSION`).

    - ``repo`` / ``released_commit`` — release identity for the manifest.
    - ``release_pr`` / ``validated_head_sha`` / ``ci_run_urls`` — provenance.
    - ``checks`` — the raw check-run name → conclusion snapshot, reproducing
      ``checks.json`` at assemble time without a second API call.
    - ``gate_conclusions`` — per-gate name → aggregated conclusion, injected
      into each rebuilt :class:`GateEvidence` (the gate's tools/metrics/files
      come from the persisted tree).
    """

    repo: str
    released_commit: str
    release_pr: int
    validated_head_sha: str
    ci_run_urls: tuple[str, ...]
    checks: dict[str, str]
    gate_conclusions: dict[str, str]


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
    """Render one gate's manifest entry, hashing each staged file once.

    ``skipped`` lists each leg the gate's skip records show skipped (empty
    when none was), with the record's path, which ``files`` also lists.
    """
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
        "skipped": [
            {
                "leg": skip.leg,
                "language": skip.language,
                "version": skip.version,
                "reason": skip.reason,
                "missing_manifests": list(skip.missing_manifests),
                "path": skip.path.relative_to(evidence_root).as_posix(),
            }
            for skip in gate.skips
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
    """Write the human-orientation README to ``evidence/README.md``.

    Rendered by :func:`render_readme` from the tree as it stands, so call it
    after every gate directory and the optional SBOM are in place.
    """
    path = _evidence_root(staging_dir) / "README.md"
    path.write_text(render_readme(staging_dir), encoding="utf-8")
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


def write_harvest_state(state: HarvestState, staging_dir: Path) -> Path:
    """Serialise *state* to ``<staging_dir>/harvest-state.json`` (schema §2330).

    Written at the staging-dir root — a sibling of ``evidence/`` — so it rides
    the job filesystem between the harvest and assemble steps without being
    tarred into the bundle. ``sort_keys`` keeps the bytes deterministic.
    """
    path = staging_dir / HARVEST_STATE_FILENAME
    payload = {
        "schema_version": HARVEST_STATE_SCHEMA_VERSION,
        "repo": state.repo,
        "released_commit": state.released_commit,
        "release_pr": state.release_pr,
        "validated_head_sha": state.validated_head_sha,
        "ci_run_urls": list(state.ci_run_urls),
        "checks": dict(state.checks),
        "gate_conclusions": dict(state.gate_conclusions),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


class HarvestStateError(ValueError):
    """The persisted harvest state is missing, unreadable, or malformed.

    Raised by :func:`read_harvest_state` so an assemble against a harvest that
    never completed (or wrote a broken state file) fails with a single-line,
    actionable message instead of a traceback (issue #3119). Subclasses
    :class:`ValueError` so existing ``ValueError`` callers keep working.
    """


# Suffix shared by every "the harvest did not leave usable state" message: the
# harvest-state file is written only as the last step of a successful
# pre-publish ``harvest``, so its absence or corruption means that gate failed.
_HARVEST_INCOMPLETE = "the pre-publish evidence gate did not complete"


def read_harvest_state(staging_dir: Path) -> HarvestState:
    """Load the :class:`HarvestState` persisted at ``<staging_dir>``.

    Raises :class:`HarvestStateError` (a single-line, traceback-free failure
    the CLI reports) when the state file is missing, unreadable, not valid
    JSON, the wrong shape, or carries an unknown ``schema_version`` — an
    assemble against an absent or incompatible harvest is a hard failure,
    never a silent best-effort read.
    """
    path = staging_dir / HARVEST_STATE_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        msg = f"harvest state not found in {staging_dir}: {_HARVEST_INCOMPLETE}"
        raise HarvestStateError(msg) from None
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"harvest state unreadable at {path} ({exc}): {_HARVEST_INCOMPLETE}"
        raise HarvestStateError(msg) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"harvest state is not valid JSON at {path} ({exc}): {_HARVEST_INCOMPLETE}"
        raise HarvestStateError(msg) from exc
    if not isinstance(data, dict):
        msg = (
            f"harvest state malformed at {path} "
            f"(expected a JSON object, got {type(data).__name__}): {_HARVEST_INCOMPLETE}"
        )
        raise HarvestStateError(msg)
    schema = data.get("schema_version")
    if schema != HARVEST_STATE_SCHEMA_VERSION:
        msg = (
            f"unsupported harvest-state schema {schema!r} at {path} "
            f"(expected {HARVEST_STATE_SCHEMA_VERSION!r})"
        )
        raise HarvestStateError(msg)
    try:
        return HarvestState(
            repo=str(data["repo"]),
            released_commit=str(data["released_commit"]),
            release_pr=int(data["release_pr"]),
            validated_head_sha=str(data["validated_head_sha"]),
            ci_run_urls=tuple(str(url) for url in data["ci_run_urls"]),
            checks={str(k): str(v) for k, v in data["checks"].items()},
            gate_conclusions={str(k): str(v) for k, v in data["gate_conclusions"].items()},
        )
    except KeyError as exc:
        msg = f"harvest state malformed at {path} (missing key {exc}): {_HARVEST_INCOMPLETE}"
        raise HarvestStateError(msg) from exc
    except (TypeError, ValueError, AttributeError) as exc:
        msg = (
            f"harvest state malformed at {path} "
            f"({type(exc).__name__}: {exc}): {_HARVEST_INCOMPLETE}"
        )
        raise HarvestStateError(msg) from exc


def copy_sbom(sbom_file: Path, staging_dir: Path) -> Path:
    """Copy an already-built SBOM into ``evidence/gates/sbom/``."""
    dest_dir = _evidence_root(staging_dir) / "gates" / _SBOM_DIRNAME
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
    """A required evidence gate is absent or carries an empty payload.

    Thin, data-carrying, distinguishing the two substantive failure modes:

    - ``.missing`` — required gates with no harvested evidence at all.
    - ``.empty_payload`` — required gates that were harvested but carry no
      report file, only the ``evidence.json`` envelope. This is the #2812 root
      cause: for weeks the test/audit/quality gates shipped empty payloads and
      the name-only completeness check passed them green even while enforcing.

    The message format is defined here once so the CLI (T5) can reuse it via
    ``emit_error``. Substantive failure — terminal in enforcing mode.
    """

    def __init__(
        self, missing: list[str] | None = None, empty_payload: list[str] | None = None
    ) -> None:
        self.missing = missing or []
        self.empty_payload = empty_payload or []
        parts: list[str] = []
        if self.missing:
            parts.append(f"required gates with no evidence artifact: {self.missing}")
        if self.empty_payload:
            parts.append(
                "required gates with an empty payload "
                f"(only evidence.json, no report files): {self.empty_payload}"
            )
        super().__init__("; ".join(parts))


def _has_report_payload(evidence: GateEvidence) -> bool:
    """True when a gate carries at least one report file besides ``evidence.json``.

    :func:`load_gate_evidence` builds ``GateEvidence.files`` from
    ``gate_dir.rglob("*")``, which **always** includes the ``evidence.json``
    envelope itself — so an empty gate still has one file. Completeness must
    therefore count report files *excluding* ``evidence.json``; a bare
    ``len(files) > 0`` would wave the empty payload through (#2812).
    """
    return any(path.name != "evidence.json" for path in evidence.files)


def validate_completeness(
    required: Sequence[EvidenceGate],
    harvested: Mapping[str, GateEvidence],
) -> None:
    """Raise :class:`IncompleteEvidenceError` for incomplete required gates.

    A required gate is complete only if it is present **and** carries at least
    one report file besides ``evidence.json``. Both failure modes are collected
    and reported together, each preserving ``required`` order for a stable,
    readable report:

    - **missing** — no harvested :class:`GateEvidence` for the gate at all.
    - **empty payload** — present but every staged file is ``evidence.json``
      (no real report), the empty-envelope bug this guard exists to catch.

    A skip record (:class:`SkipRecord`) counts as payload: it is the gate's
    evidence that ``vrg-validate`` skipped it, and the manifest records the
    gate's legs as ``skipped``.
    """
    missing =[gate.name for gate in required if gate.name not in harvested]
    empty_payload = [
        gate.name
        for gate in required
        if gate.name in harvested and not _has_report_payload(harvested[gate.name])
    ]
    if missing or empty_payload:
        raise IncompleteEvidenceError(missing, empty_payload)


# --- GitHub harvest layer ------------------------------------------------
#
# All GitHub I/O for the evidence bundle, isolated behind mockable functions so
# the CLI and the bundle core stay network-free. Every call routes through
# ``vergil_tooling.lib.github``, whose ``_run_with_retry`` already retries
# transient API failures. The one bespoke loop is :func:`wait_for_ci_run`,
# which re-queries an *empty* head-SHA run lookup while GitHub's index lags.

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

    def __init__(self, head_sha: str, *, visible: bool = True, waited: float = 0.0) -> None:
        message = f"no completed+success CI run for head SHA {head_sha}"
        if waited > 0:
            message += f" (no run became visible within {waited:g}s; GitHub's run index may lag)"
        super().__init__(message)
        self.head_sha = head_sha
        # False when the head-SHA lookup returned no run of the workflow at all
        # — the one case that may be GitHub's run index lagging (issue #3434).
        self.visible = visible


class EvidenceArtifactError(Exception):
    """A required gate's evidence artifact cannot be consumed safely.

    Raised when every listed copy of a gate's artifact has expired (nothing is
    left to download), or when a downloaded archive carries an unsafe member
    path (absolute or ``..``-traversing). Substantive harvest failure surfaced
    by the CLI as a single-line error (issue #3171).
    """


class ReleasePrUnresolvedError(ValueError):
    """Neither the commits API nor the merge subject names the release PR.

    Substantive harvest failure surfaced by the CLI as a single-line error
    (issue #3119). Subclasses :class:`ValueError` for existing callers.
    """


def _pr_number(pr: Mapping[str, Any], what: str) -> int:
    """The ``number`` of the REST PR object *pr*, or a ``ValueError`` naming *what*."""
    number = pr.get("number")
    if not isinstance(number, int) or isinstance(number, bool):
        msg = f"{what} has no usable number ({number!r})"
        raise ValueError(msg)
    return number


def _head_ref(pr: Mapping[str, Any], what: str) -> str:
    """The head branch name of the REST PR object *pr*, or a ``ValueError``."""
    head = pr.get("head")
    ref = head.get("ref") if isinstance(head, dict) else None
    if not isinstance(ref, str) or not ref:
        msg = f"{what} has no head branch"
        raise ValueError(msg)
    return ref


def _merged_prs_from_commit_api(repo: str, merge_sha: str) -> tuple[dict[int, str], list[int]]:
    """The PRs ``/commits/{sha}/pulls`` associates with ``merge_sha``.

    Returns the merged ones as number → head branch, and the numbers of the
    rest (open, or closed unmerged). Only a merged PR can have closed the
    release; the rest are never taken in its place (GHSA-r4j2-cwcp-xg56).
    """
    prs = cast(
        "list[dict[str, Any]]",
        github.read_json_paginated(f"repos/{repo}/commits/{merge_sha}/pulls"),
    )
    merged: dict[int, str] = {}
    unmerged: list[int] = []
    for pr in prs:
        number = _pr_number(pr, f"a PR of commit {merge_sha}")
        if pr.get("merged_at"):
            merged[number] = _head_ref(pr, f"PR #{number}")
        else:
            unmerged.append(number)
    return merged, unmerged


def _pr_from_subject(repo: str, merge_sha: str) -> dict[int, str]:
    """The PR the merge/squash subject of ``merge_sha`` names, as number → head branch.

    Empty when the subject names no PR
    (:func:`~vergil_tooling.lib.linkage.extract_merge_pr`).
    """
    message = github.read_output(
        "api", f"repos/{repo}/commits/{merge_sha}", "--jq", ".commit.message"
    )
    subject = message.splitlines()[0] if message else ""
    number = extract_merge_pr(subject)
    if number is None:
        return {}
    pr = github.read_json("api", f"repos/{repo}/pulls/{number}")
    if not isinstance(pr, dict):
        msg = f"PR #{number} of {repo} is not a JSON object"
        raise ValueError(msg)
    return {number: _head_ref(pr, f"PR #{number}")}


def _trusted_merged(repo: str, candidates: Mapping[int, str]) -> list[trust.PRRef]:
    """The *candidates* that are merged same-repo PRs with a trusted author.

    Each candidate's branch is read through :func:`trust.prs_for_branch`, and
    only a PR with the candidate's own number counts, so a fork PR on the same
    branch name never stands in for it. The skipped PRs are reported on
    stderr, metadata only; a read failure raises :class:`trust.TrustReadError`.
    """
    found: dict[int, trust.PRRef] = {}
    skipped: list[trust.Skipped] = []
    for branch in sorted(set(candidates.values())):
        prs, branch_skipped = trust.prs_for_branch(repo, branch, state="merged")
        skipped.extend(branch_skipped)
        for pr in prs:
            if candidates.get(pr.number) == branch:
                found[pr.number] = pr
    trust.report_skipped(skipped, stream=sys.stderr)
    return [found[number] for number in sorted(found)]


def resolve_release_pr(repo: str, merge_sha: str) -> int:
    """Resolve the release PR a merge commit closed.

    The candidates come from the ``/commits/{sha}/pulls`` API's merged PRs or,
    when it associates no PR at all, from the merge/squash commit subject
    (:func:`~vergil_tooling.lib.linkage.extract_merge_pr`). Only a candidate
    that is a merged same-repo PR with a trusted author is taken, and it must
    be the only one: there is no first-PR fallback (GHSA-r4j2-cwcp-xg56,
    GHSA-q6hx-8c85-9f56). Raises :class:`ReleasePrUnresolvedError` when no
    PR, or more than one, qualifies.
    """
    candidates, unmerged = _merged_prs_from_commit_api(repo, merge_sha)
    if not candidates and not unmerged:
        candidates = _pr_from_subject(repo, merge_sha)
    if not candidates:
        named = f": only unmerged PR(s) {_numbers(unmerged)}" if unmerged else ""
        msg = f"cannot resolve release PR for {merge_sha} in {repo}{named}"
        raise ReleasePrUnresolvedError(msg)
    trusted = _trusted_merged(repo, candidates)
    if not trusted:
        msg = (
            f"cannot resolve release PR for {merge_sha} in {repo}: merged PR(s) "
            f"{_numbers(candidates)} are not same-repo PRs with a trusted author"
        )
        raise ReleasePrUnresolvedError(msg)
    if len(trusted) > 1:
        msg = (
            f"cannot resolve release PR for {merge_sha} in {repo}: "
            f"several trusted merged PRs {_numbers(pr.number for pr in trusted)}"
        )
        raise ReleasePrUnresolvedError(msg)
    return trusted[0].number


def _numbers(numbers: Iterable[int]) -> str:
    return ", ".join(f"#{n}" for n in sorted(numbers))


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


def _is_own_run(run: Mapping[str, Any], repo: str) -> bool:
    """True when *run* was built from *repo* itself, not from a fork.

    A fork's pull-request run lists under the base repo with the same head
    commit, so the run's ``head_repository`` decides it. A run with no head
    repository (a fork deleted since, for one) cannot be shown to be the
    repo's own, so it is not: it is skipped like a fork's, and never stops the
    selection (GHSA-q6hx-8c85-9f56).
    """
    head_repo = run.get("head_repository")
    full_name = head_repo.get("full_name") if isinstance(head_repo, dict) else None
    return isinstance(full_name, str) and full_name.lower() == repo.lower()


def select_ci_run(repo: str, head_sha: str, *, workflow: str = "CI") -> dict[str, Any]:
    """Return the latest COMPLETED + SUCCESS *workflow* run for ``head_sha``.

    Only a run built from *repo* itself counts: a fork's run of the same
    commit is ignored (:func:`_is_own_run`). Cancelled and in-progress runs
    are ignored. Raises :class:`NoQualifyingRunError` when no run qualifies
    (spec §5.2).
    """
    # The query string MUST be in the endpoint path, not a ``-f head_sha=…``
    # field: ``gh api`` switches to POST the moment any ``-f``/``-F`` field is
    # present, and ``GET /actions/runs`` is GET-only, so a POST returns HTTP 404
    # and silently sinks the whole harvest (issue #2335). Keeping the filter in
    # the URL preserves the GET verb.
    # Paginated: a commit re-run many times can exceed one page (issue #3160).
    raw = github.read_json_paginated(
        f"repos/{repo}/actions/runs?head_sha={head_sha}", "workflow_runs"
    )
    runs = [run for run in cast("list[dict[str, Any]]", raw) if _is_own_run(run, repo)]
    qualifying = [run for run in runs if _is_qualifying_run(run, workflow)]
    if not qualifying:
        visible = any(run.get("name") == workflow for run in runs)
        raise NoQualifyingRunError(head_sha, visible=visible)
    return max(qualifying, key=lambda run: run["run_started_at"])


# Default grace period for a CI run to appear in GitHub's head-SHA run index.
# The index can trail a just-completed run by minutes during a GitHub incident
# (issue #3434); 5 min sits well inside the gate step's 15-min timeout.
RUN_VISIBILITY_GRACE_SECS = 300.0


def wait_for_ci_run(
    repo: str,
    head_sha: str,
    *,
    grace: float = RUN_VISIBILITY_GRACE_SECS,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """:func:`select_ci_run`, re-queried while the head-SHA run index lags.

    Only an **empty** lookup (no ``CI`` run visible for ``head_sha`` at all)
    is retried: GitHub's run-by-SHA index can trail a run that already completed
    (issue #3434). Each retry is logged and backs off with
    :func:`~vergil_tooling.lib.retry.compute_delay`, the last sleep clamped to
    the deadline, then one final query. When *grace* elapses with still nothing
    visible, :class:`NoQualifyingRunError` is raised naming the wait. A visible
    run that is failed, cancelled or in progress fails at once, as before — the
    grace period never weakens the completed+success requirement. *sleep* and
    *clock* default to :func:`time.sleep`/:func:`time.monotonic` and are
    injectable for tests.
    """
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    deadline = clock() + grace
    attempt = 0
    while True:
        try:
            return select_ci_run(repo, head_sha)
        except NoQualifyingRunError as exc:
            if exc.visible:
                raise
            remaining = deadline - clock()
            if remaining <= 0:
                raise NoQualifyingRunError(head_sha, visible=False, waited=grace) from exc
            delay = min(compute_delay(attempt), remaining)
            log.warning(
                "CI run for %s not visible yet — GitHub index may lag; "
                "retrying in %.1fs (up to %gs total)",
                head_sha,
                delay,
                grace,
            )
            sleep(delay)
            attempt += 1


def download_evidence_artifacts(
    repo: str, run_id: int, dest: Path, required: Sequence[EvidenceGate]
) -> list[Path]:
    """Download *run_id*'s evidence artifacts for the *required* gates only.

    Only the **exact** ``ci-evidence-<gate>`` artifact of each required gate is
    consumed, each into its own ``dest/<gate>/`` directory. The security gate
    also uploads SARIF *partials* (``ci-evidence-security-part-codeql`` and
    friends) that carry no ``evidence.json``; scoping to the exact required-gate
    names ignores those partials — and any other non-matching artifact — rather
    than mis-parsing them as gates (issue #2340). A required gate whose artifact
    is genuinely absent from the run is simply not downloaded, surfacing as a
    substantive :class:`IncompleteEvidenceError` at completeness validation
    rather than being silently skipped. Returns the created gate directories,
    sorted for determinism.

    A re-run of a gate's evidence job uploads a second artifact under the
    **same** name, so a gate can list several copies (issue #3171). Exactly
    one is consumed — the newest unexpired copy (:func:`_choose_artifact`) —
    and it is fetched **by id** (``actions/artifacts/{id}/zip``), because
    ``gh run download --name`` cannot tell same-named artifacts apart and
    extracting both into one directory collides.
    """
    wanted = {f"{_EVIDENCE_ARTIFACT_PREFIX}{gate.name}": gate.name for gate in required}
    # Every page, not just the first 30: a release PR's CI run uploads more
    # artifacts than one default page holds, and a required gate's artifact on
    # page 2 otherwise reads as "no evidence artifact" (issue #3160).
    raw = github.read_json_paginated(f"repos/{repo}/actions/runs/{run_id}/artifacts", "artifacts")
    artifacts = cast("list[dict[str, Any]]", raw)
    by_gate: dict[str, list[dict[str, Any]]] = {}
    for artifact in artifacts:
        gate = wanted.get(str(artifact.get("name", "")))
        if gate is not None:
            by_gate.setdefault(gate, []).append(artifact)
    gate_dirs: list[Path] = []
    for gate, candidates in sorted(by_gate.items()):
        chosen = _choose_artifact(gate, candidates)
        gate_dir = dest / gate
        gate_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "artifact.zip"
            endpoint = f"repos/{repo}/actions/artifacts/{chosen['id']}/zip"
            github.download_api_binary(endpoint, archive)
            _extract_zip_safely(archive, gate_dir)
        gate_dirs.append(gate_dir)
    return sorted(gate_dirs)


def _artifact_recency(artifact: Mapping[str, Any]) -> tuple[str, int]:
    """Sort key: newest ``created_at`` (ISO-8601 sorts lexically), then highest id."""
    return (str(artifact.get("created_at") or ""), int(artifact["id"]))


def _choose_artifact(gate: str, candidates: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Pick the one artifact to consume for *gate* from its same-named *candidates*.

    The newest unexpired copy wins (highest ``created_at``, ties broken by the
    highest ``id``). When several copies exist a warning names every id and the
    chosen one, so a re-run's duplicate is visible rather than silent. Every
    copy expired raises :class:`EvidenceArtifactError` — there is nothing left
    to harvest, and that must fail loudly (issue #3171).
    """
    live = [a for a in candidates if not a.get("expired")]
    if not live:
        ids = sorted(int(a["id"]) for a in candidates)
        msg = f"every evidence artifact for gate {gate!r} has expired (artifact ids {ids})"
        raise EvidenceArtifactError(msg)
    chosen = max(live, key=_artifact_recency)
    if len(candidates) > 1:
        log.warning(
            "gate %r has %d evidence artifacts named %s%s (ids %s); using the newest "
            "unexpired one, id %s",
            gate,
            len(candidates),
            _EVIDENCE_ARTIFACT_PREFIX,
            gate,
            sorted(int(a["id"]) for a in candidates),
            chosen["id"],
        )
    return chosen


def _extract_zip_safely(archive: Path, dest: Path) -> None:
    """Extract *archive* into *dest*, refusing any member that escapes *dest*.

    Every member is checked **before** anything is written: an absolute path or
    one that resolves outside *dest* (``..`` traversal) raises
    :class:`EvidenceArtifactError` rather than being silently rewritten.
    """
    root = dest.resolve()
    with zipfile.ZipFile(archive) as zf:
        for name in zf.namelist():
            target = (root / name).resolve()
            if name.startswith(("/", "\\")) or not target.is_relative_to(root):
                msg = f"unsafe member path {name!r} in evidence artifact archive"
                raise EvidenceArtifactError(msg)
        zf.extractall(root)  # noqa: S202 — every member validated above


def _app_id(check_run: Mapping[str, Any]) -> int:
    """The ID of the App that created *check_run*, or a ``ValueError``."""
    app = check_run.get("app")
    app_id = app.get("id") if isinstance(app, dict) else None
    if not isinstance(app_id, int) or isinstance(app_id, bool):
        msg = f"check run {check_run.get('id')!r} has no creating app"
        raise ValueError(msg)
    return app_id


def _suite_id(check_run: Mapping[str, Any]) -> int:
    """The check suite ID of *check_run*, or a ``ValueError``."""
    suite = check_run.get("check_suite")
    suite_id = suite.get("id") if isinstance(suite, dict) else None
    if not isinstance(suite_id, int) or isinstance(suite_id, bool):
        msg = f"check run {check_run.get('id')!r} has no check suite"
        raise ValueError(msg)
    return suite_id


def _is_evidence_check(check_run: Mapping[str, Any], check_suite_id: int) -> bool:
    """True when *check_run* is one the selected CI run's evidence may count.

    That is a GitHub Actions check run of the selected CI run's own check
    suite, or one of the scanner checks GitHub Advanced Security registers
    for that commit (:data:`~vergil_tooling.lib.github.UPLOADED_CHECK_JOBS`).
    A check run of the same name from any other App or workflow never counts
    (GHSA-q6hx-8c85-9f56).
    """
    app_id = _app_id(check_run)
    if app_id == GITHUB_ACTIONS_INTEGRATION_ID:
        return _suite_id(check_run) == check_suite_id
    return app_id == GHAS_INTEGRATION_ID and check_run.get("name") in github.UPLOADED_CHECK_JOBS


def read_gate_conclusions(repo: str, head_sha: str, *, check_suite_id: int) -> dict[str, str]:
    """Return a check-run name → conclusion map for ``head_sha``.

    Only the check runs of the selected CI run's check suite
    (*check_suite_id*, the run's ``check_suite_id``) and the GitHub Advanced
    Security scanner checks count (:func:`_is_evidence_check`); every other
    check run on the commit is left out. Feeds both the manifest's
    ``checks.json`` snapshot and completeness validation. A null conclusion
    (e.g. a still-neutral check) maps to the empty string.
    """
    # Paginated: a release PR's matrix CI registers more than 30 check runs, and
    # a gate check past the first page would otherwise read as absent (#3160).
    raw = github.read_json_paginated(f"repos/{repo}/commits/{head_sha}/check-runs", "check_runs")
    check_runs = cast("list[dict[str, Any]]", raw)
    return {
        str(run["name"]): str(run.get("conclusion") or "")
        for run in check_runs
        if _is_evidence_check(run, check_suite_id)
    }


def _check_suite_id(run: Mapping[str, Any]) -> int:
    """The ``check_suite_id`` of the workflow *run*, or a ``ValueError``."""
    suite_id = run.get("check_suite_id")
    if not isinstance(suite_id, int) or isinstance(suite_id, bool):
        msg = f"workflow run {run.get('id')!r} has no check suite"
        raise ValueError(msg)
    return suite_id


def resolve_required_gates(repo: str, ref: str) -> tuple[EvidenceGate, ...]:
    """Derive the evidence gates *repo* MUST emit, from its ``vergil.toml`` at *ref*.

    The config is fetched for *repo* at *ref* through the contents API
    (:func:`~vergil_tooling.lib.remote_config.require_config_at_ref`), never
    read from the current directory, so one repo's checks can never be judged
    against another repo's config and the command works from any directory
    (#3226). :func:`run_harvest` passes the release PR's validated head SHA:
    that commit's ``vergil.toml`` is the config its CI run was built from, so
    the required set matches the run being evidenced. An absent or unparseable
    file raises :class:`~vergil_tooling.lib.remote_config.RemoteConfigError`;
    there is no fallback.

    Defers to
    :func:`~vergil_tooling.lib.github_config.required_evidence_gates` — the same
    required-status-check computation that drives branch protection, so the
    enforced gates and the evidence-required gates cannot drift apart.
    """
    config = require_config_at_ref(repo, ref)
    return required_evidence_gates(
        config.project,
        config.ci,
        docs=config.publish.docs,
        package=bool(config.packages),
        integration_versions=resolved_integration_versions(config),
    )


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
    Each top-level file named as a skip record is read as one
    (:func:`load_skip_record`, which refuses a bad one) into ``skips``.
    """
    fragment = json.loads((gate_dir / "evidence.json").read_text(encoding="utf-8"))
    files = tuple(sorted(path for path in gate_dir.rglob("*") if path.is_file()))
    skips = tuple(
        load_skip_record(path, gate_dir.name)
        for path in files
        if path.parent == gate_dir and is_skip_record(path)
    )
    return GateEvidence(
        name=gate_dir.name,
        conclusion=conclusion,
        tools=tuple(dict(tool) for tool in fragment.get("tools", [])),
        metrics=dict(fragment.get("metrics", {})),
        files=files,
        skips=skips,
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


def load_harvested_gates_from_state(state: HarvestState, staging_dir: Path) -> list[GateEvidence]:
    """Rebuild the harvested gates from a persisted state + on-disk tree.

    The network-free counterpart of :func:`load_harvested_gates`: the per-gate
    conclusion comes from ``state.gate_conclusions`` (already aggregated at
    harvest time) and the tools/metrics/files come from the persisted
    ``evidence/gates/<gate>/`` tree. Gates are returned in sorted-name order to
    match :func:`load_harvested_gates`, so the manifest stays byte-reproducible.
    """
    gates_root = _evidence_root(staging_dir) / "gates"
    return [
        load_gate_evidence(gates_root / name, conclusion=conclusion)
        for name, conclusion in sorted(state.gate_conclusions.items())
    ]


# --- End-to-end orchestrators (issue #2330) ------------------------------
#
# The two halves of a release's CI-evidence flow, split so the harvest runs
# exactly once. ``run_harvest`` is the network + validation gate; ``run_assemble``
# is the network-free attach; ``run_bundle`` composes them for back-compat.


def run_harvest(
    repo: str,
    merge_sha: str,
    staging_dir: Path,
    *,
    run_grace: float = RUN_VISIBILITY_GRACE_SECS,
) -> HarvestState:
    """Harvest a release's CI evidence into ``staging_dir`` (the pre-publish gate).

    Each line is one library stage: resolve the release PR, its validated head
    SHA and the qualifying CI run; derive the required gates and the check-run
    conclusions; download and parse the evidence artifacts; enforce completeness
    (raises :class:`IncompleteEvidenceError` before any state is persisted on a
    missing gate) and refuse a skip record not of the validated head
    (:func:`validate_skip_heads`). The harvested ``evidence/gates/`` tree and a
    :class:`HarvestState` file are written into ``staging_dir`` so a later
    :func:`run_assemble` needs no network. Returns the persisted state.
    """
    gates_dest = _evidence_root(staging_dir) / "gates"
    gates_dest.mkdir(parents=True, exist_ok=True)

    release_pr = resolve_release_pr(repo, merge_sha)
    # A bare PR number resolves against the cwd checkout's repo; the URL pins
    # the lookup to *repo* so it reads the right PR from any directory (#3220).
    head_sha = github.head_sha(f"https://github.com/{repo}/pull/{release_pr}")
    # GitHub's head-SHA run index can lag a just-completed run (#3434), so an
    # empty lookup is re-queried for *run_grace* seconds before failing.
    run = wait_for_ci_run(repo, head_sha, grace=run_grace)

    # The required set comes from *repo*'s own vergil.toml at the validated
    # head — never the cwd checkout, which may be another repo or none (#3226).
    required = resolve_required_gates(repo, head_sha)
    conclusions = read_gate_conclusions(repo, head_sha, check_suite_id=_check_suite_id(run))

    gate_dirs = download_evidence_artifacts(repo, int(run["id"]), gates_dest, required)
    harvested = load_harvested_gates(gate_dirs, required, conclusions)
    validate_completeness(required, harvested)
    validate_skip_heads(harvested, head_sha)

    state = HarvestState(
        repo=repo,
        released_commit=merge_sha,
        release_pr=release_pr,
        validated_head_sha=head_sha,
        ci_run_urls=(str(run["html_url"]),),
        checks=dict(conclusions),
        gate_conclusions={name: gate.conclusion for name, gate in harvested.items()},
    )
    write_harvest_state(state, staging_dir)
    return state


def run_assemble(
    staging_dir: Path,
    *,
    version: str,
    generated_at: str,
    out_dir: Path,
    sbom_file: Path | None = None,
) -> tuple[Path, Path]:
    """Assemble the bundle from a persisted harvest (the post-release attach).

    Network-free: read the :class:`HarvestState` and the on-disk ``evidence/``
    tree, write ``checks.json`` / ``README.md`` / the optional SBOM, build and
    write the manifest, tar the tree, and drop a standalone manifest next to the
    tarball. Returns ``(tarball, standalone_manifest)`` in ``out_dir``.
    """
    state = read_harvest_state(staging_dir)
    tag = f"v{version}"
    out_dir.mkdir(parents=True, exist_ok=True)

    write_checks_json(state.checks, staging_dir)
    if sbom_file is not None:
        copy_sbom(sbom_file, staging_dir)
    # After the SBOM copy: the README describes the tree as assembled.
    write_readme(staging_dir)

    gates = load_harvested_gates_from_state(state, staging_dir)
    ctx = HarvestContext(
        repo=state.repo,
        version=version,
        tag=tag,
        released_commit=state.released_commit,
        release_pr=state.release_pr,
        validated_head_sha=state.validated_head_sha,
        ci_run_urls=state.ci_run_urls,
    )
    manifest = build_manifest(
        ctx, gates, generated_at=generated_at, missing_gates=[], staging_dir=staging_dir
    )
    manifest_path = write_manifest(manifest, staging_dir)

    tarball = out_dir / evidence_asset_name(tag)
    assemble_bundle(staging_dir, tarball)
    standalone = out_dir / evidence_manifest_name(tag)
    shutil.copyfile(manifest_path, standalone)
    return tarball, standalone


def run_bundle(
    repo: str,
    *,
    version: str,
    merge_sha: str,
    generated_at: str,
    out_dir: Path,
    sbom_file: Path | None = None,
) -> tuple[Path, Path]:
    """Harvest then assemble in one shot, through a throwaway staging dir.

    The original atomic ``bundle`` behaviour: :func:`run_harvest` followed by
    :func:`run_assemble` over the same staging directory, so local dogfooding
    and back-compat callers keep a single command. A release that runs the two
    steps separately (cd-release) persists one shared staging dir instead and so
    harvests only once.
    """
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        run_harvest(repo, merge_sha, staging)
        return run_assemble(
            staging,
            version=version,
            generated_at=generated_at,
            out_dir=out_dir,
            sbom_file=sbom_file,
        )
