from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.update_deps.context import UpdateDepsContext, UpdateDepsError
from vergil_tooling.lib.update_deps.report import HeldBack
from vergil_tooling.lib.update_deps.updaters.vergil_eco import (
    VergilUpdater,
    bump_runtime,
    format_version,
    normalize_claude_ref,
    normalize_refs,
    read_source_version,
    set_source_version,
)

if TYPE_CHECKING:
    from pathlib import Path

_DRIFT = "    - uses: vergil-project/vergil-actions/actions/y@v2.0\n"
_RUNTIME_TOML = '[package.python]\n# comment\nruntime = "3.14.8"\n'
_SOURCE_TOML = '[dependencies]\nvergil = "v2.1"\n'
_MINOR_NOTE = "[package.python] runtime: a new minor is a judgement call"


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
    assert result.commit_message == "chore(deps): normalize vergil refs to v2.1"
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


def test_normalize_claude_ref_missing_marketplace_entry(tmp_path: Path) -> None:
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(json.dumps({"extraKnownMarketplaces": {}}))
    assert normalize_claude_ref(tmp_path, "v2.0") is None


def test_normalize_claude_ref_non_dict_source(tmp_path: Path) -> None:
    claude = tmp_path / ".claude"
    claude.mkdir()
    settings = {"extraKnownMarketplaces": {"vergil-marketplace": {"source": "bad"}}}
    (claude / "settings.json").write_text(json.dumps(settings))
    assert normalize_claude_ref(tmp_path, "v2.0") is None


def _mark_self_repo(base: Path) -> None:
    (base / ".claude-plugin").mkdir()
    (base / ".claude-plugin" / "marketplace.json").write_text("{}")


def test_apply_normalize_sets_marketplace_ref_to_main(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text('[dependencies]\nvergil = "v2.0"\n')
    _seed_settings(tmp_path, ref=None)
    result = VergilUpdater().apply(_ctx(tmp_path))
    assert result.changed is True
    data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert data["extraKnownMarketplaces"]["vergil-marketplace"]["source"]["ref"] == "main"


def test_apply_normalize_self_repo_also_uses_main(tmp_path: Path) -> None:
    # Single-channel model (#1974): no source-repo → develop exemption anymore.
    (tmp_path / "vergil.toml").write_text('[dependencies]\nvergil = "v2.1"\n')
    _mark_self_repo(tmp_path)
    _seed_settings(tmp_path, ref=None)
    VergilUpdater().apply(_ctx(tmp_path))
    data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert data["extraKnownMarketplaces"]["vergil-marketplace"]["source"]["ref"] == "main"


def test_apply_bump_keeps_marketplace_ref_at_main(tmp_path: Path) -> None:
    # Workflow/source refs follow the version; the marketplace ref does not.
    (tmp_path / "vergil.toml").write_text('[dependencies]\nvergil = "v2.0"\n')
    _seed_settings(tmp_path, ref="v2.0")
    ctx = _ctx(tmp_path)
    ctx.vergil_bump = "2.1"
    VergilUpdater().apply(ctx)
    data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert data["extraKnownMarketplaces"]["vergil-marketplace"]["source"]["ref"] == "main"


# -- action.yml refs ----------------------------------------------------------


def test_normalize_refs_covers_action_files(tmp_path: Path) -> None:
    action = tmp_path / "actions" / "ci" / "x" / "action.yml"
    action.parent.mkdir(parents=True)
    action.write_text("runs:\n  steps:\n" + _DRIFT)
    root_action = tmp_path / "action.yaml"
    root_action.write_text(_DRIFT)
    aligned = tmp_path / "actions" / "ok" / "action.yml"
    aligned.parent.mkdir(parents=True)
    aligned.write_text(_DRIFT.replace("@v2.0", "@v2.1"))
    other = tmp_path / "actions" / "ci" / "x" / "other.yml"
    other.write_text(_DRIFT)
    skipped = [tmp_path / d / "lib" / "action.yml" for d in (".venv", ".worktrees", ".git")]
    for path in skipped:
        path.parent.mkdir(parents=True)
        path.write_text(_DRIFT)
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(_DRIFT)

    changed = normalize_refs(tmp_path, "v2.1")

    assert changed == [wf / "ci.yml", root_action, action]
    assert "@v2.1" in action.read_text()
    assert "@v2.1" in root_action.read_text()
    assert "@v2.0" in other.read_text()  # only action.yml/action.yaml are scanned
    for path in skipped:
        assert "@v2.0" in path.read_text()
    assert normalize_refs(tmp_path, "v2.1") == []  # idempotent


def test_apply_normalize_rewrites_action_file_without_workflows(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_SOURCE_TOML)
    action = tmp_path / "action.yml"
    action.write_text(_DRIFT)
    result = VergilUpdater().apply(_ctx(tmp_path))
    assert result.changed is True
    assert "@v2.1" in action.read_text()


# -- vergil-python runtime ----------------------------------------------------


def test_bump_runtime_rewrites_to_newest_served_patch(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_RUNTIME_TOML)
    new, entry = bump_runtime(tmp_path, {"3.14.8", "3.14.9", "3.14.10"})
    assert new == "3.14.10"
    assert entry is None
    assert (tmp_path / "vergil.toml").read_text() == _RUNTIME_TOML.replace("3.14.8", "3.14.10")


def test_bump_runtime_never_picks_unserved(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_RUNTIME_TOML)
    assert bump_runtime(tmp_path, {"3.14.8"}) == (None, None)
    assert (tmp_path / "vergil.toml").read_text() == _RUNTIME_TOML


def test_bump_runtime_reports_newer_minor_without_applying_it(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_RUNTIME_TOML)
    new, entry = bump_runtime(tmp_path, {"3.14.8", "3.15.1"})
    assert new is None
    assert entry == HeldBack(
        source="vergil",
        name="vergil-python",
        current="3.14.8",
        latest="3.15.1",
        note=_MINOR_NOTE,
    )
    assert (tmp_path / "vergil.toml").read_text() == _RUNTIME_TOML


def test_bump_runtime_patch_and_newer_minor(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_RUNTIME_TOML)
    new, entry = bump_runtime(tmp_path, {"3.14.9", "3.15.1"})
    assert new == "3.14.9"
    assert entry is not None
    assert entry.latest == "3.15.1"


def test_bump_runtime_absent_is_noop(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_SOURCE_TOML)
    assert bump_runtime(tmp_path, {"3.14.9"}) == (None, None)


def test_bump_runtime_unrewritable_line_fails_loud(tmp_path: Path) -> None:
    # Valid TOML, but not the `runtime = "<v>"` line shape the rewrite targets.
    text = "[package.python]\nruntime = '3.14.8'\n"
    (tmp_path / "vergil.toml").write_text(text)
    with pytest.raises(UpdateDepsError, match="found 0"):
        bump_runtime(tmp_path, {"3.14.9"})
    assert (tmp_path / "vergil.toml").read_text() == text


def test_apply_skips_index_when_no_runtime(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_SOURCE_TOML)

    def must_not_call(base: Path) -> set[str]:
        raise AssertionError("index lookup without [package.python] runtime")

    result = VergilUpdater(served_runtimes=must_not_call).apply(_ctx(tmp_path))
    assert result.changed is False
    assert result.held_back == []


def test_apply_normalize_combines_runtime_bump(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_SOURCE_TOML + "\n" + _RUNTIME_TOML)
    seen: list[Path] = []

    def served(base: Path) -> set[str]:
        seen.append(base)
        return {"3.14.9"}

    result = VergilUpdater(served_runtimes=served).apply(_ctx(tmp_path))
    assert seen == [tmp_path]
    assert result.changed is True
    assert result.summary == "vergil-python runtime 3.14.9"
    assert result.commit_message == "chore(deps): vergil-python runtime 3.14.9"
    assert 'runtime = "3.14.9"' in (tmp_path / "vergil.toml").read_text()


def test_apply_normalize_refs_and_runtime_with_held_back(tmp_path: Path) -> None:
    _seed(tmp_path, "v2.0")
    with (tmp_path / "vergil.toml").open("a") as fh:
        fh.write("\n" + _RUNTIME_TOML)
    result = VergilUpdater(served_runtimes=lambda _b: {"3.14.9", "3.15.0"}).apply(_ctx(tmp_path))
    assert result.commit_message == (
        "chore(deps): normalize vergil refs to v2.1; vergil-python runtime 3.14.9"
    )
    assert [h.latest for h in result.held_back] == ["3.15.0"]


def test_apply_normalize_reports_held_back_without_change(tmp_path: Path) -> None:
    _seed(tmp_path, "v2.1")
    with (tmp_path / "vergil.toml").open("a") as fh:
        fh.write("\n" + _RUNTIME_TOML)
    result = VergilUpdater(served_runtimes=lambda _b: {"3.14.8", "3.15.0"}).apply(_ctx(tmp_path))
    assert result.changed is False
    assert result.summary == "vergil refs already at v2.1"
    assert [h.latest for h in result.held_back] == ["3.15.0"]


def test_apply_bump_combines_runtime_bump(tmp_path: Path) -> None:
    _seed(tmp_path, "v2.1")
    with (tmp_path / "vergil.toml").open("a") as fh:
        fh.write("\n" + _RUNTIME_TOML)
    ctx = _ctx(tmp_path)
    ctx.vergil_bump = "2.2"
    result = VergilUpdater(served_runtimes=lambda _b: {"3.14.9"}).apply(ctx)
    assert result.changed is True
    assert result.commit_message == "chore(deps): bump vergil to v2.2; vergil-python runtime 3.14.9"


def test_apply_bump_runtime_only_change(tmp_path: Path) -> None:
    # The vergil version is already the bump target; only the runtime moves.
    (tmp_path / "vergil.toml").write_text(_SOURCE_TOML + "\n" + _RUNTIME_TOML)
    ctx = _ctx(tmp_path)
    ctx.vergil_bump = "2.1"
    result = VergilUpdater(served_runtimes=lambda _b: {"3.14.9", "3.15.0"}).apply(ctx)
    assert result.changed is True
    assert [h.latest for h in result.held_back] == ["3.15.0"]


def test_apply_runtime_lookup_failure_propagates(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_SOURCE_TOML + "\n" + _RUNTIME_TOML)

    def served(base: Path) -> set[str]:
        raise UpdateDepsError(phase="update", command="x", message="index down")

    with pytest.raises(UpdateDepsError, match="index down"):
        VergilUpdater(served_runtimes=served).apply(_ctx(tmp_path))
    assert (tmp_path / "vergil.toml").read_text() == _SOURCE_TOML + "\n" + _RUNTIME_TOML
