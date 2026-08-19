# GitHub Config Audit

`vrg-github-repo-config` audits a managed repo against the canonical
Vergil configuration and, in `apply` mode, reconciles it. It combines
two independent halves:

- **Local filesystem checks** (`audit_local_config`) — pure local file
  I/O, no network. They verify the files every managed repo checks in:
  `vergil.toml`, `CLAUDE.md`, `.claude/settings.json`,
  `.claude/hooks/guard.sh`, reusable-workflow pins, `.gitignore`, and
  `.github/workflows/ops.yml`.
- **GitHub API checks** — repo settings, branch rulesets, and the
  required-status-check set, fetched from the GitHub API.

## The `audit` command

```bash
vrg-github-repo-config audit [--repo OWNER/REPO] [--config PATH]
```

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_github_repo_config` |
| Subcommands | `audit` (report), `diff` (report GitHub half only), `apply` (reconcile GitHub half) |
| Args | `--repo OWNER/REPO` (defaults to the current git remote), `--config PATH` (local `vergil.toml`) |
| Preconditions | Local checks require running from inside the repo's own checkout; GitHub checks require `gh` credentials |
| Exit codes | 0 compliant, 1 non-compliant (drift), 2 audit could not complete |
| Status | Active |

Local checks only run when the current directory *is* the audited
repo's checkout — auditing a foreign `--repo` from elsewhere skips the
local half with a warning rather than reading the wrong files.

### Where it runs — the nightly self-policing loop

The audit is not part of `vrg-validate` (that would add per-commit
overhead for a check that only needs to catch slow drift). Its home is
the **nightly `ops.yml` config-audit job**: every managed repo ships
`.github/workflows/ops.yml`, which calls the reusable
`vergil-project/vergil-actions/.github/workflows/ops-github-config.yml`
workflow on a daily schedule. That job runs
`vrg-github-repo-config audit` from inside the repo checkout, so any
`DiffItem` returns exit 1 and **turns the scheduled run red**. Drift is
fatal where it fires: a non-conforming repo cannot silently persist.

`vrg-release` also gates on the GitHub half of this audit, so a repo
whose required-check set has drifted cannot release until reconciled.

## Local checks

`audit_local_config` runs the following, appending a `DiffItem` for each
divergence:

| Check | Asserts |
|---|---|
| `_check_vergil_toml` | `vergil.toml` present and parseable |
| `_check_hook_guard_shim` | `.claude/hooks/guard.sh` present |
| `_check_claude_md` | `CLAUDE.md` carries the canonical consumer template verbatim |
| `_check_claude_settings` | `.claude/settings.json` marketplace + `enabledPlugins` match the template |
| `_check_workflow_refs` | every `vergil-*` reusable-workflow pin matches the `vergil.toml` version |
| `_check_gitignore` | `.gitignore` is a superset of the central baseline **(new, #311)** |
| `_check_required_workflows` | `ops.yml` is present, wires the audit, and is scheduled **(new, #311)** |

### `_check_gitignore` — baseline superset

Every non-comment, non-blank line of the central baseline (see
[The baseline](#the-baseline)) must appear **verbatim** as a line in the
repo's `.gitignore`. Matching is order-independent, and the repo may add
any number of extra local lines. A missing baseline pattern is reported
as `DiffItem(field="local.gitignore", expected=<pattern>,
actual="missing")`; a repo with no `.gitignore` fails with every
baseline pattern reported missing.

Matching is deliberately verbatim — no pattern-equivalence
normalization. `.venv/` and `.venv` (or a leading-slash variant) are
**not** treated as equivalent: the baseline defines the one canonical
spelling per pattern and the fleet is standardized to it.

### `_check_required_workflows` — ops.yml wiring

A **wiring validator** for a *present* `ops.yml`. It asserts the file
(a) exists, (b) references `ops-github-config.yml` (so a repo cannot
carry an `ops.yml` that omits the config audit), and (c) carries a
scheduled (`cron`) trigger — because a wired-but-unscheduled `ops.yml`
(e.g. `workflow_dispatch` only) would pass a bare wiring check yet never
run nightly, silently defeating the self-policing mechanism.

!!! note "Structural limit — a *missing* ops.yml is not caught here"
    Because this check runs *inside* the nightly `ops.yml` job, it
    cannot detect a repo that lacks `ops.yml` **entirely** — such a repo
    has no nightly run, so nothing executes the check there. The local
    validator keeps a *present* `ops.yml` honest; guaranteeing presence
    across the fleet needs a from-outside auditor, deferred to follow-on
    C ([#315](https://github.com/vergil-project/.github/issues/315)). In
    the meantime, presence is established by `repo_init` scaffolding (new
    repos) and the one-time rollout (existing repos).

## The baseline

The single source of truth for the baseline `.gitignore` is a packaged
data asset:

```text
src/vergil_tooling/data/gitignore.baseline
```

It is loaded at runtime via `importlib.resources` — the same idiom used
for the `CLAUDE.md` and `.claude/settings.json` templates — so
scaffolding and the audit share one definition and cannot diverge.

**The integral of the fleet's ignores.** The baseline is a single
integrated file applied to *every* repo regardless of language — the
union of universal categories (editors, OS, secrets, logs), Vergil
internals (`.venv/`, `.worktrees/`, `.vergil/`, `.superpowers/`), all
build/validation/CI-evidence output (including the mkdocs build path
`docs/site/site/`), and the managed-language artifacts across Python,
TypeScript/Node, Go, Ruby, and C++. There is **no per-language
branching**: one file goes everywhere.

**Comments and blank lines are documentation, not requirements.** Only
the pattern lines are matched; the baseline's comment blocks explain
intent and never need to appear in a consuming repo.

**Local additions are expected.** A repo `.gitignore` must be a
*superset*, so a repo freely adds its own entries below the baseline
patterns. The audit never asserts the file *equals* the baseline.

### Propagation — the rolling `vX.Y` pin

Every managed repo pins `vergil-tooling` to the **rolling major-minor
tag** `vX.Y`, never a specific patch. The nightly `ops.yml` job installs
`vergil-tooling@vX.Y`, which always resolves to the latest patch under
that line — including its `gitignore.baseline`. So:

> change the baseline → release a `vergil-tooling` patch under `vX.Y` →
> **every repo picks it up on its next nightly run, automatically** — no
> pin bump, no per-repo edit.

The rolling pin the fleet already uses *is* the propagation mechanism;
there is no push-based renderer or separate sync engine.

## New repos are born conforming

`repo_init` (`vrg-github-repo-init`) scaffolds both halves of the
contract, so a freshly created repo passes both new checks from day one:

- **`.gitignore`** — `render_gitignore()` reads the baseline asset (it
  no longer returns a hardcoded string), so a new repo starts as an exact
  superset of the canonical baseline.
- **`ops.yml`** — `render_ops_workflow()` writes
  `.github/workflows/ops.yml` calling `ops-github-config.yml@vX.Y` on a
  daily schedule. The scheduled **minute is staggered per repo** — a
  deterministic hash of `<org>/<repo>` maps to a minute in `[0,59]` — so
  the fleet does not stampede a single minute as it grows. The hour stays
  in the early-UTC window; only the minute varies.

## Related

- [Consuming Repo Setup](../guides/consuming-repo-setup.md) — the
  consuming-repo baseline + `ops.yml` contract
- [CI Architecture](../guides/ci-architecture.md#every-gate-is-required-there-are-no-optional-pr-gates)
  — how the audit gates required-check drift and `vrg-release`
- [CLI Tools Overview](cli-tools-overview.md#vrg-github-repo-config) —
  runtime preconditions and failure modes
