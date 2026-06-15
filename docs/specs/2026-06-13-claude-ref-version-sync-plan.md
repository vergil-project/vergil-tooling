# Version-Derived Claude Marketplace Ref Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Claude plugin marketplace `source.ref` a version-derived reference — written by `vrg-update-deps` and asserted by the `repo_config` audit — with the plugin repo exempt to `develop`.

**Architecture:** Introduce a neutral `lib/vergil_refs.py` that owns version derivation (canonical `vergil.toml` → expected refs) and the read helpers both sides need. The `update_deps` updater (`vergil_eco`) gains a JSON-aware writer that sets the marketplace ref alongside the existing workflow-pin rewrite. The `repo_config` audit consumes the same derivation to assert every derived ref matches. `repo_init` seeds the ref at scaffold time.

**Tech Stack:** Python 3.12, `tomllib`, `json`, `re`, `pytest`, `uv`. House tooling: `vrg-git`, `vrg-commit`.

**Conventions for every task:**
- Run a single test: `uv run pytest <path>::<test> -v`
- Commit: `vrg-commit --type <type> --scope <scope> --message "<msg>"` (stage with `vrg-git add <paths>` first). Never raw `git commit`.
- Work inside the worktree: `.worktrees/issue-1654-claude-ref-version-sync/`.

**Reference — the canonical exempt signal:** a repo is the marketplace source repo iff `.claude-plugin/marketplace.json` exists at its root. Only `vergil-claude-plugin` ships that file.

---

## File Structure

- **Create** `src/vergil_tooling/lib/vergil_refs.py` — pure derivation + read helpers (shared).
- **Create** `tests/vergil_tooling/test_vergil_refs.py` — unit tests for the helpers.
- **Modify** `src/vergil_tooling/lib/update_deps/updaters/vergil_eco.py` — import shared helpers; add `normalize_claude_ref`; wire into `VergilUpdater.apply`.
- **Modify** `tests/vergil_tooling/test_update_deps_vergil_eco.py` — writer + updater tests.
- **Modify** `src/vergil_tooling/lib/repo_config.py` — marketplace-ref check + workflow-pin check.
- **Modify** `tests/vergil_tooling/test_repo_config.py` — audit tests.
- **Modify** `src/vergil_tooling/lib/repo_init.py` — seed the ref from `ctx.vergil_version`.
- **Modify** `tests/vergil_tooling/test_repo_init.py` (or create if absent) — seeding test.

---

## Task 1: Shared derivation module `vergil_refs.py`

**Files:**
- Create: `src/vergil_tooling/lib/vergil_refs.py`
- Test: `tests/vergil_tooling/test_vergil_refs.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/vergil_tooling/test_vergil_refs.py
from __future__ import annotations

from pathlib import Path

import pytest

from vergil_tooling.lib.update_deps.context import UpdateDepsError
from vergil_tooling.lib.vergil_refs import (
    MARKETPLACE_NAME,
    expected_claude_ref,
    format_version,
    is_marketplace_source_repo,
    iter_workflow_refs,
    read_source_version,
)


def _seed_toml(base: Path, version: str) -> None:
    (base / "vergil.toml").write_text(f'[dependencies]\nvergil = "{version}"\n')


def test_marketplace_name_constant() -> None:
    assert MARKETPLACE_NAME == "vergil-marketplace"


def test_format_version_normalizes() -> None:
    assert format_version("2.2") == "v2.2"
    assert format_version("v2.3") == "v2.3"


def test_format_version_rejects_invalid() -> None:
    with pytest.raises(UpdateDepsError, match="invalid vergil version"):
        format_version("2")


def test_read_source_version(tmp_path: Path) -> None:
    _seed_toml(tmp_path, "v2.1")
    assert read_source_version(tmp_path) == "v2.1"


def test_read_source_version_missing_key(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text("[project]\n")
    with pytest.raises(UpdateDepsError, match="not found"):
        read_source_version(tmp_path)


def test_is_marketplace_source_repo_true(tmp_path: Path) -> None:
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "marketplace.json").write_text("{}")
    assert is_marketplace_source_repo(tmp_path) is True


def test_is_marketplace_source_repo_false(tmp_path: Path) -> None:
    assert is_marketplace_source_repo(tmp_path) is False


def test_expected_claude_ref_consumer(tmp_path: Path) -> None:
    _seed_toml(tmp_path, "v2.0")
    assert expected_claude_ref(tmp_path) == "v2.0"


def test_expected_claude_ref_self_repo(tmp_path: Path) -> None:
    _seed_toml(tmp_path, "v2.1")
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "marketplace.json").write_text("{}")
    assert expected_claude_ref(tmp_path) == "develop"


def test_iter_workflow_refs(tmp_path: Path) -> None:
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(
        "jobs:\n"
        "  a:\n"
        "    uses: vergil-project/vergil-actions/.github/workflows/ci.yml@v2.0\n"
        "  b:\n"
        "    uses: actions/checkout@v6\n"
    )
    refs = list(iter_workflow_refs(tmp_path))
    assert refs == [(wf / "ci.yml", "v2.0")]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/vergil_tooling/test_vergil_refs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vergil_tooling.lib.vergil_refs'`

- [ ] **Step 3: Create the module**

```python
# src/vergil_tooling/lib/vergil_refs.py
"""Shared derivation of vergil-ecosystem version references.

The source of truth is ``[dependencies].vergil`` in ``vergil.toml``. Every
derived reference — reusable-workflow pins and the Claude plugin marketplace
ref — must equal the version computed from it. The marketplace source repo
(the plugin repo itself, identified by ``.claude-plugin/marketplace.json``)
is exempt: its marketplace ref is ``develop`` so plugin development dogfoods
the latest in-progress plugin.

This module holds the pure derivation and read helpers shared by the
update_deps writer (``vergil_eco``) and the repo_config auditor, so the two
can never disagree about what a ref should be. It imports only the lightweight
``UpdateDepsError`` type for back-compatible error semantics.
"""

from __future__ import annotations

import re
import tomllib
from typing import TYPE_CHECKING

from vergil_tooling.lib.update_deps.context import UpdateDepsError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

#: A vergil-internal reusable-workflow ref: owner starts with ``vergil-``,
#: pinned to a ``vX.Y`` tag. Group 1 is the prefix through ``@``; group 2 is
#: the version. Third-party actions (actions/..., docker/...) do not match.
_REF_RE = re.compile(r"(uses:\s*vergil-[\w.-]+/[^@\s]+@)(v\d+\.\d+)")

#: The ``vergil = "..."`` line in vergil.toml's [dependencies] table.
_SOURCE_RE = re.compile(r'(?m)^(vergil\s*=\s*)"[^"]*"')

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)$")

#: The marketplace name keyed under ``extraKnownMarketplaces``.
MARKETPLACE_NAME = "vergil-marketplace"


def format_version(raw: str) -> str:
    """Normalize a user-supplied version (``2.2`` or ``v2.2``) to ``vX.Y``."""
    match = _VERSION_RE.match(raw.strip())
    if match is None:
        raise UpdateDepsError(
            phase="vergil",
            command="format_version",
            message=f"invalid vergil version '{raw}' (expected X.Y, e.g. 2.2).",
        )
    return f"v{match.group(1)}.{match.group(2)}"


def read_source_version(base: Path) -> str:
    """Return ``[dependencies].vergil`` (the source of truth) from vergil.toml."""
    with (base / "vergil.toml").open("rb") as handle:
        raw = tomllib.load(handle)
    try:
        value: str = raw["dependencies"]["vergil"]
    except KeyError as exc:
        raise UpdateDepsError(
            phase="vergil",
            command="read_source_version",
            message="vergil.toml [dependencies].vergil not found.",
        ) from exc
    return value


def is_marketplace_source_repo(base: Path) -> bool:
    """True if *base* is the plugin/marketplace source repo (exempt → develop)."""
    return (base / ".claude-plugin" / "marketplace.json").is_file()


def expected_claude_ref(base: Path) -> str:
    """The marketplace ref *base* should carry: ``develop`` for the source repo,
    else the derived version from vergil.toml."""
    if is_marketplace_source_repo(base):
        return "develop"
    return read_source_version(base)


def iter_workflow_refs(base: Path) -> Iterator[tuple[Path, str]]:
    """Yield ``(workflow_file, ref_version)`` for each vergil-* reusable-workflow
    pin under ``.github/workflows``."""
    workflows = base / ".github" / "workflows"
    if not workflows.is_dir():
        return
    for path in sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")]):
        text = path.read_text(encoding="utf-8")
        for match in _REF_RE.finditer(text):
            yield path, match.group(2)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/vergil_tooling/test_vergil_refs.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/vergil_refs.py tests/vergil_tooling/test_vergil_refs.py
vrg-commit --type feat --scope deps --message "add shared vergil ref derivation helpers (#1654)"
```

---

## Task 2: Re-point `vergil_eco` onto the shared helpers (no behavior change)

**Files:**
- Modify: `src/vergil_tooling/lib/update_deps/updaters/vergil_eco.py`

This keeps `format_version`, `read_source_version`, `set_source_version`, `normalize_refs`, and `VergilUpdater` importable from `vergil_eco` (existing tests import them) while the derivation lives in `vergil_refs`.

- [ ] **Step 1: Run the existing eco tests to establish green baseline**

Run: `uv run pytest tests/vergil_tooling/test_update_deps_vergil_eco.py -v`
Expected: PASS (baseline before refactor)

- [ ] **Step 2: Replace the duplicated derivation with imports**

Replace the top of `vergil_eco.py` (the module docstring through `_VERSION_RE`, and the `format_version` / `read_source_version` defs) so it imports them instead. The file becomes:

```python
"""Vergil ecosystem updater: normalize internal refs, optionally bump the version.

The source of truth is ``[dependencies].vergil`` in ``vergil.toml`` (see
``vergil_tooling.lib.vergil_refs``). Every secondary reference — workflow
``uses: vergil-*/...@vX.Y`` and the Claude marketplace ref — must match it.
``normalize`` rewrites drifting refs to the source-of-truth version; ``bump``
first rewrites the source of truth, then normalizes.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from vergil_tooling.lib.update_deps.updater import UpdateResult
from vergil_tooling.lib.vergil_refs import (
    _REF_RE,
    _SOURCE_RE,
    MARKETPLACE_NAME,
    expected_claude_ref,
    format_version,
    is_marketplace_source_repo,
    read_source_version,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vergil_tooling.lib.update_deps.context import UpdateDepsContext
```

Keep `set_source_version` and `normalize_refs` exactly as they are (they already use `_SOURCE_RE` / `_REF_RE`, now imported). Note `normalize_refs`'s substitution `m.group(1) + target` is unchanged-correct: group 1 is still the prefix through `@` even though `_REF_RE` now also captures the version in group 2.

- [ ] **Step 3: Run the eco tests to verify no regression**

Run: `uv run pytest tests/vergil_tooling/test_update_deps_vergil_eco.py -v`
Expected: PASS (same as baseline)

- [ ] **Step 4: Commit**

```bash
vrg-git add src/vergil_tooling/lib/update_deps/updaters/vergil_eco.py
vrg-commit --type refactor --scope deps --message "source vergil ref derivation from shared module (#1654)"
```

---

## Task 3: JSON-aware `normalize_claude_ref` writer

**Files:**
- Modify: `src/vergil_tooling/lib/update_deps/updaters/vergil_eco.py`
- Test: `tests/vergil_tooling/test_update_deps_vergil_eco.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/vergil_tooling/test_update_deps_vergil_eco.py`:

```python
import json

from vergil_tooling.lib.update_deps.updaters.vergil_eco import normalize_claude_ref


def _seed_settings(base, ref=None):
    src = {"source": "github", "repo": "vergil-project/vergil-claude-plugin"}
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


def test_normalize_claude_ref_inserts_missing_ref(tmp_path):
    _seed_settings(tmp_path, ref=None)
    changed = normalize_claude_ref(tmp_path, "v2.0")
    assert changed == tmp_path / ".claude" / "settings.json"
    data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    src = data["extraKnownMarketplaces"]["vergil-marketplace"]["source"]
    assert src["ref"] == "v2.0"
    # sibling keys preserved
    assert data["enabledPlugins"] == {"vergil@vergil-marketplace": True}
    assert src["repo"] == "vergil-project/vergil-claude-plugin"


def test_normalize_claude_ref_rewrites_drifted_ref(tmp_path):
    _seed_settings(tmp_path, ref="develop")
    changed = normalize_claude_ref(tmp_path, "v2.1")
    assert changed is not None
    data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert data["extraKnownMarketplaces"]["vergil-marketplace"]["source"]["ref"] == "v2.1"


def test_normalize_claude_ref_idempotent(tmp_path):
    _seed_settings(tmp_path, ref="v2.0")
    assert normalize_claude_ref(tmp_path, "v2.0") is None


def test_normalize_claude_ref_no_settings_file(tmp_path):
    assert normalize_claude_ref(tmp_path, "v2.0") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/vergil_tooling/test_update_deps_vergil_eco.py -k normalize_claude_ref -v`
Expected: FAIL — `cannot import name 'normalize_claude_ref'`

- [ ] **Step 3: Implement the writer**

Add to `vergil_eco.py` (after `normalize_refs`):

```python
def normalize_claude_ref(base: Path, target: str) -> Path | None:
    """Set the marketplace ``source.ref`` in ``.claude/settings.json`` to *target*.

    *target* is the derived ``vX.Y`` (or ``develop`` for the source repo). The
    file is edited structurally (parsed JSON, re-dumped at indent 2) because the
    ref may need to be *inserted* where none exists — a regex cannot do that
    safely. Returns the path if changed, else ``None``. Missing file or missing
    marketplace entry is a clean no-op.
    """
    settings_path = base / ".claude" / "settings.json"
    if not settings_path.is_file():
        return None
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    try:
        source = data["extraKnownMarketplaces"][MARKETPLACE_NAME]["source"]
    except (KeyError, TypeError):
        return None
    if not isinstance(source, dict) or source.get("ref") == target:
        return None
    source["ref"] = target
    settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return settings_path
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/vergil_tooling/test_update_deps_vergil_eco.py -k normalize_claude_ref -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/update_deps/updaters/vergil_eco.py tests/vergil_tooling/test_update_deps_vergil_eco.py
vrg-commit --type feat --scope deps --message "add JSON-aware claude marketplace ref writer (#1654)"
```

---

## Task 4: Wire the writer into `VergilUpdater.apply`

**Files:**
- Modify: `src/vergil_tooling/lib/update_deps/updaters/vergil_eco.py`
- Test: `tests/vergil_tooling/test_update_deps_vergil_eco.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
def test_apply_normalize_sets_consumer_ref(tmp_path):
    (tmp_path / "vergil.toml").write_text('[dependencies]\nvergil = "v2.0"\n')
    _seed_settings(tmp_path, ref=None)
    result = VergilUpdater().apply(_ctx(tmp_path))
    assert result.changed is True
    data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert data["extraKnownMarketplaces"]["vergil-marketplace"]["source"]["ref"] == "v2.0"


def test_apply_normalize_self_repo_uses_develop(tmp_path):
    (tmp_path / "vergil.toml").write_text('[dependencies]\nvergil = "v2.1"\n')
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "marketplace.json").write_text("{}")
    _seed_settings(tmp_path, ref=None)
    VergilUpdater().apply(_ctx(tmp_path))
    data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert data["extraKnownMarketplaces"]["vergil-marketplace"]["source"]["ref"] == "develop"


def test_apply_bump_sets_ref_to_bumped_version(tmp_path):
    (tmp_path / "vergil.toml").write_text('[dependencies]\nvergil = "v2.0"\n')
    _seed_settings(tmp_path, ref="v2.0")
    ctx = UpdateDepsContext(repo="o/r", repo_root=tmp_path, worktree_path=tmp_path)
    ctx.vergil_bump = "2.1"
    VergilUpdater().apply(ctx)
    data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert data["extraKnownMarketplaces"]["vergil-marketplace"]["source"]["ref"] == "v2.1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/vergil_tooling/test_update_deps_vergil_eco.py -k apply -v`
Expected: FAIL — the marketplace ref is unset / wrong (writer not yet wired in)

- [ ] **Step 3: Update `VergilUpdater.apply`**

Replace the body of `apply` with:

```python
    def apply(self, ctx: UpdateDepsContext) -> UpdateResult:
        base = _base(ctx)
        is_self = is_marketplace_source_repo(base)
        if ctx.vergil_bump is not None:
            target = format_version(ctx.vergil_bump)
            bumped = set_source_version(base, target)
            normalized = normalize_refs(base, target)
            claude = normalize_claude_ref(base, "develop" if is_self else target)
            return UpdateResult(
                updater=self.name,
                changed=bumped or bool(normalized) or claude is not None,
                summary=f"bump vergil to {target}",
                commit_message=f"chore(deps): bump vergil to {target}",
            )
        target = read_source_version(base)
        normalized = normalize_refs(base, target)
        claude = normalize_claude_ref(base, "develop" if is_self else target)
        changed = bool(normalized) or claude is not None
        return UpdateResult(
            updater=self.name,
            changed=changed,
            summary=(
                f"normalize vergil refs to {target}"
                if changed
                else f"vergil refs already at {target}"
            ),
            commit_message=f"chore(deps): normalize vergil ecosystem refs ({target})",
        )
```

- [ ] **Step 4: Run the full eco test file**

Run: `uv run pytest tests/vergil_tooling/test_update_deps_vergil_eco.py -v`
Expected: PASS (all, including pre-existing)

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/update_deps/updaters/vergil_eco.py tests/vergil_tooling/test_update_deps_vergil_eco.py
vrg-commit --type feat --scope deps --message "normalize claude marketplace ref in VergilUpdater (#1654)"
```

---

## Task 5: Audit the marketplace ref in `repo_config`

**Files:**
- Modify: `src/vergil_tooling/lib/repo_config.py`
- Test: `tests/vergil_tooling/test_repo_config.py`

This replaces the exact-template-equality check for `extraKnownMarketplaces` (which would now false-positive on the per-repo ref) with a ref-aware check, while still verifying the marketplace points at the right repo. `enabledPlugins` keeps its existing exact check.

- [ ] **Step 1: Write the failing tests**

Add a test class to `tests/vergil_tooling/test_repo_config.py`. Use the existing helper conventions (a complete settings dict + a vergil.toml):

```python
class TestMarketplaceRef:
    def _write(self, base: Path, *, version: str, ref, self_repo: bool = False) -> None:
        (base / "vergil.toml").write_text(_MINIMAL_VERGIL_TOML_VER.format(version=version))
        if self_repo:
            (base / ".claude-plugin").mkdir()
            (base / ".claude-plugin" / "marketplace.json").write_text("{}")
        src = {"source": "github", "repo": "vergil-project/vergil-claude-plugin"}
        if ref is not None:
            src["ref"] = ref
        settings = {
            "extraKnownMarketplaces": {"vergil-marketplace": {"source": src}},
            "enabledPlugins": {"vergil@vergil-marketplace": True},
        }
        claude = base / ".claude"
        claude.mkdir(parents=True, exist_ok=True)
        (claude / "settings.json").write_text(json.dumps(settings, indent=2))
        (claude / "hooks").mkdir(parents=True, exist_ok=True)
        (claude / "hooks" / "guard.sh").write_text("#!/bin/sh\n")

    def test_consumer_ref_matches(self, tmp_path: Path) -> None:
        self._write(tmp_path, version="v2.0", ref="v2.0")
        diff = audit_local_config(tmp_path)
        assert not [i for i in diff.items if "marketplace_ref" in i.field]

    def test_consumer_ref_missing_flagged(self, tmp_path: Path) -> None:
        self._write(tmp_path, version="v2.0", ref=None)
        diff = audit_local_config(tmp_path)
        flagged = [i for i in diff.items if i.field == "local.claude_settings.marketplace_ref"]
        assert len(flagged) == 1
        assert "v2.0" in flagged[0].expected

    def test_consumer_ref_wrong_flagged(self, tmp_path: Path) -> None:
        self._write(tmp_path, version="v2.1", ref="v2.0")
        diff = audit_local_config(tmp_path)
        assert [i for i in diff.items if i.field == "local.claude_settings.marketplace_ref"]

    def test_self_repo_requires_develop(self, tmp_path: Path) -> None:
        self._write(tmp_path, version="v2.1", ref="develop", self_repo=True)
        diff = audit_local_config(tmp_path)
        assert not [i for i in diff.items if "marketplace_ref" in i.field]

    def test_self_repo_version_ref_flagged(self, tmp_path: Path) -> None:
        self._write(tmp_path, version="v2.1", ref="v2.1", self_repo=True)
        diff = audit_local_config(tmp_path)
        assert [i for i in diff.items if i.field == "local.claude_settings.marketplace_ref"]
```

Add this fixture template near the top of the test file (alongside `_MINIMAL_VERGIL_TOML`):

```python
_MINIMAL_VERGIL_TOML_VER = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "python"

[project.co-authors]

[ci]
versions = ["3.12"]

[dependencies]
vergil = "{version}"
"""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/vergil_tooling/test_repo_config.py::TestMarketplaceRef -v`
Expected: FAIL — `marketplace_ref` field never emitted (check not implemented)

- [ ] **Step 3: Implement the check**

In `repo_config.py`, add the import:

```python
from vergil_tooling.lib.update_deps.context import UpdateDepsError
from vergil_tooling.lib.vergil_refs import (
    MARKETPLACE_NAME,
    expected_claude_ref,
)
```

Replace the `extraKnownMarketplaces` call in `_check_claude_settings` (drop the `_check_settings_section(... key="extraKnownMarketplaces" ...)` call; keep the `enabledPlugins` one) and add a dedicated check:

```python
    template = _load_settings_template()
    _check_marketplace_ref(repo_root, raw, items)
    _check_settings_section(
        raw,
        template,
        key="enabledPlugins",
        field="local.claude_settings.plugin",
        items=items,
    )
```

```python
def _check_marketplace_ref(
    repo_root: Path, raw: dict[str, Any], items: list[DiffItem]
) -> None:
    """Assert the marketplace points at the right repo and carries the
    version-derived ref (or ``develop`` for the marketplace source repo)."""
    section = raw.get("extraKnownMarketplaces", {})
    entry = section.get(MARKETPLACE_NAME) if isinstance(section, dict) else None
    if not isinstance(entry, dict):
        items.append(
            DiffItem(
                field="local.claude_settings.marketplace",
                expected=f"{MARKETPLACE_NAME} present",
                actual="missing",
            )
        )
        return
    source = entry.get("source")
    if not isinstance(source, dict) or source.get("repo") != (
        "vergil-project/vergil-claude-plugin"
    ):
        items.append(
            DiffItem(
                field="local.claude_settings.marketplace",
                expected="source.repo = vergil-project/vergil-claude-plugin",
                actual=f"source = {json.dumps(source, sort_keys=True)}",
            )
        )
        return
    try:
        expected = expected_claude_ref(repo_root)
    except (UpdateDepsError, FileNotFoundError):
        # vergil.toml problems are reported by _check_vergil_toml.
        return
    actual_ref = source.get("ref")
    if actual_ref != expected:
        items.append(
            DiffItem(
                field="local.claude_settings.marketplace_ref",
                expected=f"ref = {expected}",
                actual=f"ref = {actual_ref}",
            )
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/vergil_tooling/test_repo_config.py -v`
Expected: PASS (TestMarketplaceRef and all pre-existing)

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/repo_config.py tests/vergil_tooling/test_repo_config.py
vrg-commit --type feat --scope audit --message "assert claude marketplace ref matches vergil.toml (#1654)"
```

---

## Task 6: Audit the workflow pins (unified guard)

**Files:**
- Modify: `src/vergil_tooling/lib/repo_config.py`
- Test: `tests/vergil_tooling/test_repo_config.py`

Closes the pre-existing gap: workflow pins are written on bump but never audited. Workflow pins expect the **version** (`read_source_version`) even for the self repo — only the marketplace ref is `develop`.

- [ ] **Step 1: Write the failing tests**

```python
class TestWorkflowRefs:
    def _write_wf(self, base: Path, version: str, pin: str) -> None:
        (base / "vergil.toml").write_text(_MINIMAL_VERGIL_TOML_VER.format(version=version))
        wf = base / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text(
            "jobs:\n  a:\n"
            f"    uses: vergil-project/vergil-actions/.github/workflows/ci.yml@{pin}\n"
        )

    def test_matching_pin_clean(self, tmp_path: Path) -> None:
        self._write_wf(tmp_path, "v2.0", "v2.0")
        diff = audit_local_config(tmp_path)
        assert not [i for i in diff.items if i.field == "local.workflow_ref"]

    def test_drifted_pin_flagged(self, tmp_path: Path) -> None:
        self._write_wf(tmp_path, "v2.1", "v2.0")
        diff = audit_local_config(tmp_path)
        flagged = [i for i in diff.items if i.field == "local.workflow_ref"]
        assert len(flagged) == 1
        assert "v2.1" in flagged[0].expected and "v2.0" in flagged[0].actual

    def test_no_workflows_no_flag(self, tmp_path: Path) -> None:
        (tmp_path / "vergil.toml").write_text(_MINIMAL_VERGIL_TOML_VER.format(version="v2.0"))
        diff = audit_local_config(tmp_path)
        assert not [i for i in diff.items if i.field == "local.workflow_ref"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/vergil_tooling/test_repo_config.py::TestWorkflowRefs -v`
Expected: FAIL — `local.workflow_ref` never emitted

- [ ] **Step 3: Implement the check**

Add to the imports in `repo_config.py`:

```python
from vergil_tooling.lib.vergil_refs import (
    MARKETPLACE_NAME,
    expected_claude_ref,
    iter_workflow_refs,
    read_source_version,
)
```

Call it from `audit_local_config`:

```python
def audit_local_config(repo_root: Path) -> ConfigDiff:
    """Run all local config checks against a repo root directory."""
    items: list[DiffItem] = []
    _check_vergil_toml(repo_root, items)
    _check_hook_guard_shim(repo_root, items)
    _check_claude_md(repo_root, items)
    _check_claude_settings(repo_root, items)
    _check_workflow_refs(repo_root, items)
    return ConfigDiff(items=items)
```

```python
def _check_workflow_refs(repo_root: Path, items: list[DiffItem]) -> None:
    """Assert every vergil-* reusable-workflow pin matches the vergil.toml version."""
    try:
        expected = read_source_version(repo_root)
    except (UpdateDepsError, FileNotFoundError):
        return  # vergil.toml problems reported by _check_vergil_toml
    for path, actual in iter_workflow_refs(repo_root):
        if actual != expected:
            items.append(
                DiffItem(
                    field="local.workflow_ref",
                    expected=f"{path.name}: {expected}",
                    actual=f"{path.name}: {actual}",
                )
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/vergil_tooling/test_repo_config.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/repo_config.py tests/vergil_tooling/test_repo_config.py
vrg-commit --type feat --scope audit --message "assert workflow pins match vergil.toml (#1654)"
```

---

## Task 7: Seed the ref in `repo_init`

**Files:**
- Modify: `src/vergil_tooling/lib/repo_init.py` (around lines 746–750)
- Test: `tests/vergil_tooling/test_repo_init.py`

The settings template is ref-less; `repo_init` injects the ref from `ctx.vergil_version`. A freshly initialized repo is never the marketplace source repo, so it always gets the version. Extract a pure helper for testability.

- [ ] **Step 1: Write the failing test**

```python
# tests/vergil_tooling/test_repo_init.py  (add; create file with header if absent)
from __future__ import annotations

import json

from vergil_tooling.lib.repo_init import render_claude_settings


def test_render_claude_settings_injects_ref() -> None:
    text = render_claude_settings("v2.1")
    data = json.loads(text)
    src = data["extraKnownMarketplaces"]["vergil-marketplace"]["source"]
    assert src["ref"] == "v2.1"
    assert text.endswith("\n")


def test_render_claude_settings_other_version() -> None:
    data = json.loads(render_claude_settings("v2.0"))
    assert data["extraKnownMarketplaces"]["vergil-marketplace"]["source"]["ref"] == "v2.0"
```

If `tests/vergil_tooling/test_repo_init.py` does not exist, create it with the above. If it exists, append the two tests and reuse its imports.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/vergil_tooling/test_repo_init.py -k render_claude_settings -v`
Expected: FAIL — `cannot import name 'render_claude_settings'`

- [ ] **Step 3: Add the helper and use it**

Ensure `import json` is present at the top of `repo_init.py` (add if missing). Add the helper near `_load_data_file`:

```python
def render_claude_settings(vergil_version: str) -> str:
    """Return the .claude/settings.json text with the marketplace ref seeded."""
    data = json.loads(_load_data_file("claude_settings.json"))
    data["extraKnownMarketplaces"]["vergil-marketplace"]["source"]["ref"] = vergil_version
    return json.dumps(data, indent=2) + "\n"
```

Replace the two lines at ~746–750 that write the settings file:

```python
    # .claude/settings.json — marketplace ref seeded from the chosen version
    (claude_dir / "settings.json").write_text(render_claude_settings(ctx.vergil_version))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/vergil_tooling/test_repo_init.py -k render_claude_settings -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/repo_init.py tests/vergil_tooling/test_repo_init.py
vrg-commit --type feat --scope init --message "seed claude marketplace ref from vergil version (#1654)"
```

---

## Task 8: Full validation + spec/plan cross-check

**Files:** none (verification)

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest tests/vergil_tooling/test_vergil_refs.py tests/vergil_tooling/test_update_deps_vergil_eco.py tests/vergil_tooling/test_repo_config.py tests/vergil_tooling/test_repo_init.py -v`
Expected: PASS (all)

- [ ] **Step 2: Run the repo's standard validation gate**

Run the project's standard validation (lint + type + full tests) as documented for vergil-tooling. Expected: green.

- [ ] **Step 3: Confirm no other importer of the moved symbols broke**

Run: `uv run pytest tests/vergil_tooling/ -q`
Expected: PASS (whole suite)

---

## Task 9 (GATE — manual): moving-tag refresh smoke test

**This gates the fleet sweep (Task 10). Do not sweep until this passes.**

- [ ] **Step 1: On a persistent install, pin a test consumer to a `vX.Y` tag**

In a throwaway consumer repo's `.claude/settings.json`, set the marketplace `source.ref` to a `vX.Y` tag that you control (e.g. `v2.1`). Run `claude plugin marketplace update vergil-marketplace` and confirm the installed plugin matches the tag's current commit.

- [ ] **Step 2: Advance the moving tag and re-update**

Cut/simulate a patch so `vrg-promote` force-moves `v2.1` to a newer commit. On the same persistent install, run `claude plugin marketplace update vergil-marketplace` again.

- [ ] **Step 3: Verify the install advanced**

Confirm the install now reflects the new commit the moved tag points to.
- **PASS** → the design ships as written (moving `vX.Y` tag).
- **FAIL** (install stuck on the old commit) → adopt the `release/X.Y` **branch** fallback: change `expected_claude_ref` and `read_source_version`'s consumers to derive `release/X.Y` for the marketplace ref (workflow pins stay on `vX.Y`), update the corresponding tests, and re-run Tasks 4–7. Record the outcome in the spec's refresh-gate section.

---

## Task 10 (operational): fleet sweep

**Files:** none in this repo (per-repo PRs).

- [ ] **Step 1: Dry-run normalize on one consumer**

In a `v2.0` consumer worktree, run `vrg-update-deps` in normalize mode and confirm the diff sets the marketplace ref to `v2.0` (and corrects any drifted workflow pins). Review before committing.

- [ ] **Step 2: Sweep every repo carrying a `vergil.toml`**

For each repo (the mq-rest-admin set, logical-minds-foundry, diogenes, the vergil-* repos, the `.github` org repos, wphillipmoore/the-infrastructure-mindset), run `vrg-update-deps` normalize and open a PR through the normal flow. The plugin repo (`vergil-claude-plugin`) normalizes to `develop`.

- [ ] **Step 3: Confirm compliance**

Run the `repo_config` audit across the fleet; expect zero `marketplace_ref` / `workflow_ref` findings.

---

## Self-Review

**Spec coverage:**
- Shared derivation helpers → Task 1. ✓
- Write side (`normalize_claude_ref` + wiring) → Tasks 2–4. ✓
- Audit side (marketplace ref + workflow pins, unified) → Tasks 5–6. ✓
- Self-detection via `.claude-plugin/marketplace.json` → Task 1 (`is_marketplace_source_repo`), used in Tasks 4–5. ✓
- Seeding (`repo_init`) → Task 7. ✓
- Moving-tag gate → Task 9. ✓
- Fleet sweep → Task 10. ✓
- Template "drops to no-ref form": already ref-less; no change needed — noted in the plan preamble and Task 7. ✓

**Type/name consistency:** `expected_claude_ref`, `read_source_version`, `iter_workflow_refs`, `is_marketplace_source_repo`, `normalize_claude_ref`, `MARKETPLACE_NAME`, `render_claude_settings` are defined once and referenced consistently. Field names `local.claude_settings.marketplace`, `local.claude_settings.marketplace_ref`, `local.workflow_ref` are used identically in code and tests.

**Placeholder scan:** no TBD/TODO; every code step carries complete code; Task 9 is intentionally a manual procedure with explicit pass/fail branches.
