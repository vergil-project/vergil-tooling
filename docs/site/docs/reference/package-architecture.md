# Package Architecture

How the `vergil_tooling` Python package is laid out. Every `vrg-*` command
is a console script declared in `[project.scripts]` in `pyproject.toml`,
which is the complete list of entry points. The
[CLI Tools Overview](cli-tools-overview.md) documents each command.

## Layout

| Path | Holds |
|---|---|
| `src/vergil_tooling/bin/` | One module per console script (`vrg_commit.py` for `vrg-commit`, and so on) |
| `src/vergil_tooling/lib/` | Shared libraries the commands build on |
| `src/vergil_tooling/data/` | Packaged data: the label registry, the host-dependency manifest, the managed `CLAUDE.md` and `.claude/settings.json` templates, task-body scaffolds |
| `src/vergil_tooling/configs/` | Bundled tool configs: the canonical lint configs the common checks use, and the `git-cliff` changelog configs |

## Shared libraries

The main modules under `src/vergil_tooling/lib/`:

- **`git.py`**: git subprocess wrappers.
- **`github.py`**: `gh` CLI subprocess wrappers.
- **`config.py`**: parse and validate `vergil.toml`.
- **`identity_mode.py`**: the identity resolver behind
  [`vrg-whoami`](cli-tools-overview.md#vrg-whoami); `platform_env.py`
  resolves the platform.
- **`epics.py`**: the epic/task model: the `StandingKind` table (the
  ad-hoc, intake, planning and deferred standing epics and their
  per-quarter archives), epic and queue homes, sub-issue linking, rollup,
  and the parent-based queue-item check that `vrg-pr-workflow report-ready`
  and `vrg-submit-pr` use to refuse intake, planning and deferred items.
- **`epic_audit.py`**, **`intake_route.py`** and **`queue_view.py`** build
  on `epics.py`: the `vrg-epic-audit` drift report and orphan pass, the
  `vrg-intake-route` mechanics and route table, and the `vrg-queue` views.
- **`security_records.py`**: `vrg-security-record` and the security-records
  pass of `vrg-epic-audit --close`.
- **`release/`**: the mechanized release workflow behind `vrg-release`:
  preflight, prepare, merge, bump, confirm, finalize, handoff, the
  orchestrator, the package-index wait, and the `update-deps` stage.
  Preflight holds the dependency-update gate.
- **`update_deps/`**: the `vrg-update-deps` pipeline: preflight,
  orchestrator, the PR and the fix task, failure output, the held-back
  report, and registry and package-index lookups. Its updaters live under
  `updaters/`.
- **`package/`**: binary package builds for `vrg-package`: the matrix, the
  python and staged builders, nFPM, install-test, repository bootstrap, and
  `index/` for the signed apt/dnf site.
- **`host_*.py`**: `vrg-host`: manifest loading (`host_deps`),
  install-method handlers (`host_methods`), host pins (`host_pins`),
  classification and rendering (`host_status`), and version ordering
  (`host_versions`).
