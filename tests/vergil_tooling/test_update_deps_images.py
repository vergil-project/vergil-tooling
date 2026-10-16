"""Tests for the images updater (spec §8.3, epic vergil-project/.github#381)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.update_deps.context import UpdateDepsContext, UpdateDepsError
from vergil_tooling.lib.update_deps.registry import ImageRef, parse_image_ref
from vergil_tooling.lib.update_deps.report import HeldBack
from vergil_tooling.lib.update_deps.updater import Updater
from vergil_tooling.lib.update_deps.updaters import images

if TYPE_CHECKING:
    from pathlib import Path


def _ref(text: str) -> ImageRef:
    ref = parse_image_ref(text)
    assert ref is not None
    return ref


def _ctx(root: Path, *, worktree: bool = True) -> UpdateDepsContext:
    return UpdateDepsContext(repo="o/r", repo_root=root, worktree_path=root if worktree else None)


def _ls_files(monkeypatch: pytest.MonkeyPatch, output: str) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    def read_output(*args: str) -> str:
        calls.append(args)
        return output

    monkeypatch.setattr(images.git, "read_output", read_output)
    return calls


# --- plan: exact tags ---------------------------------------------------------


def test_plan_exact_bumps_within_major_including_zero() -> None:
    new, entry = images.plan(
        _ref("ghcr.io/aquasecurity/trivy:0.71.0"),
        ["0.70.0", "0.71.0", "0.74.0", "0.74.0-amd64", "1.0.0"],
    )
    assert new == "0.74.0"
    assert entry == HeldBack(
        source="images",
        name="ghcr.io/aquasecurity/trivy",
        current="0.71.0",
        latest="1.0.0",
        note="newer major; never bumped automatically",
    )


def test_plan_exact_never_crosses_major() -> None:
    new, entry = images.plan(_ref("a/b:1.9.9"), ["1.9.9", "2.0.0", "3.1.0", "2.5.0"])
    assert new is None
    assert entry is not None
    assert entry.latest == "3.1.0"


def test_plan_exact_compares_numerically() -> None:
    new, _ = images.plan(_ref("a/b:1.9.0"), ["1.10.0", "1.9.0", "1.2.0"])
    assert new == "1.10.0"


def test_plan_exact_keeps_v_prefix() -> None:
    new, entry = images.plan(_ref("hadolint/hadolint:v2.12.0"), ["v2.12.0", "v2.14.1", "2.99.0"])
    assert new == "v2.14.1"
    assert entry is None


def test_plan_exact_already_newest() -> None:
    assert images.plan(_ref("a/b:1.2.3"), ["1.2.3", "1.2.2"]) == (None, None)


# --- plan: numeric series tags ------------------------------------------------


def test_plan_series_reports_newer_series_never_bumps() -> None:
    new, entry = images.plan(
        _ref("python:3.14-slim"),
        ["3.13-slim", "3.14-slim", "3.15-slim", "3.15", "3.15-alpine"],
    )
    assert new is None
    assert entry == HeldBack(
        source="images",
        name="python",
        current="3.14-slim",
        latest="3.15-slim",
        note="newer series; a judgement call",
    )


def test_plan_series_bare_numbers() -> None:
    _, entry = images.plan(_ref("ubuntu:24.04"), ["22.04", "24.04", "26.04", "24.04.1"])
    assert entry is not None
    assert entry.latest == "26.04"


def test_plan_series_single_part_ignores_two_part_tags() -> None:
    _, entry = images.plan(_ref("postgres:16"), ["16", "17", "18.1", "16-alpine"])
    assert entry is not None
    assert entry.latest == "17"


def test_plan_series_multi_segment_suffix() -> None:
    _, entry = images.plan(
        _ref("python:3.14-slim-bookworm"),
        ["3.14-slim-bookworm", "3.15-slim-bookworm", "3.16-slim-trixie"],
    )
    assert entry is not None
    assert entry.latest == "3.15-slim-bookworm"


def test_plan_series_already_newest() -> None:
    assert images.plan(_ref("ubuntu:24.04"), ["22.04", "24.04", "latest"]) == (None, None)


@pytest.mark.parametrize("text", ["a/b:stable", "debian:trixie-slim", "a/b:1.2.3.4", "a/b:v16"])
def test_plan_other_tag_shapes_skipped(text: str) -> None:
    assert images.plan(_ref(text), ["stable", "1.0.0", "99", "v99"]) == (None, None)


# --- find_refs ----------------------------------------------------------------


def test_find_refs_scans_dockerfiles_workflows_and_action_inputs(tmp_path: Path) -> None:
    (tmp_path / "docker").mkdir()
    (tmp_path / "docker" / "Dockerfile").write_text(
        "ARG BASE=x\nFROM --platform=$BUILDPLATFORM python:3.14-slim AS build\nFROM $BASE\n"
        "from build as final\n"
    )
    (tmp_path / "Dockerfile.dev").write_text("FROM node:22\n")
    (tmp_path / "ci.Dockerfile").write_text("FROM golang:1.25.1\n")
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(
        "jobs:\n  a:\n    container: hadolint/hadolint:v2.12.0\n"
        "  b:\n    container: ${{ matrix.image }}\n"
        '    services:\n      db:\n        image: "postgres:16"\n'
        "  c:\n    container:\n      image: 'redis:7.2.4' # pinned\n"
    )
    (wf / "notes.md").write_text("image: x:1.0.0\n")
    act = tmp_path / "actions" / "trivy" / "action.yml"
    act.parent.mkdir(parents=True)
    act.write_text(
        'inputs:\n  trivy-image:\n    default: "ghcr.io/aquasecurity/trivy:0.71.0"\n'
        '  format:\n    default: "sarif:2.1.0"\n'
        "  other-image:\n    description: no default\n"
        "  third-image: plain-string\n"
        "  number-image:\n    default: 3\n"
    )
    files = [
        "docker/Dockerfile",
        "Dockerfile.dev",
        "ci.Dockerfile",
        ".github/workflows/ci.yml",
        ".github/workflows/notes.md",
        "actions/trivy/action.yml",
        "README.md",
    ]
    found = [
        (p.relative_to(tmp_path).as_posix(), r.text) for p, r in images.find_refs(tmp_path, files)
    ]
    assert found == [
        ("docker/Dockerfile", "python:3.14-slim"),
        ("Dockerfile.dev", "node:22"),
        ("ci.Dockerfile", "golang:1.25.1"),
        (".github/workflows/ci.yml", "hadolint/hadolint:v2.12.0"),
        (".github/workflows/ci.yml", "postgres:16"),
        (".github/workflows/ci.yml", "redis:7.2.4"),
        ("actions/trivy/action.yml", "ghcr.io/aquasecurity/trivy:0.71.0"),
    ]


@pytest.mark.parametrize(
    "text",
    ["", "- a\n- b\n", "name: x\n", "inputs: [a, b]\n"],
)
def test_find_refs_action_without_image_inputs(tmp_path: Path, text: str) -> None:
    (tmp_path / "action.yaml").write_text(text)
    assert images.find_refs(tmp_path, ["action.yaml"]) == []


# --- ImagesUpdater ------------------------------------------------------------


def test_images_updater_satisfies_protocol() -> None:
    updater = images.ImagesUpdater()
    assert isinstance(updater, Updater)
    assert updater.name == "images"


def test_apply_rewrites_exact_ref_and_collects_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    act = tmp_path / "action.yml"
    act.write_text('inputs:\n  trivy-image:\n    default: "ghcr.io/aquasecurity/trivy:0.71.0"\n')
    calls = _ls_files(monkeypatch, "action.yml")
    tags = {"aquasecurity/trivy": ["0.71.0", "0.74.0", "1.0.0"]}
    result = images.ImagesUpdater(list_tags=lambda ref: tags[ref.repository]).apply(_ctx(tmp_path))
    assert result.changed is True
    assert "trivy:0.74.0" in act.read_text()
    assert result.summary == (
        "ghcr.io/aquasecurity/trivy:0.71.0 → ghcr.io/aquasecurity/trivy:0.74.0"
    )
    assert [e.latest for e in result.held_back] == ["1.0.0"]
    assert result.commit_message == "chore(deps): bump image tags"
    assert calls == [("-C", str(tmp_path), "ls-files")]


def test_apply_rewrites_only_the_exact_ref(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM tool:1.0.0 AS a\nFROM my/tool:1.0.0 AS b\nFROM tool:1.0.0 AS c\n")
    _ls_files(monkeypatch, "Dockerfile")
    tags = {"library/tool": ["1.0.0", "1.1.0"], "my/tool": ["1.0.0"]}
    looked_up: list[str] = []

    def list_tags(ref: ImageRef) -> list[str]:
        looked_up.append(ref.repository)
        return tags[ref.repository]

    result = images.ImagesUpdater(list_tags=list_tags).apply(_ctx(tmp_path))
    assert dockerfile.read_text() == (
        "FROM tool:1.1.0 AS a\nFROM my/tool:1.0.0 AS b\nFROM tool:1.1.0 AS c\n"
    )
    assert looked_up == ["library/tool", "my/tool"]
    assert result.summary == "tool:1.0.0 → tool:1.1.0"


def test_apply_same_ref_in_two_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM a/b:1.0.0\n")
    (tmp_path / "Dockerfile.ci").write_text("FROM a/b:1.0.0\n")
    _ls_files(monkeypatch, "Dockerfile\nDockerfile.ci\n")
    result = images.ImagesUpdater(list_tags=lambda ref: ["1.0.0", "1.0.1", "2.0.0"]).apply(
        _ctx(tmp_path)
    )
    assert (tmp_path / "Dockerfile").read_text() == "FROM a/b:1.0.1\n"
    assert (tmp_path / "Dockerfile.ci").read_text() == "FROM a/b:1.0.1\n"
    assert result.summary == "a/b:1.0.0 → a/b:1.0.1"
    assert len(result.held_back) == 1


def test_apply_no_change_reports_series(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM python:3.14-slim\n")
    _ls_files(monkeypatch, "Dockerfile")
    result = images.ImagesUpdater(list_tags=lambda ref: ["3.14-slim", "3.15-slim"]).apply(
        _ctx(tmp_path, worktree=False)
    )
    assert result.changed is False
    assert result.summary == "image tags already newest within their major"
    assert [e.latest for e in result.held_back] == ["3.15-slim"]
    assert (tmp_path / "Dockerfile").read_text() == "FROM python:3.14-slim\n"


def test_apply_lookup_failure_is_loud(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM a/b:1.0.0\n")
    _ls_files(monkeypatch, "Dockerfile")

    def boom(ref: ImageRef) -> list[str]:
        raise UpdateDepsError(phase="update", command="x", message="Tag lookup failed")

    with pytest.raises(UpdateDepsError, match="Tag lookup failed"):
        images.ImagesUpdater(list_tags=boom).apply(_ctx(tmp_path))
    assert (tmp_path / "Dockerfile").read_text() == "FROM a/b:1.0.0\n"


def test_apply_fails_loud_when_ref_text_cannot_be_located(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A YAML escape parses to the ref, but the literal text is not in the file.
    act = tmp_path / "action.yml"
    original = 'inputs:\n  x-image:\n    default: "a/b:1.0\\x2E0"\n'
    act.write_text(original)
    _ls_files(monkeypatch, "action.yml")
    with pytest.raises(UpdateDepsError, match=r"Could not locate image ref 'a/b:1\.0\.0'"):
        images.ImagesUpdater(list_tags=lambda ref: ["1.0.0", "1.0.1"]).apply(_ctx(tmp_path))
    assert act.read_text() == original


def test_applies_when_any_scanned_file_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _ls_files(monkeypatch, "")
    assert images.ImagesUpdater().applies(_ctx(tmp_path, worktree=False)) is False
    assert calls == [("-C", str(tmp_path), "ls-files")]
    _ls_files(monkeypatch, "README.md\nDockerfile")
    assert images.ImagesUpdater().applies(_ctx(tmp_path)) is True
