from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.update_deps.context import UpdateDepsContext, UpdateDepsError
from vergil_tooling.lib.update_deps.updaters.vergil_eco import (
    VergilUpdater,
    format_version,
    normalize_claude_ref,
    normalize_refs,
    read_source_version,
    set_source_version,
)

if TYPE_CHECKING:
    from pathlib import Path


def _ctx(worktree: Path) -> UpdateDepsContext:
    return UpdateDepsContext(repo="o/r", repo_root=worktree, worktree_path=worktree)


def _seed(worktree: Path, ci_version: str) -> None:
    (worktree / "vergil.toml").write_text('[dependencies]\nvergil = "v2.1"\n')
    wf = worktree / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(
        "jobs:\n"
        "  a:\n"
        f"    uses: vergil-project/vergil-actions/.github/workflows/ci.yml@{ci_version}\n"
    )


def test_format_version_normalizes() -> None:
    assert format_version("2.2") == "v2.2"
    assert format_version("v2.3") == "v2.3"
    assert format_version(" 2.4 ") == "v2.4"


def test_format_version_rejects_invalid() -> None:
    with pytest.raises(UpdateDepsError, match="invalid vergil version"):
        format_version("2")


def test_read_source_version(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text('[dependencies]\nvergil = "v2.1"\n')
    assert read_source_version(tmp_path) == "v2.1"


def test_read_source_version_missing_raises(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text("[dependencies]\n")
    with pytest.raises(UpdateDepsError, match="dependencies..vergil"):
        read_source_version(tmp_path)


def test_set_source_version_rewrites_and_is_idempotent(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text('[dependencies]\nvergil = "v2.1"\n')
    assert set_source_version(tmp_path, "v2.2") is True
    assert 'vergil = "v2.2"' in (tmp_path / "vergil.toml").read_text()
    assert set_source_version(tmp_path, "v2.2") is False


def test_normalize_refs_rewrites_drifting_only(tmp_path: Path) -> None:
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(
        "jobs:\n"
        "  a:\n"
        "    uses: vergil-project/vergil-actions/.github/workflows/ci.yml@v2.0\n"
        "  b:\n"
        "    uses: vergil-project/vergil-actions/.github/workflows/cd.yml@v2.1\n"
        "  c:\n"
        "    uses: actions/checkout@v4\n"
    )
    changed = normalize_refs(tmp_path, "v2.1")
    assert changed == [wf / "ci.yml"]
    text = (wf / "ci.yml").read_text()
    assert "@v2.0" not in text
    assert text.count("@v2.1") == 2
    assert "actions/checkout@v4" in text  # third-party untouched
    assert normalize_refs(tmp_path, "v2.1") == []  # idempotent


def test_normalize_refs_no_workflows_dir(tmp_path: Path) -> None:
    assert normalize_refs(tmp_path, "v2.1") == []


def test_updater_applies(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text('[dependencies]\nvergil = "v2.1"\n')
    assert VergilUpdater().applies(_ctx(tmp_path)) is True


def test_updater_applies_false_without_config(tmp_path: Path) -> None:
    assert VergilUpdater().applies(_ctx(tmp_path)) is False


def test_apply_normalize_rewrites_drift(tmp_path: Path) -> None:
    _seed(tmp_path, "v2.0")
    result = VergilUpdater().apply(_ctx(tmp_path))
    assert result.changed is True
    assert result.commit_message == "chore(deps): normalize vergil ecosystem refs (v2.1)"
    assert "@v2.1" in (tmp_path / ".github" / "workflows" / "ci.yml").read_text()


def test_apply_normalize_noop_when_aligned(tmp_path: Path) -> None:
    _seed(tmp_path, "v2.1")
    result = VergilUpdater().apply(_ctx(tmp_path))
    assert result.changed is False


def test_apply_bump_rewrites_source_and_refs(tmp_path: Path) -> None:
    _seed(tmp_path, "v2.1")
    ctx = _ctx(tmp_path)
    ctx.vergil_bump = "2.2"
    result = VergilUpdater().apply(ctx)
    assert result.changed is True
    assert result.commit_message == "chore(deps): bump vergil to v2.2"
    assert 'vergil = "v2.2"' in (tmp_path / "vergil.toml").read_text()
    assert "@v2.2" in (tmp_path / ".github" / "workflows" / "ci.yml").read_text()


def _seed_settings(base: Path, ref: str | None = None) -> None:
    src: dict[str, str] = {
        "source": "github",
        "repo": "vergil-project/vergil-claude-plugin",
    }
    if ref is not None:
        src["ref"] = ref
    settings = {
        "permissions": {"allow": ["Bash(vrg-*)"]},
        "extraKnownMarketplaces": {"vergil-marketplace": {"source": src}},
        "enabledPlugins": {"vergil@vergil-marketplace": True},
    }
    claude = base / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    (claude / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")


def test_normalize_claude_ref_inserts_missing_ref(tmp_path: Path) -> None:
    _seed_settings(tmp_path, ref=None)
    changed = normalize_claude_ref(tmp_path, "v2.0")
    assert changed == tmp_path / ".claude" / "settings.json"
    data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    src = data["extraKnownMarketplaces"]["vergil-marketplace"]["source"]
    assert src["ref"] == "v2.0"
    assert data["enabledPlugins"] == {"vergil@vergil-marketplace": True}
    assert src["repo"] == "vergil-project/vergil-claude-plugin"


def test_normalize_claude_ref_rewrites_drifted_ref(tmp_path: Path) -> None:
    _seed_settings(tmp_path, ref="develop")
    changed = normalize_claude_ref(tmp_path, "v2.1")
    assert changed is not None
    data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert data["extraKnownMarketplaces"]["vergil-marketplace"]["source"]["ref"] == "v2.1"


def test_normalize_claude_ref_idempotent(tmp_path: Path) -> None:
    _seed_settings(tmp_path, ref="v2.0")
    assert normalize_claude_ref(tmp_path, "v2.0") is None


def test_normalize_claude_ref_no_settings_file(tmp_path: Path) -> None:
    assert normalize_claude_ref(tmp_path, "v2.0") is None


def _mark_self_repo(base: Path) -> None:
    (base / ".claude-plugin").mkdir()
    (base / ".claude-plugin" / "marketplace.json").write_text("{}")


def test_apply_normalize_sets_consumer_ref(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text('[dependencies]\nvergil = "v2.0"\n')
    _seed_settings(tmp_path, ref=None)
    result = VergilUpdater().apply(_ctx(tmp_path))
    assert result.changed is True
    data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert data["extraKnownMarketplaces"]["vergil-marketplace"]["source"]["ref"] == "v2.0"


def test_apply_normalize_self_repo_uses_develop(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text('[dependencies]\nvergil = "v2.1"\n')
    _mark_self_repo(tmp_path)
    _seed_settings(tmp_path, ref=None)
    VergilUpdater().apply(_ctx(tmp_path))
    data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert (
        data["extraKnownMarketplaces"]["vergil-marketplace"]["source"]["ref"] == "develop"
    )


def test_apply_bump_sets_ref_to_bumped_version(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text('[dependencies]\nvergil = "v2.0"\n')
    _seed_settings(tmp_path, ref="v2.0")
    ctx = _ctx(tmp_path)
    ctx.vergil_bump = "2.1"
    VergilUpdater().apply(ctx)
    data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert data["extraKnownMarketplaces"]["vergil-marketplace"]["source"]["ref"] == "v2.1"
