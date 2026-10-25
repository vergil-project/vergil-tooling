from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.update_deps.context import UpdateDepsContext, UpdateDepsError
from vergil_tooling.lib.update_deps.report import HeldBack
from vergil_tooling.lib.update_deps.updaters.vergil_eco import (
    VergilUpdater,
    _declared_runtime,
    bump_runtime,
    format_version,
    normalize_claude_ref,
    normalize_refs,
    read_source_version,
    set_source_version,
)
from vergil_tooling.lib.update_deps.vergil_index import ServedRuntime

if TYPE_CHECKING:
    from pathlib import Path

_DRIFT = "    - uses: vergil-project/vergil-actions/actions/y@v2.0\n"
_RUNTIME_TOML = '[package.python]\n# comment\nruntime = "3.14.8"\n'
_SOURCE_TOML = '[dependencies]\nvergil = "v2.1"\n'
_SERIES_NOTE = "[package.python] runtime: a new series is a reviewed change, never applied"


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


def _served(patch: str, newer_series: str | None = None) -> ServedRuntime:
    return ServedRuntime(
        package="vergil-python3.14",
        patches={"deb noble/amd64": patch, "rpm el9/x86_64": patch},
        newer_series=newer_series,
    )


def test_bump_runtime_rewrites_to_the_served_patch(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_RUNTIME_TOML)
    new, held = bump_runtime(tmp_path, _served("3.14.9"))
    assert new == "3.14.9"
    assert held == []
    assert (tmp_path / "vergil.toml").read_text() == _RUNTIME_TOML.replace("3.14.8", "3.14.9")


def test_bump_runtime_already_served_is_noop(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_RUNTIME_TOML)
    assert bump_runtime(tmp_path, _served("3.14.8")) == (None, [])
    assert (tmp_path / "vergil.toml").read_text() == _RUNTIME_TOML


def test_bump_runtime_reports_newer_series_without_applying_it(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_RUNTIME_TOML)
    new, held = bump_runtime(tmp_path, _served("3.14.8", "3.15"))
    assert new is None
    assert held == [
        HeldBack(
            source="vergil",
            name="vergil-python",
            current="3.14.8",
            latest="3.15",
            note=_SERIES_NOTE,
        )
    ]
    assert (tmp_path / "vergil.toml").read_text() == _RUNTIME_TOML


def test_bump_runtime_patch_and_newer_series(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_RUNTIME_TOML)
    new, held = bump_runtime(tmp_path, _served("3.14.9", "3.15"))
    assert new == "3.14.9"
    assert [h.latest for h in held] == ["3.15"]
    assert 'runtime = "3.14.9"' in (tmp_path / "vergil.toml").read_text()


def test_bump_runtime_holds_back_while_indexes_disagree(tmp_path: Path) -> None:
    # Right after a vergil-python release the deb and rpm indexes briefly serve
    # different patches: no bump, and a visible held-back entry naming both.
    (tmp_path / "vergil.toml").write_text(_RUNTIME_TOML)
    served = ServedRuntime(
        package="vergil-python3.14",
        patches={
            "deb noble/amd64": "3.14.9",
            "deb noble/arm64": "3.14.9",
            "rpm el9/x86_64": "3.14.8",
        },
        newer_series="3.15",
    )
    new, held = bump_runtime(tmp_path, served)
    assert new is None
    assert held == [
        HeldBack(
            source="vergil",
            name="vergil-python",
            current="3.14.8",
            latest="3.15",
            note=_SERIES_NOTE,
        ),
        HeldBack(
            source="vergil",
            name="vergil-python3.14",
            current="3.14.8",
            latest="3.14.9",
            note=(
                "indexes disagree (3.14.9 on deb noble/amd64, deb noble/arm64; "
                "3.14.8 on rpm el9/x86_64); bump held until they agree"
            ),
        ),
    ]
    assert (tmp_path / "vergil.toml").read_text() == _RUNTIME_TOML


def test_bump_runtime_never_crosses_series(tmp_path: Path) -> None:
    # A 3.14 pin is never moved to a 3.15 patch, even if one were handed in.
    (tmp_path / "vergil.toml").write_text(_RUNTIME_TOML)
    with pytest.raises(UpdateDepsError, match="series 3.14") as info:
        bump_runtime(tmp_path, _served("3.15.1"))
    assert "3.15.1" in str(info.value)
    assert (tmp_path / "vergil.toml").read_text() == _RUNTIME_TOML


def test_bump_runtime_cross_series_fails_loud_even_while_disagreeing(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_RUNTIME_TOML)
    served = ServedRuntime(
        package="vergil-python3.14",
        patches={"deb noble/amd64": "3.14.9", "rpm el9/x86_64": "3.15.1"},
        newer_series=None,
    )
    with pytest.raises(UpdateDepsError, match="series 3.14"):
        bump_runtime(tmp_path, served)
    assert (tmp_path / "vergil.toml").read_text() == _RUNTIME_TOML


def test_bump_runtime_never_downgrades(tmp_path: Path) -> None:
    text = _RUNTIME_TOML.replace("3.14.8", "3.14.10")
    (tmp_path / "vergil.toml").write_text(text)
    with pytest.raises(UpdateDepsError, match="older") as info:
        bump_runtime(tmp_path, _served("3.14.9"))
    assert "3.14.10" in str(info.value)
    assert (tmp_path / "vergil.toml").read_text() == text


def test_bump_runtime_absent_is_noop(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_SOURCE_TOML)
    assert bump_runtime(tmp_path, _served("3.14.9")) == (None, [])


def test_bump_runtime_unrewritable_line_fails_loud(tmp_path: Path) -> None:
    # Valid TOML, but not the `runtime = "<v>"` line shape the rewrite targets.
    text = "[package.python]\nruntime = '3.14.8'\n"
    (tmp_path / "vergil.toml").write_text(text)
    with pytest.raises(UpdateDepsError, match="found 0"):
        bump_runtime(tmp_path, _served("3.14.9"))
    assert (tmp_path / "vergil.toml").read_text() == text


def test_apply_skips_index_when_no_runtime(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_SOURCE_TOML)

    def must_not_call(base: Path) -> ServedRuntime:
        raise AssertionError("index lookup without [package.python] runtime")

    result = VergilUpdater(served_runtime=must_not_call).apply(_ctx(tmp_path))
    assert result.changed is False
    assert result.held_back == []


def test_apply_normalize_combines_runtime_bump(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_SOURCE_TOML + "\n" + _RUNTIME_TOML)
    seen: list[Path] = []

    def served(base: Path) -> ServedRuntime:
        seen.append(base)
        return _served("3.14.9")

    result = VergilUpdater(served_runtime=served).apply(_ctx(tmp_path))
    assert seen == [tmp_path]
    assert result.changed is True
    assert result.summary == "vergil-python runtime 3.14.9"
    assert result.commit_message == "chore(deps): vergil-python runtime 3.14.9"
    assert 'runtime = "3.14.9"' in (tmp_path / "vergil.toml").read_text()


def test_apply_normalize_refs_and_runtime_with_held_back(tmp_path: Path) -> None:
    _seed(tmp_path, "v2.0")
    with (tmp_path / "vergil.toml").open("a") as fh:
        fh.write("\n" + _RUNTIME_TOML)
    result = VergilUpdater(served_runtime=lambda _b: _served("3.14.9", "3.15")).apply(
        _ctx(tmp_path)
    )
    assert result.commit_message == (
        "chore(deps): normalize vergil refs to v2.1; vergil-python runtime 3.14.9"
    )
    assert [h.latest for h in result.held_back] == ["3.15"]


def test_apply_normalize_reports_held_back_without_change(tmp_path: Path) -> None:
    _seed(tmp_path, "v2.1")
    with (tmp_path / "vergil.toml").open("a") as fh:
        fh.write("\n" + _RUNTIME_TOML)
    result = VergilUpdater(served_runtime=lambda _b: _served("3.14.8", "3.15")).apply(
        _ctx(tmp_path)
    )
    assert result.changed is False
    assert result.summary == "vergil refs already at v2.1"
    assert [h.latest for h in result.held_back] == ["3.15"]


def test_apply_bump_combines_runtime_bump(tmp_path: Path) -> None:
    _seed(tmp_path, "v2.1")
    with (tmp_path / "vergil.toml").open("a") as fh:
        fh.write("\n" + _RUNTIME_TOML)
    ctx = _ctx(tmp_path)
    ctx.vergil_bump = "2.2"
    result = VergilUpdater(served_runtime=lambda _b: _served("3.14.9")).apply(ctx)
    assert result.changed is True
    assert result.commit_message == "chore(deps): bump vergil to v2.2; vergil-python runtime 3.14.9"


def test_apply_bump_runtime_only_change(tmp_path: Path) -> None:
    # The vergil version is already the bump target; only the runtime moves.
    (tmp_path / "vergil.toml").write_text(_SOURCE_TOML + "\n" + _RUNTIME_TOML)
    ctx = _ctx(tmp_path)
    ctx.vergil_bump = "2.1"
    result = VergilUpdater(served_runtime=lambda _b: _served("3.14.9", "3.15")).apply(ctx)
    assert result.changed is True
    assert [h.latest for h in result.held_back] == ["3.15"]


def test_apply_runtime_lookup_failure_propagates(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_SOURCE_TOML + "\n" + _RUNTIME_TOML)

    def served(base: Path) -> ServedRuntime:
        raise UpdateDepsError(phase="update", command="x", message="index down")

    with pytest.raises(UpdateDepsError, match="index down"):
        VergilUpdater(served_runtime=served).apply(_ctx(tmp_path))
    assert (tmp_path / "vergil.toml").read_text() == _SOURCE_TOML + "\n" + _RUNTIME_TOML


def test_apply_end_to_end_bumps_pin_from_the_series_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The default lookup reads the real vergil.toml and the (faked) index: a
    # 3.14.8 pin, with vergil-python3.14 serving 3.14.9 and a 3.15 series also
    # published, is bumped to 3.14.9 and the 3.15 series is only reported.
    toml = (
        '[project]\nrepository-type = "library"\nversioning-scheme = "semver"\n'
        'branching-model = "library-release"\nrelease-model = "tagged-release"\n'
        'primary-language = "python"\n\n'
        '[dependencies]\nvergil = "v2.1"\n\n[ci]\nversions = ["3.14"]\n\n'
        '[package]\nbuilder = "python"\n'
        'vendor = "vergil"\nsummary = "s"\nsmoke = "x --version"\n\n'
        '[package.python]\nruntime = "3.14.8"\n'
    )
    (tmp_path / "vergil.toml").write_text(toml)
    (tmp_path / "uv.lock").write_text("")
    deb = (
        b"Package: vergil-python3.14\nVersion: 3.14.9+20261020-1\n\n"
        b"Package: vergil-python3.14.8\nVersion: 3.14.8+20261003-1\n\n"
        b"Package: vergil-python3.15\nVersion: 3.15.1+20261021-1\n"
    )
    monkeypatch.setattr(
        "vergil_tooling.lib.update_deps.vergil_index.runtime_indexes",
        lambda pkg: [("deb", "noble", "amd64")],
    )
    monkeypatch.setattr(
        "vergil_tooling.lib.update_deps.vergil_index.served_runtime.__kwdefaults__",
        {"fetch": lambda url: deb},
    )
    result = VergilUpdater().apply(_ctx(tmp_path))
    assert result.changed is True
    assert result.summary == "vergil-python runtime 3.14.9"
    assert [(h.current, h.latest) for h in result.held_back] == [("3.14.8", "3.15")]
    assert 'runtime = "3.14.9"' in (tmp_path / "vergil.toml").read_text()


def test_apply_holds_the_bump_while_indexes_disagree(tmp_path: Path) -> None:
    # Not a failure (which would file a fix task): no change, and a visible
    # held-back entry until the deb and rpm indexes serve the same patch.
    (tmp_path / "vergil.toml").write_text(_SOURCE_TOML + "\n" + _RUNTIME_TOML)
    served = ServedRuntime(
        package="vergil-python3.14",
        patches={"deb noble/amd64": "3.14.9", "rpm el9/x86_64": "3.14.8"},
        newer_series=None,
    )
    result = VergilUpdater(served_runtime=lambda _b: served).apply(_ctx(tmp_path))
    assert result.changed is False
    assert [(h.name, h.current, h.latest) for h in result.held_back] == [
        ("vergil-python3.14", "3.14.8", "3.14.9")
    ]
    note = result.held_back[0].note
    assert note is not None
    assert "3.14.9 on deb noble/amd64; 3.14.8 on rpm el9/x86_64" in note
    assert (tmp_path / "vergil.toml").read_text() == _SOURCE_TOML + "\n" + _RUNTIME_TOML


# -- runtime pins over the packages list (epic vergil-project/.github#472, T5) --

_STAGED_ENTRY = (
    '[[package]]\nname = "{name}"\nbuilder = "staged"\nvendor = "vergil"\n'
    'summary = "s"\nsmoke = "true"\n\n[package.staged]\nbuild-command = "true"\n\n'
)
_PYTHON_ENTRY = (
    '[[package]]\nname = "{name}"\nbuilder = "python"\nvendor = "vergil"\n'
    'summary = "s"\nsmoke = "true"\n\n'
)
_PIN = '[package.python]\nruntime = "{runtime}"\n\n'


def _must_not_call(base: Path) -> ServedRuntime:
    raise AssertionError("index lookup without a [package.python] runtime pin")


def test_declared_runtime_reads_package_list() -> None:
    text = (
        _STAGED_ENTRY.format(name="a")
        + _PYTHON_ENTRY.format(name="b")
        + _PIN.format(runtime="3.14.8")
        + _PYTHON_ENTRY.format(name="c")
    )
    assert _declared_runtime(text) == "3.14.8"


def test_declared_runtime_single_table_unchanged() -> None:
    assert _declared_runtime(_RUNTIME_TOML) == "3.14.8"
    assert _declared_runtime(_SOURCE_TOML) is None


def test_declared_runtime_ignores_non_table_entries() -> None:
    # Not a valid [package] (lib.config refuses it); there is simply no pin here.
    assert _declared_runtime('package = ["x"]\n') is None
    assert _declared_runtime('package = "x"\n') is None


def test_staged_only_multi_has_no_runtime(tmp_path: Path) -> None:
    text = _SOURCE_TOML + "\n" + _STAGED_ENTRY.format(name="a") + _STAGED_ENTRY.format(name="b")
    (tmp_path / "vergil.toml").write_text(text)
    result = VergilUpdater(served_runtime=_must_not_call).apply(_ctx(tmp_path))
    assert result.changed is False
    assert result.held_back == []
    assert bump_runtime(tmp_path, _served("3.14.9")) == (None, [])
    assert (tmp_path / "vergil.toml").read_text() == text


def test_single_python_pin_unchanged(tmp_path: Path) -> None:
    # The pinned single-[package] fixture bumps exactly as before.
    (tmp_path / "vergil.toml").write_text(_SOURCE_TOML + "\n" + _RUNTIME_TOML)
    result = VergilUpdater(served_runtime=lambda _b: _served("3.14.9")).apply(_ctx(tmp_path))
    assert result.summary == "vergil-python runtime 3.14.9"
    assert (tmp_path / "vergil.toml").read_text() == (
        _SOURCE_TOML + "\n" + _RUNTIME_TOML.replace("3.14.8", "3.14.9")
    )


def test_bump_runtime_rewrites_the_one_pin_in_a_list(tmp_path: Path) -> None:
    text = (
        _STAGED_ENTRY.format(name="a")
        + _PYTHON_ENTRY.format(name="b")
        + _PIN.format(runtime="3.14.8")
    )
    (tmp_path / "vergil.toml").write_text(text)
    assert bump_runtime(tmp_path, _served("3.14.9")) == ("3.14.9", [])
    assert (tmp_path / "vergil.toml").read_text() == text.replace("3.14.8", "3.14.9")


def test_two_pins_refused(tmp_path: Path) -> None:
    text = (
        _SOURCE_TOML
        + "\n"
        + _PYTHON_ENTRY.format(name="a")
        + _PIN.format(runtime="3.14.8")
        + _PYTHON_ENTRY.format(name="b")
        + _PIN.format(runtime="3.15.1")
    )
    (tmp_path / "vergil.toml").write_text(text)
    msg = r"several \[\[package\]\] entries pin a runtime; pin it on one entry"
    with pytest.raises(UpdateDepsError, match=msg):
        VergilUpdater(served_runtime=_must_not_call).apply(_ctx(tmp_path))
    with pytest.raises(UpdateDepsError, match=msg):
        bump_runtime(tmp_path, _served("3.14.9"))
    assert (tmp_path / "vergil.toml").read_text() == text
