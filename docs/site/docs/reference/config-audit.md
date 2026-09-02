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
| `_check_gitignore` | `.gitignore` carries the correct `base + <language>` vergil-managed fence **(#311, #325)** |
| `_check_required_workflows` | `ops.yml` is present, wires the audit, and is scheduled **(new, #311)** |

### `_check_gitignore` — managed fence

The repo's `.gitignore` must carry the vergil-managed fence for its
resolved language (see
[The composed managed fence](#the-composed-managed-fence)). The check is
**fenced-only**: a well-formed fence must be present, its body must equal
`render_block(<language>)` **exactly**, and no managed-vocabulary pattern
may appear loose outside the fence. Each failed condition is reported as
`DiffItem(field="local.gitignore", expected="vergil-managed fence",
actual=<reason>)`; a repo with no `.gitignore` fails because no fence is
present. Genuinely repo-local lines outside the fence are never asserted
against.

The resolved language is the repo's `[project].primary-language`,
normalized to base-only for any language without a managed fragment — the
same rule scaffolding uses, so a freshly inited repo is fenced-compliant
by construction. The transitional legacy monolith-superset acceptance was
removed in epic
[#325](https://github.com/vergil-project/.github/issues/325), Task 10.

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

## The composed managed fence

The source of truth for the fleet's ignore vocabulary is a set of
packaged data-asset **fragments**:

```text
src/vergil_tooling/data/gitignore/base        # language-agnostic
src/vergil_tooling/data/gitignore/<language>  # one per managed language
```

They are loaded at runtime via `importlib.resources` — the same idiom
used for the `CLAUDE.md` and `.claude/settings.json` templates — and
`lib/gitignore.py` *composes* `base + <language>` into an order-stable,
de-duplicated pattern list. Scaffolding, the applicator
(`vrg-gitignore-sync`), and the audit all render through that one module,
so they cannot diverge.

**Composed per language.** A repo's block is the union of the
language-agnostic `base` (editors, OS, secrets, logs, Vergil internals
like `.venv/` / `.worktrees/` / `.vergil/` / `.superpowers/`, and all
build/validation/CI-evidence output including the mkdocs build path
`docs/site/site/`) plus the fragment for its `[project].primary-language`
(Python, TypeScript/Node, Go, Ruby, C++; Rust and Java are managed with
an empty fragment). A repo that declares no fragment language gets the
base-only block.

**The managed fence.** The composed block is written into `.gitignore`
between a `# >>> vergil-managed: base + <language> …` begin marker and a
`# <<< vergil-managed <<<` end marker. The audit
(`lib/gitignore.py::check`) is **fenced-only**: it requires a well-formed
fence whose body equals the composed block for the repo's language
**exactly**, with no managed pattern left loose outside the fence. (The
transitional legacy monolith-superset acceptance and the monolithic
`gitignore.baseline` asset were removed in epic
[#325](https://github.com/vergil-project/.github/issues/325), Task 10,
once the whole fleet was fenced.)

**Local additions are expected.** Genuinely repo-local lines live
*outside* the fence and are never asserted against or touched; a repo
freely adds its own entries above or below the managed block.

### Propagation — the rolling `vX.Y` pin

Every managed repo pins `vergil-tooling` to the **rolling major-minor
tag** `vX.Y`, never a specific patch. The nightly `ops.yml` job installs
`vergil-tooling@vX.Y`, which always resolves to the latest patch under
that line — including its fragment set. So:

> change a fragment → release a `vergil-tooling` patch under `vX.Y` →
> **every repo's nightly audit flags the fence as drifted** until its
> managed block is re-rendered.

Because the fence must match the composed block *exactly* (not merely be
a superset), a fragment change is applied — not auto-satisfied — by
re-running `vrg-gitignore-sync`, which rewrites the managed block in
place and leaves the repo-local section untouched. The fleet driver runs
that applicator across every repo as reviewed per-repo PRs; the rolling
pin delivers the new fragments to the tool, and the applicator writes the
fence.

## New repos are born conforming

`repo_init` (`vrg-github-repo-init`) scaffolds both halves of the
contract, so a freshly created repo passes both new checks from day one:

- **`.gitignore`** — `render_gitignore()` renders through
  `lib/gitignore.py` (the same module the audit uses), so a new repo is
  born with a correct `base + <language>` managed fence and passes the
  fenced-only audit by construction.
- **`ops.yml`** — `render_ops_workflow()` writes
  `.github/workflows/ops.yml` calling `ops-github-config.yml@vX.Y` on a
  daily schedule. The scheduled **minute is staggered per repo** — a
  deterministic hash of `<org>/<repo>` maps to a minute in `[0,59]` — so
  the fleet does not stampede a single minute as it grows. The hour stays
  in the early-UTC window; only the minute varies.

## GitHub checks — the required-status-check set

The GitHub half compares the repo's live branch-protection configuration
against the canonical CI-gates ruleset that
[`desired_ci_gates_ruleset()`](../guides/ci-evidence-convention.md#the-evidence-producing-gate-set)
computes from `vergil.toml`. Three properties matter for the dynamic,
version-agnostic CI model (epic
[vergil-project/.github#338](https://github.com/vergil-project/.github/issues/338)):

- **Version-agnostic required checks.** For the matrixed kinds the desired set
  requires the stable `audit / evidence`, `quality / evidence`, and
  `test / evidence` aggregates — never per-version legs such as
  `audit / dependencies / 3.12`. A `[ci].versions` change no longer churns the
  required-check set, so the ruleset stops drifting when the matrix changes.
- **Unproducible-context check.** The audit asserts every required context is
  one the repo's workflows can actually produce. A leftover required leg that no
  workflow emits — for example a stale per-version check surviving a matrix
  reduction — is reported as drift rather than silently sitting "expected, never
  reported" and blocking every PR.
- **Classic branch-protection read + scoped cleanup.** The audit reads legacy
  *classic* branch protection in addition to the rulesets API. When it
  reconciles, it removes **only** the stale version-suffixed CI contexts the
  evidence ruleset now owns; every other classic setting (review requirements,
  push restrictions) is **reported, not touched**. The blast radius is minimal
  and intentional protections are preserved.

## Related

- [Consuming Repo Setup](../guides/consuming-repo-setup.md) — the
  consuming-repo managed `.gitignore` fence + `ops.yml` contract
- [CI Architecture](../guides/ci-architecture.md#every-gate-is-required-there-are-no-optional-pr-gates)
  — how the audit gates required-check drift and `vrg-release`
- [CLI Tools Overview](cli-tools-overview.md#vrg-github-repo-config) —
  runtime preconditions and failure modes
