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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

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
