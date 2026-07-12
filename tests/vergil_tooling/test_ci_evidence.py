"""Tests for vergil_tooling.lib.ci_evidence."""

from __future__ import annotations

import json
import tarfile
from typing import TYPE_CHECKING

from vergil_tooling.lib.ci_evidence import (
    GateEvidence,
    HarvestContext,
    assemble_bundle,
    build_manifest,
    copy_sbom,
    sha256_file,
    write_checks_json,
    write_readme,
)

if TYPE_CHECKING:
    from pathlib import Path


def _stage_one_gate(tmp_path: Path) -> Path:
    """Create a staging dir with one gate's evidence file staged under it.

    Returns the staging dir; writes ``evidence/gates/test/coverage.xml``.
    """
    staging = tmp_path / "staging"
    gate_dir = staging / "evidence" / "gates" / "test"
    gate_dir.mkdir(parents=True)
    (gate_dir / "coverage.xml").write_text("<coverage/>", encoding="utf-8")
    return staging


def _gate_test(staging: Path) -> GateEvidence:
    """A ``test`` GateEvidence referencing the staged coverage.xml."""
    return GateEvidence(
        name="test",
        conclusion="success",
        tools=({"name": "pytest", "version": "8.0"},),
        metrics={"coverage_pct": 100, "tests": 1423},
        files=(staging / "evidence" / "gates" / "test" / "coverage.xml",),
    )


def _ctx() -> HarvestContext:
    return HarvestContext(
        repo="o/r",
        version="2.1.129",
        tag="v2.1.129",
        released_commit="deadbeef",
        release_pr=2281,
        validated_head_sha="cafef00d",
        ci_run_urls=("https://github.com/o/r/actions/runs/1",),
    )


def test_sha256_file(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_text("hello")
    assert sha256_file(p) == ("2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824")


def test_build_manifest_shape(tmp_path: Path) -> None:
    staging = _stage_one_gate(tmp_path)
    ctx = _ctx()
    m = build_manifest(
        ctx,
        [_gate_test(staging)],
        generated_at="2026-07-12T00:00:00Z",
        missing_gates=[],
        staging_dir=staging,
    )
    assert m["schema_version"] == "1.0"
    assert m["repo"] == "o/r"
    assert m["release"]["tag"] == "v2.1.129"
    assert m["release"]["version"] == "2.1.129"
    assert m["release"]["released_commit"] == "deadbeef"
    assert m["provenance"]["release_pr"] == 2281
    assert m["provenance"]["validated_head_sha"] == "cafef00d"
    assert m["provenance"]["ci_run_urls"] == ["https://github.com/o/r/actions/runs/1"]
    assert m["generated_at"] == "2026-07-12T00:00:00Z"
    assert m["gates"][0]["name"] == "test"
    assert m["gates"][0]["conclusion"] == "success"
    assert m["gates"][0]["tools"] == [{"name": "pytest", "version": "8.0"}]
    assert m["gates"][0]["metrics"] == {"coverage_pct": 100, "tests": 1423}
    assert m["gates"][0]["files"][0]["path"] == "gates/test/coverage.xml"
    assert m["gates"][0]["files"][0]["sha256"]  # hashed
    assert m["missing_gates"] == []


def test_build_manifest_records_missing_gates(tmp_path: Path) -> None:
    staging = _stage_one_gate(tmp_path)
    m = build_manifest(
        _ctx(),
        [_gate_test(staging)],
        generated_at="2026-07-12T00:00:00Z",
        missing_gates=["security"],
        staging_dir=staging,
    )
    assert m["missing_gates"] == ["security"]


def test_assemble_bundle_roundtrip(tmp_path: Path) -> None:
    staging = _stage_one_gate(tmp_path)
    out = tmp_path / "bundle.tar.gz"
    result = assemble_bundle(staging, out)
    assert result == out
    with tarfile.open(out) as tf:
        names = tf.getnames()
    assert "evidence/gates/test/coverage.xml" in names


def test_assemble_bundle_is_deterministic(tmp_path: Path) -> None:
    staging = _stage_one_gate(tmp_path)
    (staging / "evidence" / "gates" / "test" / "junit.xml").write_text("<junit/>", encoding="utf-8")
    out = tmp_path / "bundle.tar.gz"
    assemble_bundle(staging, out)
    with tarfile.open(out) as tf:
        names = tf.getnames()
    assert names == sorted(names)


def test_write_checks_json(tmp_path: Path) -> None:
    staging = tmp_path / "s"
    (staging / "evidence").mkdir(parents=True)
    p = write_checks_json({"test / unit / 3.14": "success"}, staging)
    assert p == staging / "evidence" / "checks.json"
    assert json.loads(p.read_text())["test / unit / 3.14"] == "success"


def test_write_readme(tmp_path: Path) -> None:
    staging = tmp_path / "s"
    (staging / "evidence").mkdir(parents=True)
    p = write_readme(staging)
    assert p == staging / "evidence" / "README.md"
    assert "evidence" in p.read_text().lower()


def test_copy_sbom(tmp_path: Path) -> None:
    sbom = tmp_path / "sbom.cdx.json"
    sbom.write_text("{}")
    staging = tmp_path / "s"
    (staging / "evidence").mkdir(parents=True)
    out = copy_sbom(sbom, staging)
    assert out == staging / "evidence" / "gates" / "sbom" / "sbom.cdx.json"
    assert out.read_text() == "{}"
