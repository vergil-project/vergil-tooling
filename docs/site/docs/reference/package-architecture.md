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
- **`trust.py`**: the trust layer, the one place a behaviour-driving read
  of GitHub content happens (a body, comments, reviews, edit history,
  event actors, closing references, or a PR found by its head branch).
  An identity holds the `human`, `agents` or `automation` role, or none
  ([Trusted authors](../guides/account-setup.md#trusted-authors)); a body
  or comment is trusted when its author and its editors hold a role; a
  close or reopen when its actor does. Readers return the trusted items
  and report each untrusted one as metadata only, or, where the content
  is load-bearing, stop. A read it cannot complete fails loudly, never
  as "trusted" or "nothing found", and a batch run stops the one item,
  not the run. `trusted_authors.py` loads the configuration,
  `trust_publish.py` publishes and checks its GitHub Actions copies, and
  `app_secrets.py` checks that the automation App's secrets reach each
  repo from its `automation` environment, with no repo- or org-level copy.
  The guard test, `tests/vergil_tooling/test_trust_guard.py`, scans the
  package and fails on any content read outside `trust.py` unless
  `tests/vergil_tooling/trust_guard_exemptions.toml` lists it with a
  reason: a query built at runtime with its fields declared
  (`[[registered]]`), content acted on only through the layer's answer
  (`[[judged]]`), a display-only read (`[[exempt]]`), or a read a tracked
  task moves onto the layer (`[[pending]]`). Adding an entry is a
  reviewed change.
- **`config.py`**: parse and validate `vergil.toml`.
- **`identity_mode.py`**: the identity resolver behind
  [`vrg-whoami`](cli-tools-overview.md#vrg-whoami); `platform_env.py`
  resolves the platform.
- **`epics.py`**: the epic/task model: the `StandingKind` table (the
  ad-hoc, intake, planning, deferred, hoshin and operations standing epics and their
  per-quarter archives), the epic categories and their derived category
  labels, epic and queue homes, sub-issue linking, rollup,
  and the parent-based check that `vrg-pr-workflow report-ready` and
  `vrg-submit-pr` use to refuse intake, planning and deferred items and the
  ops issues under an operations epic.
- **`epic_chain.py`**: an epic's
  [chain](../standards/github-issues.md#chained-epics) of containers past
  GitHub's 100-sub-issue limit: walking it to its head, first-fit linking,
  moving and unlinking with rollover, the capacity and depth checks,
  orphaned continuations, the race heals and closing a whole chain. Every
  caller outside `epics.py` and `epic_chain.py` links, moves and unlinks
  through it; the raw per-container reads and writes are private to the two
  modules.
- **`proxies.py`**: the proxy epic's link to its primary, read from the
  proxy's `Primary:` body line (from a body the trust layer trusts) and the
  primary's trusted `Proxy:` comments (`epics.is_proxy` is the label test every development-epic check skips).
- **`epic_audit.py`**, **`intake_route.py`** and **`queue_view.py`** build
  on `epics.py`: the `vrg-epic-audit` drift report and orphan pass, the
  `vrg-intake-route` mechanics and route table, and the `vrg-queue` views.
- **`epic_size.py`**: the epic-size scale, the sizing comment and cleared
  marker formats, and an item's size record, shared by the
  `vrg-intake-route` `size` route and the `vrg-queue` size column.
- **`ops_report.py`**: `vrg-ops-report`: the combined result of an
  unattended job, the lookup of its open ops issue through the trust layer
  (`trust.py`), and the failure, cancel and recovery reports. It never
  closes an issue.
- **`security_records.py`**: `vrg-security-record`, including the
  security-records pass of `vrg-security-record sync` (previewed by the
  read-only `vrg-epic-audit`).
- **`outsider_report.py`**: the outsider report of `vrg-epic-audit
  --untrusted`, the untrusted activity in a listed org's repos, built on the
  trust layer (`trust.py`).
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
  `index/` for the signed apt/dnf site. `repo_setup.dnf_refresh` refreshes
  a repository's dnf metadata through the cache window after an index
  publish ([Index generations and by-hash files](package-config.md#index-generations-and-by-hash-files)).
- **`host_*.py`**: `vrg-host`: manifest loading (`host_deps`),
  install-method handlers (`host_methods`), host pins (`host_pins`),
  classification and rendering (`host_status`), and version ordering
  (`host_versions`).
