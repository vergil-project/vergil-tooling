# `.claude` Share-Set Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the vestigial `~/.claude/sessions` VM mount, document the projects-vs-sessions persistence model, and keep agent-VM Claude plugins current with a VM-local refresh that mirrors the existing vergil-tooling update path — without ever sharing plugins across the host/VM boundary.

**Architecture:** All agent-VM `.claude` wiring lives in `src/vergil_tooling/lib/lima.py` (mounts, symlinks, config copy, in-VM update primitives) and is driven by the lifecycle pipeline + `update` command in `src/vergil_tooling/bin/vrg_vm.py`. We add an `update_plugins(instance)` primitive next to `update_tooling`, fold it into `vrg-vm update`, and add a warn-mode `update-plugins` stage to the start and rebuild pipelines. Plugins are installed VM-locally from their GitHub marketplaces (declared in the copied `settings.json`); the refresh just advances them to latest.

**Tech Stack:** Python 3.12+, pytest + `unittest.mock`, Lima (`limactl`), MkDocs docs site. Git via `vrg-git`/`vrg-commit` (raw `git` is blocked in this repo).

**Spec:** `docs/specs/2026-06-11-claude-share-set-audit-design.md` (issue #1603).

---

## Working context (read before starting)

- **Worktree:** all work happens in `/Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1603-claude-share-set/` on branch `feature/1603-claude-share-set`. Use absolute paths or `cd` into the worktree first.
- **Git:** use `vrg-git` (never raw `git`) and `vrg-commit` (never `git commit`). `vrg-commit` signature:
  `vrg-commit --type <type> --scope <scope> --message "<desc>" --body "<body>"`.
- **Run a single test:** from inside the worktree,
  `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest <path>::<test> -v`.
  (The dev-tree override venv; see CLAUDE.md "Environment Setup".)
- **Full validation (final gate only):** `vrg-container-run -- vrg-validate` (transparently expands to `uv run vrg-validate` here).
- **Do not run `vrg-submit-pr`.** PR submission is the human's job.

## File structure

| File | Responsibility | Tasks |
|---|---|---|
| `src/vergil_tooling/lib/lima.py` | `create_vm` mounts; `update_plugins` primitive; persistence-model comment | 1, 2, 3 |
| `src/vergil_tooling/bin/vrg_vm.py` | fold plugins into `vrg-vm update`; warn-mode start/rebuild stage | 4, 5 |
| `docs/site/docs/guides/agent-vm-claude-share-set.md` | new durable guide (persistence + plugin model) | 2 |
| `docs/site/mkdocs.yml` | register the new guide in nav | 2 |
| `tests/vergil_tooling/test_lima.py` | mount-removal + `update_plugins` coverage | 1, 3 |
| `tests/vergil_tooling/test_vrg_vm.py` | update-command + start/rebuild-stage coverage | 4, 5 |

---

## Task 1: Remove the vestigial `~/.claude/sessions` mount

**Files:**
- Modify: `src/vergil_tooling/lib/lima.py` (`create_vm`, ~lines 215-240)
- Test: `tests/vergil_tooling/test_lima.py` (`test_adds_claude_submounts` ~280, `test_creates_host_claude_dirs` ~300)

Rationale: `mounts[3]` mounts the host `~/.claude/sessions`, but the VM keeps `sessions/` VM-local (`_CLAUDE_UNLINK_DIRS`) and never reads the mount. Nothing host-side reads that dir either — `vrg_vm_resolve.read_roster` reads the VM's *local* roster in-guest. Keep `_CLAUDE_UNLINK_DIRS` and the symlink-removal loop untouched (they still clean up stale symlinks on old VMs).

- [ ] **Step 1: Update the two tests to assert the sessions mount and host dir are gone**

In `tests/vergil_tooling/test_lima.py`, replace the body of `test_adds_claude_submounts` (drop the `claude_sessions` local and the three `mounts[3]` asserts; add an assert that no `mounts[3]` arg exists):

```python
    @patch("vergil_tooling.lib.lima.Path.home")
    @patch("vergil_tooling.lib.lima._limactl")
    def test_adds_claude_submounts(
        self, mock: MagicMock, mock_home: MagicMock, tmp_path: Path
    ) -> None:
        mock_home.return_value = tmp_path
        tpl = Path("/tmp/template.yaml")  # noqa: S108
        create_vm("vergil-agent", tpl, "/home/user/projects")
        args = mock.call_args[0]
        claude_projects = str(tmp_path / ".claude" / "projects")
        claude_skills = str(tmp_path / ".claude" / "skills")
        assert f'--set=.mounts[1].location = "{claude_projects}"' in args
        assert f'--set=.mounts[1].mountPoint = "{claude_projects}"' in args
        assert "--set=.mounts[1].writable = true" in args
        assert f'--set=.mounts[2].location = "{claude_skills}"' in args
        assert f'--set=.mounts[2].mountPoint = "{claude_skills}"' in args
        assert "--set=.mounts[2].writable = false" in args
        # The vestigial sessions mount (mounts[3]) is removed; sessions stays VM-local.
        assert not any(".mounts[3]" in a for a in args)
```

And replace the body of `test_creates_host_claude_dirs` (drop the `sessions` dir assertion; assert it is NOT created):

```python
    @patch("vergil_tooling.lib.lima.Path.home")
    @patch("vergil_tooling.lib.lima._limactl")
    def test_creates_host_claude_dirs(
        self, _mock: MagicMock, mock_home: MagicMock, tmp_path: Path
    ) -> None:
        mock_home.return_value = tmp_path
        tpl = Path("/tmp/template.yaml")  # noqa: S108
        create_vm("vergil-agent", tpl, "/home/user/projects")
        assert (tmp_path / ".claude" / "projects").is_dir()
        assert (tmp_path / ".claude" / "skills").is_dir()
        # create_vm no longer backs a sessions mount, so it must not create the dir.
        assert not (tmp_path / ".claude" / "sessions").is_dir()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_lima.py::TestCreateVm::test_adds_claude_submounts tests/vergil_tooling/test_lima.py::TestCreateVm::test_creates_host_claude_dirs -v`
Expected: FAIL — `test_adds_claude_submounts` fails on the new `mounts[3]` assertion; `test_creates_host_claude_dirs` fails because the sessions dir is still created.

- [ ] **Step 3: Remove the sessions mount from `create_vm`**

In `src/vergil_tooling/lib/lima.py`, change the head of `create_vm`. Replace:

```python
    claude_projects_path = Path.home() / ".claude" / "projects"
    claude_skills_path = Path.home() / ".claude" / "skills"
    claude_sessions_path = Path.home() / ".claude" / "sessions"
    claude_projects_path.mkdir(parents=True, exist_ok=True)
    claude_skills_path.mkdir(parents=True, exist_ok=True)
    claude_sessions_path.mkdir(parents=True, exist_ok=True)
    claude_projects = str(claude_projects_path)
    claude_skills = str(claude_skills_path)
    claude_sessions = str(claude_sessions_path)
```

with:

```python
    claude_projects_path = Path.home() / ".claude" / "projects"
    claude_skills_path = Path.home() / ".claude" / "skills"
    claude_projects_path.mkdir(parents=True, exist_ok=True)
    claude_skills_path.mkdir(parents=True, exist_ok=True)
    claude_projects = str(claude_projects_path)
    claude_skills = str(claude_skills_path)
```

And in the `args` list, delete these three lines:

```python
        f'--set=.mounts[3].location = "{claude_sessions}"',
        f'--set=.mounts[3].mountPoint = "{claude_sessions}"',
        "--set=.mounts[3].writable = true",
```

Leave `_CLAUDE_LINK_DIRS`, `_CLAUDE_UNLINK_DIRS`, and `link_claude_dirs` unchanged.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_lima.py::TestCreateVm -v`
Expected: PASS (all `TestCreateVm` tests green).

- [ ] **Step 5: Commit**

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1603-claude-share-set
vrg-git add src/vergil_tooling/lib/lima.py tests/vergil_tooling/test_lima.py
vrg-commit --type fix --scope lima \
  --message "remove vestigial .claude/sessions VM mount (#1603)" \
  --body "mounts[3] mounted the host ~/.claude/sessions, but the VM keeps sessions/ VM-local (_CLAUDE_UNLINK_DIRS) and never reads the mount; nothing host-side reads it either. Remove the mount and its host mkdir. The _CLAUDE_UNLINK_DIRS guard stays so old VMs with a stale sessions symlink are still cleaned up on re-link.

Ref #1603"
```

---

## Task 2: Document the projects-vs-sessions persistence model

**Files:**
- Modify: `src/vergil_tooling/lib/lima.py` (comment block at `_CLAUDE_LINK_DIRS`, ~lines 622-636)
- Create: `docs/site/docs/guides/agent-vm-claude-share-set.md`
- Modify: `docs/site/mkdocs.yml` (nav, ~line 128-132)

Documentation-only task (no unit tests). Validation is markdownlint via the final `vrg-validate` gate (Task 6).

- [ ] **Step 1: Expand the code comment to state the full model**

In `src/vergil_tooling/lib/lima.py`, replace the comment immediately above `_CLAUDE_LINK_DIRS`:

```python
# Subdirs symlinked to the path-preserved host mounts so they are shared with
# the host and survive VM rebuilds. projects/ holds the conversation
# transcripts (append writes, which work fine through the virtiofs mount);
# skills/ is a read-only reference mount.
_CLAUDE_LINK_DIRS = ("projects", "skills")
```

with:

```python
# The agent-VM ~/.claude share set. See
# docs/site/docs/guides/agent-vm-claude-share-set.md for the full model.
#
# projects/ -> durable, host-shared transcript store. Resume-after-rebuild
#   reads these, so a conversation survives a VM rebuild (the data lives on
#   the host). Append-only writes, which work fine through the virtiofs mount.
# skills/   -> read-only reference mount.
# sessions/ -> NOT shared; see _CLAUDE_UNLINK_DIRS below. It is a disposable
#   VM-local roster (pid->session), regenerated each run. Resume does NOT
#   depend on it, so keeping it VM-local does not break resume.
# plugins/  -> NOT shared; kept current VM-locally via update_plugins (each VM
#   installs/refreshes from the GitHub marketplaces declared in the copied
#   settings.json). Sharing the host's materialized checkout across the
#   macOS/Linux boundary would be fragile and is unnecessary.
_CLAUDE_LINK_DIRS = ("projects", "skills")
```

- [ ] **Step 2: Create the durable guide**

Create `docs/site/docs/guides/agent-vm-claude-share-set.md` with exactly this content:

```markdown
# Agent VM `.claude` Share Set

This guide explains which parts of the host's `~/.claude` directory are
shared into agent VMs, how conversation resume survives a VM rebuild, and
why plugins and the session roster are deliberately kept VM-local. The
wiring lives in `src/vergil_tooling/lib/lima.py` (`create_vm`,
`link_claude_dirs`, `copy_claude_config`); the `vergil-vm` template only
declares the static `projects` mount.

## The share set

| Subdir | How it is shared | Writable | Why |
|---|---|---|---|
| `projects/` | virtiofs mount + symlink | yes | Durable conversation transcripts; must survive rebuilds. |
| `skills/` | virtiofs mount + symlink | no | Read-only reference. |
| `sessions/` | **not shared** (VM-local) | n/a | Disposable per-VM roster; sharing breaks atomic writes (EXDEV). |
| `plugins/` | **not shared** (VM-local) | n/a | Installed/refreshed in-VM from GitHub marketplaces. |

`copy_claude_config` additionally copies `CLAUDE.md` and `settings.json`
into each VM on create and on every start.

## Why resume survives a rebuild

Conversation transcripts are written as append-only `*.jsonl` files under
`~/.claude/projects/<slug>/`. That directory is a symlink onto the
path-preserved host mount, so the transcripts live on the host and are
untouched when a VM is destroyed and recreated. Resuming a conversation
reads those transcripts — **not** the `sessions/` roster — so a rebuild
never loses history.

## Why `sessions/` is VM-local

`~/.claude/sessions/` holds a small roster of `pid -> session` files that
Claude writes atomically: it writes a temp file in the VM-local tmpdir and
then `rename()`s it onto the target. Renaming across filesystems (VM-local
ext4 to the virtiofs host mount) fails with `EXDEV`, so the write would
silently fail and no roster file would ever appear. The roster is also
per-machine (pids only mean anything on the owning host), so there is no
value in sharing it. Session detection reads each VM's local roster
in-guest over `limactl shell`. See vergil-tooling #1301 and vergil-vm #73.

> A host `~/.claude/sessions` mount used to exist (`mounts[3]`) but the VM
> never read it once the roster was made VM-local; it was removed as dead
> weight (#1603).

## Why plugins are VM-local, not shared

Plugins are declared in the host `settings.json` (`enabledPlugins` and
`extraKnownMarketplaces`), which is copied into each VM. The marketplaces
are **GitHub repositories**, so each VM installs the enabled plugins itself
on first launch and keeps them current with an in-VM refresh — the same
model used for vergil-tooling. The host's materialized `~/.claude/plugins`
checkout is never shared: doing so would cross the macOS/Linux boundary
(fragile if any plugin ships a binary) and hit the same `EXDEV` write
problem as the roster. Instead, `update_plugins` runs `claude plugin
marketplace update` + `claude plugin update` inside the VM, driven by
`vrg-vm update` and a warn-mode stage on VM start/rebuild.

See also: [Identity Architecture](identity-architecture.md).
```

- [ ] **Step 3: Register the guide in the docs nav**

In `docs/site/mkdocs.yml`, under the `Identity & Permissions:` nav section, add the new guide after `Account Setup`. Change:

```yaml
  - Identity & Permissions:
      - Identity Architecture: guides/identity-architecture.md
      - Credential Management: guides/credential-management.md
      - Permission Model: guides/permission-model.md
      - Account Setup: guides/account-setup.md
```

to:

```yaml
  - Identity & Permissions:
      - Identity Architecture: guides/identity-architecture.md
      - Credential Management: guides/credential-management.md
      - Permission Model: guides/permission-model.md
      - Account Setup: guides/account-setup.md
      - Agent VM Claude Share Set: guides/agent-vm-claude-share-set.md
```

- [ ] **Step 4: Sanity-check the new doc renders as valid markdown**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run python -c "import pathlib; print(pathlib.Path('docs/site/docs/guides/agent-vm-claude-share-set.md').read_text().count('#'), 'headings')"`
Expected: prints a heading count > 0 (a cheap existence/readability check; full markdownlint runs in Task 6).

- [ ] **Step 5: Commit**

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1603-claude-share-set
vrg-git add src/vergil_tooling/lib/lima.py docs/site/docs/guides/agent-vm-claude-share-set.md docs/site/mkdocs.yml
vrg-commit --type docs --scope lima \
  --message "document the agent-VM .claude persistence model (#1603)" \
  --body "Expand the _CLAUDE_LINK_DIRS comment to state the full share-set model and add a durable guide: projects/ is the host-shared transcript store that makes resume survive rebuilds; sessions/ and plugins/ are deliberately VM-local. Register the guide in the docs nav.

Ref #1603"
```

---

## Task 3: Add the `update_plugins` VM-local refresh primitive

**Files:**
- Modify: `src/vergil_tooling/lib/lima.py` (add `update_plugins` after `update_tooling`, ~line 580)
- Test: `tests/vergil_tooling/test_lima.py` (new `TestUpdatePlugins` class)

`update_plugins` mirrors `update_tooling`: run two `claude plugin` commands inside the VM via `shell_run`. It is invoked through a **login shell** (`bash -lc`) so the VM's interactive environment resolves `claude` on `PATH` — `claude` is a global npm install, not a `uv` tool, so the `~/.local/bin` export used by `update_tooling` does not apply. (Task 6 verifies this resolves on a real VM.)

- [ ] **Step 1: Write the failing test**

Add to `tests/vergil_tooling/test_lima.py`. First ensure `update_plugins` is in the imports from `vergil_tooling.lib.lima` (add it next to `update_tooling` in the existing import block at the top of the file). Then add:

```python
class TestUpdatePlugins:
    @patch("vergil_tooling.lib.lima.shell_run")
    def test_runs_marketplace_then_plugin_update(self, mock_run: MagicMock) -> None:
        update_plugins("vergil-agent")
        # Two in-VM commands, marketplace refresh before plugin update.
        assert mock_run.call_count == 2
        first_cmd = mock_run.call_args_list[0][0][-1]
        second_cmd = mock_run.call_args_list[1][0][-1]
        assert "claude plugin marketplace update" in first_cmd
        assert "claude plugin update" in second_cmd
        # Invoked via a login shell so claude resolves on PATH.
        assert mock_run.call_args_list[0][0][:3] == ("vergil-agent", "bash", "-lc")
        assert mock_run.call_args_list[1][0][:3] == ("vergil-agent", "bash", "-lc")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_lima.py::TestUpdatePlugins -v`
Expected: FAIL — `ImportError`/`cannot import name 'update_plugins'` (or `NameError` at collection).

- [ ] **Step 3: Implement `update_plugins`**

In `src/vergil_tooling/lib/lima.py`, add directly after the `update_tooling` function (after its final line, ~580):

```python
def update_plugins(instance: str) -> None:
    """Refresh Claude Code plugins inside the VM.

    Plugins are installed VM-locally from their GitHub marketplaces (declared
    in the copied settings.json); they are deliberately not shared from the
    host (see docs/site/docs/guides/agent-vm-claude-share-set.md). This
    advances them to the latest published versions, mirroring how
    update_tooling advances vergil-tooling.

    Uses a login shell (bash -lc) so claude resolves on PATH: claude is a
    global npm install, not a uv tool, so the ~/.local/bin export that the
    tooling install uses does not apply here.
    """
    print("  Refreshing Claude plugins...")
    shell_run(instance, "bash", "-lc", "claude plugin marketplace update")
    shell_run(instance, "bash", "-lc", "claude plugin update")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_lima.py::TestUpdatePlugins -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1603-claude-share-set
vrg-git add src/vergil_tooling/lib/lima.py tests/vergil_tooling/test_lima.py
vrg-commit --type feat --scope lima \
  --message "add update_plugins VM-local plugin refresh (#1603)" \
  --body "update_plugins runs 'claude plugin marketplace update' then 'claude plugin update' inside the VM over a login shell, mirroring update_tooling. Plugins stay VM-local (installed from the GitHub marketplaces declared in settings.json); this only advances them to latest.

Ref #1603"
```

---

## Task 4: Fold the plugin refresh into `vrg-vm update`

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_vm.py` (import `update_plugins`; call it in `_update_instance`, ~line 533)
- Test: `tests/vergil_tooling/test_vrg_vm.py` (`TestUpdate` class, ~line 907; existing tests need a new patch)

`vrg-vm update` and `vrg-vm update --all` both go through `_update_instance`. Adding `update_plugins` there makes a single "update my VM(s)" refresh both tooling and plugins. **Because the existing `TestUpdate` tests drive `_update_instance` with the real functions patched, they will call the real `update_plugins` (hitting `limactl`) unless you add a patch.**

- [ ] **Step 1: Add a failing assertion and patch the existing update tests**

In `tests/vergil_tooling/test_vrg_vm.py`:

(a) Add a new test to `TestUpdate` that asserts plugins are refreshed:

```python
    @patch("vergil_tooling.bin.vrg_vm.update_plugins")
    @patch("vergil_tooling.bin.vrg_vm.get_tooling_version", return_value=None)
    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Running")
    def test_update_refreshes_plugins(
        self,
        _status: MagicMock,
        _update: MagicMock,
        _ver: MagicMock,
        mock_plugins: MagicMock,
        config_file: Path,
    ) -> None:
        result = main(["update", "--config", str(config_file)])
        assert result == 0
        mock_plugins.assert_called_once_with("vergil-agent")
```

(b) Every existing test in `TestUpdate` that patches `update_tooling` and reaches `_update_instance` must also patch `update_plugins`, or it will invoke the real one. Find them:

Run: `grep -n 'vrg_vm.update_tooling' tests/vergil_tooling/test_vrg_vm.py`

For each such test inside `TestUpdate` (`test_update_default_tag`, `test_update_explicit_tag`, `test_shows_version_change`, `test_shows_already_up_to_date`, `test_shows_version_when_before_unknown`, and the `update --all` tests in `TestUpdateAll`), add the decorator as the **innermost** `@patch` and a matching first positional param after `self`. Example — change:

```python
    @patch("vergil_tooling.bin.vrg_vm.get_tooling_version", return_value=None)
    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Running")
    def test_update_default_tag(
        self, _status: MagicMock, mock_update: MagicMock, _ver: MagicMock, config_file: Path
    ) -> None:
```

to:

```python
    @patch("vergil_tooling.bin.vrg_vm.update_plugins")
    @patch("vergil_tooling.bin.vrg_vm.get_tooling_version", return_value=None)
    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Running")
    def test_update_default_tag(
        self,
        _status: MagicMock,
        mock_update: MagicMock,
        _ver: MagicMock,
        _plugins: MagicMock,
        config_file: Path,
    ) -> None:
```

(Decorators apply bottom-up, so the bottom-most `@patch` maps to the first param after `self`. The newly added top decorator maps to the **last** mock param before the fixtures — here `_plugins`.)

- [ ] **Step 2: Run the new test to verify it fails**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_vrg_vm.py::TestUpdate::test_update_refreshes_plugins -v`
Expected: FAIL — `AssertionError: Expected 'update_plugins' to be called once. Called 0 times.`

- [ ] **Step 3: Wire `update_plugins` into `_update_instance`**

In `src/vergil_tooling/bin/vrg_vm.py`:

(a) Add `update_plugins` to the import from `vergil_tooling.lib.lima` (the block that already imports `update_tooling`, ~lines 42-56).

(b) In `_update_instance` (~line 533), refresh plugins after tooling:

```python
def _update_instance(instance: str, name: str, tag: str | None, fallback: str) -> None:
    """Update vergil-tooling and Claude plugins in one running VM, printing the version transition."""
    print(f"Updating vergil-tooling in VM '{instance}' (identity: {name})...")

    before = get_tooling_version(instance)
    update_tooling(instance, tag, fallback_tag=fallback)
    after = get_tooling_version(instance)

    if before and after:
        if before == after:
            print(f"  vergil-tooling: {after} (already up to date)")
        else:
            print(f"  vergil-tooling: {before} → {after}")
    elif after:
        print(f"  vergil-tooling: {after}")

    update_plugins(instance)
```

- [ ] **Step 4: Run the update tests to verify they pass**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_vrg_vm.py::TestUpdate tests/vergil_tooling/test_vrg_vm.py::TestUpdateAll -v`
Expected: PASS (new test green; existing tests still green because `update_plugins` is now patched in each).

- [ ] **Step 5: Commit**

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1603-claude-share-set
vrg-git add src/vergil_tooling/bin/vrg_vm.py tests/vergil_tooling/test_vrg_vm.py
vrg-commit --type feat --scope vrg-vm \
  --message "refresh Claude plugins in vrg-vm update (#1603)" \
  --body "vrg-vm update and update --all now refresh Claude plugins alongside vergil-tooling via _update_instance, so one 'update my VM' gesture brings everything current. Mirrors the established tooling-update path.

Ref #1603"
```

---

## Task 5: Refresh plugins on VM start and rebuild (warn-mode stage)

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_vm.py` (`_st_update_plugins` after `_st_update_tooling` ~line 349; add to `_start_stages` ~375 and `_rebuild_stages` ~388)
- Test: `tests/vergil_tooling/test_vrg_vm.py` (new stage-presence test; existing `TestStart`/`TestStartStaleness`/`TestRebuild` tests need a new patch)

Warn-mode means a failed plugin refresh surfaces as ⚠ in the lifecycle summary and never aborts the session — identical to `update-tooling`. **Adding the stage means every test that drives `main(["start"...])` or `main(["rebuild"...])` through the pipeline will call the real `update_plugins` unless patched.**

- [ ] **Step 1: Confirm the `Stage` field names, then write the failing stage-presence test**

First inspect how `Stage` is defined so the asserts match reality:

Run: `grep -rn "class Stage\|Stage = \|namedtuple\|@dataclass" src/vergil_tooling/lib/progress.py`
(Follow the `Stage` import in `vrg_vm.py` if it lives elsewhere.) Confirm the name attribute is `.name` and the mode attribute is `.mode`; if not, adjust the asserts below to the real field names.

In `tests/vergil_tooling/test_vrg_vm.py`, add (near the other lifecycle tests; the pipeline factories are module-level in `vergil_tooling.bin.vrg_vm`):

```python
class TestPluginStage:
    def test_start_pipeline_includes_update_plugins(self) -> None:
        from vergil_tooling.bin.vrg_vm import _start_stages

        names = [s.name for s in _start_stages()]
        assert "update-plugins" in names
        # Runs after tooling, in warn mode.
        assert names.index("update-plugins") > names.index("update-tooling")
        stage = next(s for s in _start_stages() if s.name == "update-plugins")
        assert stage.mode == "warn"

    def test_rebuild_pipeline_includes_update_plugins(self) -> None:
        from vergil_tooling.bin.vrg_vm import _rebuild_stages

        names = [s.name for s in _rebuild_stages()]
        assert "update-plugins" in names
        stage = next(s for s in _rebuild_stages() if s.name == "update-plugins")
        assert stage.mode == "warn"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_vrg_vm.py::TestPluginStage -v`
Expected: FAIL — `"update-plugins" in names` is False; the stage does not exist yet.

- [ ] **Step 3: Add the stage function and wire both pipelines**

In `src/vergil_tooling/bin/vrg_vm.py`, add after `_st_update_tooling` (~line 349):

```python
def _st_update_plugins(state: _LifecycleState) -> None:
    # Warn-mode stage: a failed plugin refresh surfaces as ⚠ and the lifecycle
    # continues, the same warn-and-continue contract as update-tooling. Plugins
    # are VM-local; this advances them to the latest published versions.
    update_plugins(state.target.instance)
```

In `_start_stages`, add the stage after `update-tooling`:

```python
def _start_stages() -> list[Stage]:
    return [
        Stage("start", _st_start, mode="fail_fast"),
        Stage("spec-check", _st_spec_check, mode="warn"),
        Stage("credentials", _st_credentials, mode="fail_fast"),
        Stage("copy-config", _st_copy_config, mode="fail_fast"),
        Stage("update-tooling", _st_update_tooling, mode="warn"),
        Stage("update-plugins", _st_update_plugins, mode="warn"),
    ]
```

In `_rebuild_stages`, add the stage after `copy-config` (rebuild has no `update-tooling`; it installs fresh, then this converges plugins to latest):

```python
def _rebuild_stages() -> list[Stage]:
    return [
        Stage("destroy", _st_destroy, mode="fail_fast"),
        Stage("fetch-template", _st_fetch_template, mode="fail_fast"),
        Stage("create", _st_create, mode="fail_fast"),
        Stage("start", _st_start, mode="fail_fast"),
        Stage("credentials", _st_credentials, mode="fail_fast"),
        Stage("tooling", _st_install_tooling, mode="fail_fast"),
        Stage("copy-config", _st_copy_config, mode="fail_fast"),
        Stage("update-plugins", _st_update_plugins, mode="warn"),
        Stage("cycle-ssh", _st_cycle_ssh, mode="fail_fast"),
    ]
```

(`update_plugins` is already imported from Task 4.)

- [ ] **Step 4: Run the stage test to verify it passes, then find the now-broken pipeline tests**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_vrg_vm.py::TestPluginStage -v`
Expected: PASS.

Now the existing pipeline tests call the real `update_plugins`. Surface them:

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_vrg_vm.py::TestStart tests/vergil_tooling/test_vrg_vm.py::TestStartStaleness tests/vergil_tooling/test_vrg_vm.py::TestRebuild -v`
Expected: FAIL/ERROR — real `update_plugins` calls `shell_run`/`limactl` (no VM). This tells you which tests to patch.

Patch them: every test that drives `main(["start"...])` or `main(["rebuild"...])` to completion needs `@patch("vergil_tooling.bin.vrg_vm.update_plugins")` as the **innermost** decorator plus a matching first param after `self`. Find them:

Run: `grep -n 'main(\["start"\|main(\["rebuild"' tests/vergil_tooling/test_vrg_vm.py`

Apply to each pipeline-completing test (the `*_fails_if_not_created` early-return tests do not reach the stage and need no patch). Example — change:

```python
    @patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.vm_age_days", return_value=1.0)
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Stopped")
    def test_start_and_inject(
        self,
        _status: MagicMock,
        _age: MagicMock,
        mock_start: MagicMock,
        mock_inject: MagicMock,
        mock_update: MagicMock,
        mock_copy: MagicMock,
        config_file: Path,
    ) -> None:
```

to (add the plugins patch at the top, param at the end before fixtures):

```python
    @patch("vergil_tooling.bin.vrg_vm.update_plugins")
    @patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.vm_age_days", return_value=1.0)
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Stopped")
    def test_start_and_inject(
        self,
        _status: MagicMock,
        _age: MagicMock,
        mock_start: MagicMock,
        mock_inject: MagicMock,
        mock_update: MagicMock,
        mock_copy: MagicMock,
        _plugins: MagicMock,
        config_file: Path,
    ) -> None:
```

For `TestRebuild` tests, do the same: add `@patch("vergil_tooling.bin.vrg_vm.update_plugins")` as the innermost (top) decorator and a trailing `_plugins: MagicMock` param before the fixtures.

- [ ] **Step 5: Run the full vrg_vm suite to verify it passes**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_vrg_vm.py -v`
Expected: PASS (all green — stage present, all pipeline tests patched).

- [ ] **Step 6: Commit**

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1603-claude-share-set
vrg-git add src/vergil_tooling/bin/vrg_vm.py tests/vergil_tooling/test_vrg_vm.py
vrg-commit --type feat --scope vrg-vm \
  --message "refresh Claude plugins on VM start and rebuild (#1603)" \
  --body "Add a warn-mode update-plugins lifecycle stage to the start and rebuild pipelines, beside update-tooling, so a started or rebuilt VM converges to the latest plugin versions. A failed refresh surfaces as a warning and never aborts the session.

Ref #1603"
```

---

## Task 6: Full validation and live-VM verification of the open item

**Files:** none (verification only).

This task closes the spec's one open item (first-launch install reliability) with real evidence, and runs the full gate. The unit suite mocks `limactl`, so the in-VM behavior must be confirmed on a real VM by a human (the agent cannot spin up host Lima VMs).

- [ ] **Step 1: Run the full validation gate**

Run: `vrg-container-run -- vrg-validate`
Expected: PASS — lint, typecheck, pytest, markdownlint (covers the new guide), and common checks all green. Fix anything that fails before proceeding.

- [ ] **Step 2: (Human, on a macOS host) Verify the plugin refresh on a live VM**

This step must be run by the human; the agent should hand off with these exact commands. From a host with a running agent VM `<vm>`:

```bash
# update_plugins uses: bash -c with an explicit PATH (NOT a login shell), because
# the VM's login shell is zsh and PATH comes from /etc/environment, not bash rc.
PE='export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"'

# 1. Confirm claude resolves under the exact invocation update_plugins uses:
limactl shell <vm> -- bash -c "$PE && command -v claude && claude --version"

# 2. Record current plugin state, run the refresh, record again:
limactl shell <vm> -- bash -c "$PE && claude plugin list"
vrg-vm update --identity <identity>     # or: vrg-vm update --all
limactl shell <vm> -- bash -c "$PE && claude plugin list"
```

Pass conditions:
- `command -v claude` resolves (Step 1 of this verification). **If it does not**, the explicit PATH in `update_plugins` is wrong for this VM — note where `command -v claude` actually points, update the PATH (or use the absolute path) in `lima.py`, update the `TestUpdatePlugins` assertions, and re-run Task 3's tests.
- `vrg-vm update` advances at least one plugin's version (or reports already-current), with no error from the `update-plugins` stage.

- [ ] **Step 3: (Human) Verify a rebuilt VM ends up with plugins installed**

```bash
vrg-vm rebuild --identity <identity>
limactl shell <vm> -- bash -lc 'claude plugin list'
```

Pass condition: the enabled plugins from `settings.json` are present after rebuild (whether via first-launch auto-install or the `update-plugins` stage).

- **If plugins are NOT installed after a fresh rebuild** (first-launch auto-install is not dependable headlessly): this is a follow-up — `update_plugins` updates already-installed plugins but does not install from scratch. File a follow-up issue to add an explicit install step (read `enabledPlugins`/`extraKnownMarketplaces` from `settings.json`, run `claude plugin marketplace add` + `claude plugin install <plugin>@<marketplace>` per enabled plugin) to the create/rebuild pipeline. Do **not** block this PR on it: the observed behavior to date is that auto-install works, and the version-skew bug (#1603's actual finding) is fixed by the refresh shipped here.

- [ ] **Step 4: Hand off for PR**

The agent writes `.vergil/pr-template.yml` (`issue: 1603`, `title`, `summary`, `notes`; `linkage` omitted/`Ref`). The human runs `vrg-submit-pr`. Do not run `vrg-submit-pr` as the agent.

---

## Self-review notes (for the implementer)

- **Spec coverage:** Finding 3 → Task 1. Finding 2 → Task 2. Finding 1 (refresh primitive → Task 3; folded into `vrg-vm update` → Task 4; start/rebuild stage → Task 5). Open item (first-launch install) → Task 6 verification with a concrete fallback. Rejected share-via-mount approach → no task, by design.
- **Test fragility:** Tasks 4 and 5 add calls into already-tested code paths. The plan calls this out explicitly and gives a `grep` to find every affected test, because a missed patch turns a unit test into a real `limactl` call. Do not skip the grep.
- **Type/name consistency:** `update_plugins(instance: str) -> None` is defined in Task 3 and used unchanged in Tasks 4 and 5. The stage is named `update-plugins` everywhere. Confirm the `Stage` field names in Task 5 Step 2 before asserting on them.
