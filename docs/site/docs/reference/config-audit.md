# GitHub Config Audit

`vrg-github-repo-config` audits a managed repo against the canonical
Vergil configuration and, in `apply` mode, reconciles it. It combines
two independent halves:

- **Local filesystem checks** (`audit_local_config`) — pure local file
  I/O, no network. They verify the files every managed repo checks in:
  `vergil.toml`, `CLAUDE.md`, `.claude/settings.json`,
  `.claude/hooks/guard.sh`, reusable-workflow pins, `.gitignore`,
  `.github/dependabot.yml`, and `.github/workflows/ops.yml`.
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
| `_check_claude_md` | `CLAUDE.md` has one pair of template markers, and the marked region carries the canonical consumer template verbatim |
| `_check_claude_md_length` | `CLAUDE.md` is at most 200 lines, template region included **(.github#558)** |
| `_check_agents_md` | no `AGENTS.md` exists **(.github#558)** |
| `_check_claude_settings` | `.claude/settings.json` equals the template after JSON parsing |
| `_check_workflow_refs` | every `vergil-*` reusable-workflow pin matches the `vergil.toml` version |
| `_check_gitignore` | `.gitignore` carries the correct `base + <language>` vergil-managed fence **(#311, #325)** |
| `_check_dependabot_yml` | `.github/dependabot.yml` is the canonical report-only render for the repo's language **(.github#420)** |
| `_check_required_workflows` | `ops.yml` is present, wires the audit, and is scheduled **(new, #311)** |

### `_check_claude_settings` — the whole file

`.claude/settings.json` must equal the packaged template,
[`claude_settings.json`](https://github.com/vergil-project/vergil-tooling/blob/develop/src/vergil_tooling/data/claude_settings.json),
after JSON parsing: key order and whitespace don't matter, but every key
and value does, and no other key may appear. The template holds the guard
`PreToolUse` hook, `permissions.allow: ["Bash(vrg-*)"]`,
`enabledPlugins: {"vergil@vergil-marketplace": true}` and
`attribution: {"commit": "", "pr": ""}`. It holds no
`extraKnownMarketplaces`: the marketplace is defined only in the user
settings (see the
[ownership model](../guides/claude-config-ownership.md)).

`attribution` turns off Claude Code's AI `Co-Authored-By` trailer
([#3200](https://github.com/vergil-project/vergil-tooling/issues/3200)).
Its empty-string form is required: `attribution: false` needs a newer
Claude Code and makes older versions skip the whole settings file.

Any other content is one
`DiffItem(field="local.claude_settings", ...)` whose actual value names
each top-level key that is missing, unexpected or different. Fix it by
copying the template into the repo.

### `CLAUDE.md` and `AGENTS.md` — the instruction file

`CLAUDE.md` must hold exactly one pair of template markers,
`<!-- vergil:template:claude-md:begin -->` and
`<!-- vergil:template:claude-md:end -->`. The lines between them, ignoring
leading and trailing blank lines, must equal the packaged template,
[`claude_md_consumer.md`](https://github.com/vergil-project/vergil-tooling/blob/develop/src/vergil_tooling/data/claude_md_consumer.md),
verbatim. Content outside the markers belongs to the repo and is never
compared. A file with no markers fails with
`DiffItem(field="local.claude_md", expected="well-formed template markers",
actual="no template markers")`; a region that differs names the first
divergent template line and `CLAUDE.md` line.

`CLAUDE.md` is held to a line budget of **200 lines**, counted as
`len(text.splitlines())` over the whole file, template region included: a
file of exactly 200 lines passes, 201 fails, and a trailing newline is not
an extra line. Over the budget is
`DiffItem(field="local.claude_md_length", expected="at most 200 lines",
actual="<N> lines")`. Don't meet it by moving content into `@imports`; see
the [ownership model](../guides/claude-config-ownership.md).

`AGENTS.md` must not exist: `CLAUDE.md` is the one instruction file. Any
`AGENTS.md`, a symlink included, is
`DiffItem(field="local.agents_md", expected="absent", actual="present")`.

These rules, and the settings check above, became hard failures with no
transition window once every managed repo had been swept under epic
[vergil-project/.github#558](https://github.com/vergil-project/.github/issues/558)
([#3728](https://github.com/vergil-project/vergil-tooling/issues/3728)).
The previous templates and the legacy no-marker form are gone.

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

The resolved language is `[project].primary-language`. No `vergil.toml`,
or no declared language, means base-only. A `vergil.toml` that fails to
load is **not** treated as base-only: the fence cannot be judged, so the
check reports one explicit failure,
`DiffItem(field="local.gitignore", expected="resolvable [project].primary-language",
actual="cannot resolve the fence language: <config error>")`, and nothing
else for the file (#3451).

The resolved language is the repo's `[project].primary-language`,
normalized to base-only for any language without a managed fragment — the
same rule scaffolding uses, so a freshly inited repo is fenced-compliant
by construction. The transitional legacy monolith-superset acceptance was
removed in epic
[#325](https://github.com/vergil-project/.github/issues/325), Task 10.

### `_check_dependabot_yml` — report-only Dependabot config

The repo's `.github/dependabot.yml` must be **exactly** the canonical
report-only render for its `[project].primary-language` (epic
[vergil-project/.github#420](https://github.com/vergil-project/.github/issues/420)):
a `github-actions` entry plus the language's ecosystem, each with
`open-pull-requests-limit: 0`. The comparison is an exact match with no
local additions — the same `lib/dependabot.py::check` that
`vrg-dependabot-sync --check` uses. Each failure is reported as
`DiffItem(field="local.dependabot", expected="canonical report-only dependabot.yml",
actual=<reason>)`, where the reason is `missing .github/dependabot.yml`
or `differs from the canonical render`. Fix either with
`vrg-dependabot-sync --write`.

No `vergil.toml`, or no declared language, means the actions-only
render. A `vergil.toml` that fails to load is **not** treated as
actions-only: the check reports one explicit failure,
`DiffItem(field="local.dependabot", expected="resolvable [project].primary-language",
actual="cannot resolve the dependabot language: <config error>")`, and
nothing else for the file (#3451).

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
like `.venv/` / `.worktrees/` / `.vergil/` / `.superpowers/`, paad's
`paad/security/` output, and all
build/validation/CI-evidence output including the mkdocs build path
`docs/site/site/` and the paths `vrg-container-docs` stages:
`docs/site/docs/changelog.md`, `docs/site/docs/releases/` and
`mkdocs.staged.yml`) plus the fragment for its `[project].primary-language`
(Python, TypeScript/Node, Go, C++; Rust and Java are managed with
an empty fragment). A repo that declares no fragment language gets the
base-only block.

**The managed fence.** The composed block is written into `.gitignore`
between a `# >>> vergil-managed: base + <language> …` begin marker and a
`# <<< vergil-managed <<<` end marker. The audit
(`lib/gitignore.py::check`) is **fenced-only**: it requires a well-formed
fence whose body equals the composed block for the repo's language
**exactly**, with no managed pattern left loose outside the fence, in the
exact layout `vrg-gitignore-sync --write` produces: the fence first and a
trailing newline, so the audit drifts exactly when `--write` would change
the file (#3568). (The
transitional legacy monolith-superset acceptance and the monolithic
`gitignore.baseline` asset were removed in epic
[#325](https://github.com/vergil-project/.github/issues/325), Task 10,
once the whole fleet was fenced.)

**Local additions are expected.** Genuinely repo-local lines live
*outside* the fence and are never asserted against; a repo freely adds
its own entries below the managed block (`--write` moves any above it to
below it, in order).

**Editing a fragment in vergil-tooling itself.** The installed
`vrg-gitignore-sync` composes from the installed package's data, so after
a fragment edit it reports "already in sync" and leaves this repo's own
fence stale (`vrg-validate` fails on it). Resync from the worktree's
source with
`PYTHONPATH=src python3 -m vergil_tooling.bin.vrg_gitignore_sync --write`.

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
place and leaves the genuinely repo-local section untouched. The fleet driver runs
that applicator across every repo as reviewed per-repo PRs; the rolling
pin delivers the new fragments to the tool, and the applicator writes the
fence.

## New repos are born conforming

`repo_init` (`vrg-github-repo-init`) scaffolds these checked-in files
through the same modules the audit uses, so a freshly created repo
passes their checks from day one:

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
- **`.github/dependabot.yml`** — the config-files step writes
  `lib/dependabot.py::render(<language>)`, the same render
  `_check_dependabot_yml` compares against, so a new repo passes that
  check from day one.

## GitHub checks — security settings

The GitHub half also compares the repo's security settings
(`desired_security_settings()` in `lib/github_config.py`) and reports
each difference as a `security.<field>` `DiffItem`:

| Field | Desired | Notes |
|---|---|---|
| `security.vulnerability_alerts` | `true` | Dependabot alerts on, for **every** repo, public or private |
| `security.dependabot_security_updates` | `disabled` | Dependabot never opens a security-update PR |
| `security.secret_scanning` | `enabled` | Only when GHAS is available; otherwise unmanaged |
| `security.secret_scanning_push_protection` | `enabled` | Only when GHAS is available; otherwise unmanaged |

GHAS is available when `[project].ghas` in `vergil.toml` says so, and
otherwise exactly when the repo is not private.

The first two rows are the **report-only Dependabot posture** (epic
[vergil-project/.github#420](https://github.com/vergil-project/.github/issues/420)):
alerts are on so the nightly
[`vrg-dependabot-audit`](dependabot-audit.md) has something to read,
and security-update PRs stay off. Version-update PRs are suppressed
separately, by `open-pull-requests-limit: 0` in the canonical
`.github/dependabot.yml`, which `_check_dependabot_yml` enforces (see
[Local checks](#local-checks)).

Alerts are read from `GET /repos/{repo}/vulnerability-alerts` (204 means
on, 404 means off). Any other status is an error, never a silent "off".
`apply` turns alerts on with `PUT` on that endpoint (or off with
`DELETE`), and sets `dependabot_security_updates` through the repo's
`security_and_analysis` settings. `apply` is a human action: it needs
Administration write access, which the org automation Apps do not
have.

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
  required-check set, so the ruleset stops drifting when the matrix changes. A
  repo with a `[package]` section additionally requires `package / evidence`,
  the single aggregate over its binary-package target matrix.
- **Unproducible-context check.** The audit asserts every required context is
  one the repo's workflows can actually produce. A leftover required leg that no
  workflow emits — for example a stale per-version check surviving a matrix
  reduction — is reported as drift rather than silently sitting "expected, never
  reported" and blocking every PR.
- **Apply-time producibility guard.** Before `apply` writes anything, it
  fetches the repo's committed `.github/workflows/ci.yml` from `develop` and
  refuses (exit 1) if the desired `CI gates` ruleset requires a context no
  `pull_request` job in that file emits — the same guard repo-init runs
  against the ci.yml it generates. A missing `ci.yml` is also a refusal, never
  a skip: with no workflow, no required check could ever report.
- **Classic branch-protection read + scoped cleanup.** The audit reads legacy
  *classic* branch protection in addition to the rulesets API. When it
  reconciles, it removes **only** the stale version-suffixed CI contexts the
  evidence ruleset now owns; every other classic setting (review requirements,
  push restrictions) is **reported, not touched**. The blast radius is minimal
  and intentional protections are preserved.

## GitHub checks — undeclared ruleset parameters

GitHub sometimes adds a new ruleset rule parameter and sets a default for
it on its side, as it did with
`require_extra_approval_for_unattributed_changes` on the `pull_request`
rule (#2860). When a live rule carries a parameter key that the desired
rule of the same `type` does not declare, the audit reports it as an
**undeclared parameter**, separately from value mismatches. The message
names the ruleset, the rule type, the key, and the live value:

```text
UNDECLARED PARAMETER: rulesets.Branch protection rule 'pull_request' has
'require_extra_approval_for_unattributed_changes' = True live, ...
```

- **It is non-compliant.** The live ruleset enforces a setting the spec
  never chose, so `audit` exits 1, the same as any other drift.
- **`apply` cannot fix it.** The ruleset `PUT` leaves out the key, so
  GitHub applies its default again. `apply` still fixes any value drift,
  writes nothing when the only issues are undeclared parameters, and
  exits 1 with a pointer to the decision.
- **Resolve it in the spec.** Either declare the key with an explicit
  value in the desired ruleset in `lib/github_config.py`, or add it to
  `_STRIP_RULE_PARAMS` if it should be ignored. Keys on that list
  (`do_not_enforce_on_create`, `dismissal_restriction`) are removed before
  comparison and never reported.

A whole live rule whose `type` the desired ruleset lacks is not an
undeclared parameter. It is reported as `.rules` value drift.

## Related

- [Consuming Repo Setup](../guides/consuming-repo-setup.md) — the
  consuming-repo managed `.gitignore` fence + `ops.yml` contract
- [CI Architecture](../guides/ci-architecture.md#every-gate-is-required-there-are-no-optional-pr-gates)
  — how the audit gates required-check drift and `vrg-release`
- [CLI Tools Overview](cli-tools-overview.md#vrg-github-repo-config) —
  runtime preconditions and failure modes
- [Claude Config Ownership](../guides/claude-config-ownership.md) — the
  ownership model the `CLAUDE.md` and `.claude/settings.json` checks
  enforce
