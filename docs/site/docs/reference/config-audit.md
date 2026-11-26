# GitHub Config Audit

`vrg-github-repo-config` audits a managed repo against the canonical
Vergil configuration and, in [`update`](#the-update-command) mode,
reconciles it. It combines two independent halves:

- **Managed-file checks** (`audit_local_config`) — file I/O on a fresh
  shallow clone of the repo's `develop` branch. They verify the files
  every managed repo checks in:
  `vergil.toml`, `CLAUDE.md`, `.claude/settings.json`,
  `.claude/hooks/guard.sh`, reusable-workflow pins, `.gitignore`,
  `.github/dependabot.yml`, the per-language managed files (such as
  TypeScript's `tsconfig.vergil.json`), and `.github/workflows/ops.yml`.
- **GitHub API checks** — repo settings, branch rulesets, the
  required-status-check set and the `automation` environment, fetched
  from the GitHub API.

## The `audit` command

```bash
vrg-github-repo-config audit [--repo OWNER/REPO] [--config PATH] [--in-tree-templates]
```

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_github_repo_config` |
| Subcommands | `audit` (report), `diff` (report GitHub half only), [`update`](#the-update-command) (reconcile both halves), [`apply`](#the-apply-command-deprecated) (reconcile GitHub half; deprecated) |
| Args | `--repo OWNER/REPO` (defaults to the current git remote), `--config PATH` (local `vergil.toml`), `--in-tree-templates` (`audit` only, vergil-tooling only: see [Auditing vergil-tooling against its own templates](#auditing-vergil-tooling-against-its-own-templates)) |
| Preconditions | Local checks clone the repo's `develop` over HTTPS, so git's credential helper must reach it (`gh auth setup-git`); GitHub checks require `gh` credentials |
| Exit codes | `audit`: 0 compliant, 1 non-compliant (drift), 2 audit could not complete (including settings [not checked](#settings-the-identity-cannot-read)). `diff`: 0 whether or not it finds drift, 2 could not complete (including settings not checked). `update`: see [its exit codes](#update-exit-codes). `apply`: 0 converged, 1 left non-compliant (a write failed, an undeclared ruleset parameter, a branch named like a rolling tag, which must be deleted by hand, or managed-file drift), 2 could not complete or refused |
| Status | Active |

Every subcommand runs the managed-file checks against what is committed
on the repo's `develop`, never the current directory. It makes a shallow
clone (`git clone --depth 1 --branch develop`) into a temporary
directory, runs the checks there, and removes the clone when they end.
A dirty or unrelated working tree cannot change the result, and
`--repo OWNER/REPO` audits that repo's files from any directory. The
clone is plain `git` over HTTPS and authenticates through git's
credential helper (`gh auth setup-git`), so no token appears in argv or
the logs. A clone that fails exits 2, naming the repo.

### Auditing vergil-tooling against its own templates

vergil-tooling holds the master copies of the managed-file templates, and
a change to one updates vergil-tooling's own managed files in the same
PR. After any template change, its `develop` is ahead of the installed
release, so the installed templates would report drift that is not
there. `audit --in-tree-templates` checks the managed files of
vergil-tooling's `develop` with the templates and checks of the source
tree in that same clone, the ones the next release will ship. It runs
them in a child Python process whose `vergil_tooling` package is the
clone's `src/` tree, and the child refuses to run if the package was
loaded from anywhere else. The GitHub-side checks still run with the
installed release.

The flag refuses, with exit 2, a repo whose `develop` is not the
vergil-tooling project (its `pyproject.toml` must name
`vergil-tooling`). An in-tree audit that cannot complete, for example
because the source tree needs a dependency the installed release lacks,
also exits 2. `vrg-release` passes the flag when it releases
vergil-tooling; no other repo uses it.

## The `update` command

```bash
vrg-github-repo-config update [--repo OWNER/REPO] [--config PATH]
```

`update` brings a repo in line with the canonical configuration, both
halves of it (epic vergil-project/.github#1064). It runs these steps in
order:

1. **The identity gate.** `update` runs as the human or as the org
   automation App (identity mode `human` or `automation`). It refuses
   the AI agent modes, `user` and `audit`, by name, and exits 2. It also
   exits 2 when the identity cannot be resolved, for example when
   `VRG_AUTOMATION_APP` is set but the token belongs to another App.
   A run that passes logs the mode it resolved, such as
   `identity: human` or `identity: automation (<slug>[bot])`.
2. **The GitHub side.** The same audit and convergence as `apply`. For
   each managed ruleset, `update` logs the bypass actors it read back,
   one `<actor_type> <actor_id> <bypass_mode>` per actor (`none` for an
   empty list), or why the list was not read: skipped, not checked, or
   the ruleset absent. A
   setting that is [not checked](#settings-the-identity-cannot-read),
   such as an empty `bypass_actors` read under the automation identity,
   stops the run with exit 2 before anything is written or cloned. So
   does a `bypass_actors` field skipped as invisible to an App token,
   since `update` converges only what it has audited. A write that
   fails also exits 2.
3. **The managed files.** A fresh shallow clone of `develop`, in which
   every fixer runs: the hook-guard shim (mode 0755 included), the
   CLAUDE.md template region, `.claude/settings.json`, the `.gitignore`
   managed fence, `.github/dependabot.yml`, the per-language managed
   files and the `vergil-*` workflow pins. Each fixer shares its definition of "correct" with its check.
   A fixer that cannot compute a fix (a CLAUDE.md with no template
   markers, for example) is reported and does not stop the others.
4. **One sync PR.** Whatever the fixers changed is committed on a new
   `automation/repo-config-sync/<run-id>` branch and opened as one PR
   against `develop`, then armed through the one PR path, which turns
   squash auto-merge on, pinned to the pushed commit, and reads it back.
   When GitHub refuses auto-merge because the PR can already merge, the
   path merges it directly and `update` does not wait. A PR the path
   refuses to arm fails the run, naming the PR and the path's reason.
   An earlier run's sync PR is closed and its branch deleted first,
   whether or not there is new drift. With no change, no PR is opened.
5. **Wait until merged.** `update` then watches the PR until it has
   merged. The watch never merges the PR itself: GitHub's auto-merge
   merges it once its checks are green, and a PR whose auto-merge is
   off is waited for until the deadline. `develop` does not require a
   branch to be up to date, so a PR merges from its base once its own
   checks pass. Only where `develop` still requires an up-to-date
   branch is a PR that falls behind updated, up to 5 times. A failed
   check, a conflict, a PR closed by someone else, the update cap or
   the deadline ends the watch, naming the PR and the reason. The
   deadline is `$VRG_MECHANIZED_PR_DEADLINE` seconds, 2400 (40
   minutes) by default, so the watch ends inside the one hour the ops
   job's App installation token lasts.
6. **The verdict.** The fixed clone is audited again. A check that has
   no fixer (a missing or invalid `vergil.toml`, a CLAUDE.md over the
   length limit, an `AGENTS.md`, a missing or unwired `ops.yml`) still
   fails the run, and the PR body lists it for a human.

`update` never reads or touches the caller's working checkout, so a
human can run `update --repo OWNER/REPO` from anywhere. Once the sync
PR has merged, `vrg-finalize-pr --cleanup-only` brings a local `develop` up
to date.

### `update` exit codes

| Exit | Meaning |
|---|---|
| 0 | The GitHub side converged and the managed files are in sync: no sync PR was needed, or the one opened this run merged |
| 1 | Drift that could not be fixed: a check with no fixer, a fix that cannot be computed, an undeclared ruleset parameter, a branch named like a rolling tag, which must be deleted by hand, or a workflow GitHub disabled for inactivity, which must be re-enabled by hand. The sync PR for the fixable drift is still opened, and exit 1 applies once it has merged |
| 2 | Something could not be audited or converged: an agent identity, a setting not checked, a failed GitHub write, clone or PR, or a sync PR that did not merge (the error names the PR and the reason). 2 outranks 1. A setting not checked or a failed write opens nothing |

## The `apply` command (deprecated)

```bash
vrg-github-repo-config apply [--repo OWNER/REPO] [--config PATH]
```

`apply` is deprecated in favour of [`update`](#the-update-command), and
prints a notice on stderr naming it. Its behaviour is unchanged: it
converges only the GitHub side and reports managed-file drift without
fixing it, and it has no identity gate. Since it shares its code path
with `update`, it also converges the auto-merge setting and the
`Automation branches` ruleset.

## Where it runs — the nightly self-policing loop

The config check is not part of `vrg-validate` (that would add
per-commit overhead for a check that only needs to catch slow drift).
Its home is the **nightly `ops.yml` `github-config` job**: every managed
repo ships `.github/workflows/ops.yml`, which calls the reusable
`vergil-project/vergil-actions/.github/workflows/ops-github-config.yml`
workflow on a daily schedule. That job runs
`vrg-github-repo-config update` on the repo as the org automation App
(identity mode `automation`), with a token scoped to the calling repo.
It runs only from `develop`, checks out `develop`, and is capped at 60
minutes, the life of the App's installation token (see the vergil-actions
[ops-github-config](https://vergil-project.github.io/vergil-actions/latest/workflows/ops-github-config/)
reference).

Drift that a fixer can correct arrives as one sync PR, which merges
within the run. Anything `update` cannot converge, fix or audit gives a
non-zero [exit code](#update-exit-codes) and **turns the scheduled run
red**, so a non-conforming repo cannot silently persist.

`vrg-release` also runs `audit` before it releases, so a repo whose
configuration has drifted cannot release until it is reconciled.

In the org's own `.github` repository only, the same workflow's
`trusted-authors-check` job runs
[`vrg-trusted-authors check`](cli-tools-overview.md#vrg-trusted-authors)
for the whole org, as the automation App: a missing, invalid or not
`all`-visible `VRG_TRUSTED_AUTHORS` or `VRG_AUTOMATION_APP`, or a
repository-level variable of either name, turns that run red. It uploads
the values it read as the `trusted-authors-values` artifact, which the
morning review compares with `vrg-trusted-authors render` (see
[Trusted authors](../guides/account-setup.md#trusted-authors)).

### Settings the identity cannot read

Several GitHub reads need the `administration=read` repository
permission: `vulnerability-alerts`, the three `actions/permissions`
endpoints, classic branch protection, and the `security_and_analysis`
block of the repo object (GitHub leaves the block out for an identity
without admin access). The agent App does not hold that permission
(#3816). When a read fails with a 403 naming a missing permission, the
audit does not guess the value. It reports the setting as not checked:

```text
  vergil-project/vergil-tooling: NOT FULLY AUDITED (no drift in the settings read; 10 not checked)
    security.vulnerability_alerts: not checked (needs administration=read)
    classic_protection.develop: not checked (needs administration=read)
```

- **It is not drift.** An unread setting is never compared, so it never
  shows up as a `DiffItem`.
- **It is not a pass.** `audit` exits 2 with a one-line error naming
  the permission, unless it also found drift in the settings it did
  read, in which case it exits 1. It never exits 0 while any setting
  is not checked. `diff`, which otherwise always exits 0, exits 2 with
  the same error.
- **`update` and `apply` refuse.** They exit 2 and write nothing, since
  they would converge settings they could not read.
- **The check for an `update` is the `update` exiting 0.** `update` is
  run by the human or the org automation App, never by an agent. Under
  the agent identity, `diff` exits 2 on every repo, since the
  `administration=read` settings are not checked, so a `diff` an agent
  runs after an `update` is informational only and its exit code is not
  the verdict on the update.
- A repo setting that `GET /repos/{owner}/{repo}` leaves out of its
  response, or returns as `null`, is not checked either, as
  `repo_settings.<field>`, never read as `false` or `''` (#4322).
  GitHub leaves the merge settings out for a token without Contents
  read and write: its
  [Get a repository](https://docs.github.com/en/rest/repos/repos#get-a-repository)
  docs say viewing merge-related settings needs `contents:read` and
  `contents:write` (vergil-project/vergil-actions#1164). So the line
  for a missing merge setting says
  `needs contents=read and contents=write` (#4326). The docs state no
  requirement for any other field, so the line for one of those says
  `needs a token GitHub returns the field to`. A repo setting of the
  wrong type is an error.
- A setting the desired state leaves unmanaged (a ruleset's
  `bypass_actors` under a token that cannot see them) is skipped as
  before, not listed as not checked.
- A 403 that names no missing permission (a rate limit, for example) is
  still an error, as is any other unexpected status.
- **An empty bypass list under the automation identity is not checked.**
  GitHub answers a token that cannot see a ruleset's `bypass_actors`
  with an empty list, not an error. The automation App's token is meant
  to read the field (it holds Administration write), so under the
  automation identity a ruleset whose desired bypass list is non-empty
  and which reads back empty is reported as not checked, never as drift
  (epic vergil-project/.github#1064):

  ```text
      rulesets.Tag protection.bypass_actors: not checked (read back empty under the automation identity, so unreadable; needs administration=write)
  ```

  `update` then refuses and converges nothing. For a human run the same
  empty list is ordinary drift, and `update` writes the desired list.

## Local checks

`audit_local_config` runs the following, appending a `DiffItem` for each
divergence:

| Check | Asserts |
|---|---|
| `_check_vergil_toml` | `vergil.toml` present and parseable |
| `_check_hook_guard_shim` | `.claude/hooks/guard.sh` present, readable, and byte-identical to the packaged hook guard shim **(#4109)** |
| `_check_claude_md` | `CLAUDE.md` has one pair of template markers, and the marked region carries the canonical consumer template verbatim |
| `_check_claude_md_length` | `CLAUDE.md` is at most 200 lines, template region included **(.github#558)** |
| `_check_agents_md` | no `AGENTS.md` exists **(.github#558)** |
| `_check_claude_settings` | `.claude/settings.json` equals the template after JSON parsing |
| `_check_workflow_refs` | every `vergil-*` reusable-workflow pin matches the `vergil.toml` version |
| `_check_gitignore` | `.gitignore` carries the correct `base + <language>` vergil-managed fence **(#311, #325)** |
| `_check_dependabot_yml` | `.github/dependabot.yml` is the canonical report-only render for the repo's language **(.github#420)** |
| `_check_language_files` | each managed file of the repo's language is its packaged master, byte for byte **(.github#1174)** |
| `_check_required_workflows` | `ops.yml` is present, wires the audit, and is scheduled **(new, #311)** |

### `_check_claude_settings` — the whole file

`.claude/settings.json` must equal the packaged template,
[`claude_settings.json`](https://github.com/vergil-project/vergil-tooling/blob/develop/src/vergil_tooling/data/claude_settings.json),
after JSON parsing: key order and whitespace don't matter, but every key
and value does, and no other key may appear. The template holds the guard
`PreToolUse` hook, `permissions.allow: ["Bash(vrg-*)"]`,
`enabledPlugins: {"vergil@vergil-marketplace": true}`,
`attribution: {"commit": "", "pr": ""}` and `idleCompaction: false`. It
holds no
`extraKnownMarketplaces`: the marketplace is defined only in the user
settings (see the
[ownership model](../guides/claude-config-ownership.md)).

`attribution` turns off Claude Code's AI `Co-Authored-By` trailer
([#3200](https://github.com/vergil-project/vergil-tooling/issues/3200)).
Its empty-string form is required: `attribution: false` needs a newer
Claude Code and makes older versions skip the whole settings file.

`idleCompaction: false` stops Claude Code compacting a session while it
sits idle
([#3795](https://github.com/vergil-project/vergil-tooling/issues/3795)),
so the human decides when a session is compacted or cleared. It needs
Claude Code v2.1.290 or later; older versions ignore the key.
Context-full auto-compaction (`autoCompactEnabled`) is left on, and
manual `/compact` still works.

Any other content is one
`DiffItem(field="local.claude_settings", ...)` whose actual value names
each top-level key that is missing, unexpected or different.
[`update`](#the-update-command) fixes it by replacing the file with the
template.

### `_check_hook_guard_shim` — the whole file

`.claude/hooks/guard.sh` must match the packaged shim,
[`hook_guard_shim.sh`](https://github.com/vergil-project/vergil-tooling/blob/develop/src/vergil_tooling/data/hook_guard_shim.sh),
byte for byte. The packaged shim fails closed: when `vrg-hook-guard` is
not installed, its `jq` fallback refuses input it cannot check. An older
copy that lets such input through, a local edit, or a CRLF copy is drift.
The check reports one `DiffItem(field="local.hook_guard_shim", ...)`:
`missing` when the file is absent, `unreadable (…)` when it cannot be
read, `differs from the packaged shim (stale or locally edited)`, or,
for the right bytes without every execute bit, `not executable (mode
…)`: Claude Code cannot run a hook that is not executable.

[`update`](#the-update-command) fixes it by replacing the file with the
packaged shim, mode `0755`.

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
[`update`](#the-update-command) fixes a differing region by replacing
only the marked region with the template. It does not repair missing or
malformed markers: that stays a hand fix.

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
local additions, through `lib/dependabot.py::check`. Each failure is reported as
`DiffItem(field="local.dependabot", expected="canonical report-only dependabot.yml",
actual=<reason>)`, where the reason is `missing .github/dependabot.yml`
or `differs from the canonical render`.
[`update`](#the-update-command) fixes either by writing the render.

No `vergil.toml`, or no declared language, means the actions-only
render. A `vergil.toml` that fails to load is **not** treated as
actions-only: the check reports one explicit failure,
`DiffItem(field="local.dependabot", expected="resolvable [project].primary-language",
actual="cannot resolve the dependabot language: <config error>")`, and
nothing else for the file (#3451).

### `_check_language_files` — per-language managed files

Some files are managed whole for the repos of one
`[project].primary-language`, each a byte-for-byte copy of a master in
vergil-tooling's package data (epic
[vergil-project/.github#1174](https://github.com/vergil-project/.github/issues/1174)).
`LANGUAGE_MANAGED_FILES` in `lib/repo_config.py` lists them; another
language adds managed files with an entry there. The masters stay where
the gates read them, so there is one copy of each.

| Language | File in the repo | Master |
|---|---|---|
| `typescript` | `tsconfig.vergil.json` | `configs/typescript/tsconfig.base.json` |
| `typescript` | `eslint.vergil.config.mjs` | `configs/typescript/eslint.config.mjs` |
| `typescript` | `.prettierrc.json` | `configs/typescript/prettier.config.json` |

The repo never edits these files. It owns its entry points
(`tsconfig.json`, `eslint.config.mjs`), which build on them.

A missing file, or one whose bytes differ from the master (a stale or
locally edited copy, or one with other line ends), is one
`DiffItem(field="local.language_files", expected="<file>: matches the master (configs/<master>)",
actual="<file>: missing" | "<file>: differs from the master (stale or locally edited)")`.
[`update`](#the-update-command) writes the master whole, keeping the
file's mode, or 0644 for a new file.

A repo of any other language, or with no `vergil.toml`, has no such
files, and a file of the same name in it is left alone. A `vergil.toml`
that fails to load is reported as one
`DiffItem(field="local.language_files", expected="resolvable [project].primary-language",
actual="cannot resolve the managed-file language: <config error>")`,
which `update` cannot fix (#3451).

### `_check_required_workflows` — ops.yml wiring

A **wiring validator** for a *present* `ops.yml`. It asserts the file
(a) exists, (b) references `ops-github-config.yml` (so a repo cannot
carry an `ops.yml` that omits the nightly config job), and (c) carries a
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
de-duplicated pattern list. Scaffolding, the `update` fixer and the
audit all render through that one module, so they cannot diverge.

**Composed per language.** A repo's block is the union of the
language-agnostic `base` (editors, OS, secrets, logs, Vergil internals
like `.venv/` / `.worktrees/` / `.vergil/` / `.superpowers/`, paad's
`paad/security/` output, and all
build/validation/CI-evidence output including the mkdocs build path
`docs/site/site/` and the paths `vrg-container-docs` stages:
`docs/site/docs/changelog.md`, `docs/site/docs/releases/` and
`mkdocs.staged.yml`) plus the fragment for its `[project].primary-language`
(Python, TypeScript/Node, Go, C++, Rust). A repo that declares no fragment language gets the
base-only block.

**The managed fence.** The composed block is written into `.gitignore`
between a `# >>> vergil-managed: base + <language> …` begin marker and a
`# <<< vergil-managed <<<` end marker. The audit
(`lib/gitignore.py::check`) is **fenced-only**: it requires a well-formed
fence whose body equals the composed block for the repo's language
**exactly**, with no managed pattern left loose outside the fence, in the
exact layout the `update` fixer writes: the fence first and a
trailing newline, so the audit drifts exactly when the fixer would change
the file (#3568). (The
transitional legacy monolith-superset acceptance and the monolithic
`gitignore.baseline` asset were removed in epic
[#325](https://github.com/vergil-project/.github/issues/325), Task 10,
once the whole fleet was fenced.)

**Local additions are expected.** Genuinely repo-local lines live
*outside* the fence and are never asserted against; a repo freely adds
its own entries below the managed block (the fixer moves any above it to
below it, in order).

**Editing a fragment in vergil-tooling itself.** The installed
`vrg-github-repo-config update` composes from the installed package's
data, so after a fragment edit this repo's own fence stays stale
(`vrg-validate` fails on it) until the change is released. Resync from
the worktree's source by running the fixer from `src/` at the worktree
root:
`PYTHONPATH=src python3 -c 'from pathlib import Path; from vergil_tooling.lib.repo_config_fixes import fix_gitignore; fix_gitignore(Path("."))'`.

### Propagation — the rolling `vX.Y` pin

Every managed repo pins `vergil-tooling` to the **rolling major-minor
tag** `vX.Y`, never a specific patch. The nightly `ops.yml` job installs
`vergil-tooling@vX.Y`, which resolves to the release the rolling tag
names, the latest patch under that line unless a rollback moved it
back — including its fragment set. So:

> change a fragment → release a `vergil-tooling` patch under `vX.Y` →
> **every repo's nightly `update` finds the fence drifted** and opens a
> sync PR that re-renders its managed block.

Because the fence must match the composed block *exactly* (not merely be
a superset), a fragment change is applied — not auto-satisfied — by
the [`update`](#the-update-command) fixer, which rewrites the managed
block in place and leaves the genuinely repo-local section untouched. The
nightly ops run's `update` does this in every repo, one sync PR each; the
rolling pin delivers the new fragments to the tool, and the fixer writes
the fence.

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
- **The per-language managed files** — the config-files step writes the
  language's entries in `LANGUAGE_MANAGED_FILES` from their masters, so
  a new TypeScript repo passes `_check_language_files` from its first
  commit, with no wait for the nightly sync.

## GitHub checks — security settings

The GitHub half also compares the repo's security settings
(`desired_security_settings()` in `lib/github_config.py`) and reports
each difference as a `security.<field>` `DiffItem`:

| Field | Desired | Notes |
|---|---|---|
| `security.vulnerability_alerts` | `true` | Dependabot alerts on, for **every** repo |
| `security.dependabot_security_updates` | `disabled` | Dependabot never opens a security-update PR |
| `security.secret_scanning` | `enabled` | For every repo |
| `security.secret_scanning_push_protection` | `enabled` | For every repo |

The first two rows are the **report-only Dependabot posture** (epic
[vergil-project/.github#420](https://github.com/vergil-project/.github/issues/420)):
alerts are on so the nightly
[`vrg-dependabot-audit`](dependabot-audit.md) has something to read,
and security-update PRs stay off. Version-update PRs are suppressed
separately, by `open-pull-requests-limit: 0` in the canonical
`.github/dependabot.yml`, which `_check_dependabot_yml` enforces (see
[Local checks](#local-checks)).

Alerts are read from `GET /repos/{repo}/vulnerability-alerts` (204 means
on, 404 means off). A 403 for a missing permission is reported as
[not checked](#settings-the-identity-cannot-read); any other status is an
error, never a silent "off".
`update` (like the deprecated `apply`) turns alerts on with `PUT` on that
endpoint (or off with `DELETE`), and sets `dependabot_security_updates`
through the repo's `security_and_analysis` settings. That needs
Administration write access, which the human and the org automation
Apps hold and the agent Apps do not.

## GitHub checks — rulesets

The GitHub half compares the repo's live rulesets against the desired set
that `compute_desired_state()` in `lib/github_config.py` declares, in this
order:

| Ruleset | Target | Includes | Rules | Bypass |
|---|---|---|---|---|
| `Branch protection` | branch | `refs/heads/main`, `refs/heads/develop` | `deletion`, `non_fast_forward`, `pull_request` | none |
| `Tag protection` | tag | `refs/tags/v*.*.*`, `refs/tags/v*.*` | `creation`, `deletion`, `non_fast_forward`, `update` | the repository Admin role (`RepositoryRole`, `actor_id` 5) and the org automation App (`Integration`, its App ID) |
| `Rolling tag branch names` | branch | `refs/heads/v[0-9]*` | `creation`, `update`, `non_fast_forward` | none |
| `Automation branches` | branch | `refs/heads/automation/**` | `creation`, `update`, `non_fast_forward` | the org automation App (`Integration`, its App ID) and the repository Admin role (`RepositoryRole`, `actor_id` 5) |
| `Main updates` | branch | `refs/heads/main` | `update` | the repository Admin role (`RepositoryRole`, `actor_id` 5) |
| `Agent audit markers` | branch | `refs/heads/vergil/agent-audit/*` | `update`, `creation`, `deletion` | the repository Admin role (`RepositoryRole`, `actor_id` 5) and the org automation App (`Integration`, its App ID) |
| `CI gates (develop)` | branch | `refs/heads/develop` | `required_status_checks` (see below), not strict | none |
| `CI gates (main)` | branch | `refs/heads/main` | `required_status_checks` (see below), strict | none |

- **CI gates** is one ruleset per branch, both requiring the same checks.
  `develop`'s does not require a branch to be up to date before it merges
  (`strict_required_status_checks_policy` is `false`), so ready PRs into
  `develop` can merge in parallel; `main`'s does (`true`). A
  `docs-single-branch` repo has no `main`, so it gets `CI gates (develop)`
  only
  ([epic vergil-project/.github#1218](https://github.com/vergil-project/.github/issues/1218)).
  A repo still carrying the older shared `CI gates` ruleset reports it as
  drift, replaced by the pair, and `update` writes the pair before it
  deletes the old ruleset, so neither branch is ever left without required
  checks.

- **Tag protection** covers both the `vX.Y.Z` release tags and the rolling
  `vX.Y` tags consumers follow as their deploy ref, so only the repository
  Admin role and the org automation App can create, move or delete either.
  The shared release action pushes `vX.Y.Z` with the App's installation
  token, and the human moves `vX.Y` with `vrg-promote`. The GitHub Actions
  integration is never a bypass actor, so no workflow's `GITHUB_TOKEN` can
  create a release tag.
- **Rolling tag branch names** blocks creating any branch named like a rolling
  tag (a `v` followed by a digit). Some consumers, Claude Code among them,
  resolve a ref such as `v2.1` as a branch before a tag (git's own short-name
  lookup prefers the tag), so for them the tag's protection holds only while
  no such branch can exist. The ruleset also blocks updates and force-pushes
  to such a branch, but not its deletion, so a stray one can be removed.
  The audit lists the repo's branches named like a rolling tag and reports
  each as drift, `rolling_tag_branches.<name>`; `apply` never deletes a
  branch, so it exits 1 until a human removes it.
  Names such as `vm-…` are not covered. GitHub's ruleset ref matching
  uses `fnmatch` syntax and supports character sets in square brackets
  ([Using `fnmatch` syntax](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/creating-rulesets-for-a-repository#using-fnmatch-syntax)).
  A pattern include names no single branch, so the classic branch-protection
  read skips it.
- **Automation branches** keeps every identity but the org automation App
  and the human admin off the `automation/` branches that mechanized jobs
  push to. Their PRs merge through GitHub auto-merge, which the repo
  settings allow (`allow_auto_merge` is `true`), so without the ruleset an
  agent with Contents write could push to such a branch and have its
  content merged with no human in the loop. Deletion is not restricted:
  the branches are temporary. The App's numeric ID differs per org. The
  tooling reads the App's slug from `$VRG_AUTOMATION_APP` in a job of the
  same org, and otherwise (a human run, or a job of another org) from the
  org's `[orgs.<org>]` stanza of the
  [trusted-authors configuration](../guides/account-setup.md#trusted-authors),
  never from the API, and looks the ID up with `GET /apps/{slug}`. An org
  with no stanza, a missing configuration or a failed lookup fails the run
  (exit 2).
- **Main updates** lets only the human admin update `main`. A pull
  request's merge updates its base branch, and GitHub's `update` rule
  allows that only to a bypass actor, so no other identity can merge into
  `main`, whatever pull-request permission it holds. A GitHub App's
  permissions cover a whole repository, never one branch, so this is what
  keeps an agent that may merge its own PRs into `develop` out of `main`
  ([epic vergil-project/.github#1384](https://github.com/vergil-project/.github/issues/1384),
  spec §4.1). The org automation App is not a bypass actor: `vrg-release`
  refuses every agent identity, so the human merges every release PR. Its
  `merge-release` stage asks for that bypass explicitly (`gh pr merge
  --admin`, pinned with `--match-head-commit`) once its own checks pass,
  since a plain merge into `main` is refused even for the admin; it is the
  only merge that does
  ([vergil-project/vergil-tooling#5095](https://github.com/vergil-project/vergil-tooling/issues/5095)).
  The ruleset has no `creation` rule, so repo init can still create `main`.
- **Agent audit markers** keeps the `vergil/agent-audit/` marker branches,
  which record where the daily agent audit has checked up to, out of reach
  of the identity being audited. Only the human admin (whose repo init
  creates them) and the org automation App (which runs the audit) can
  create, move or delete one.
- **Bypass actors are not compared under an App token that is not the
  automation identity**, such as an agent App's token or a CI
  `GITHUB_TOKEN`. Such a token cannot read a ruleset's bypass list, so in
  that mode the audit checks each ruleset's includes, enforcement and
  rules but not its bypass actors, and lists the skipped
  `rulesets.<name>.bypass_actors` fields instead. This holds for every
  ruleset above, and `update` refuses to converge on such a token. Under
  the human's login and the automation identity they are compared, and
  under the automation identity an empty read is
  [not checked](#settings-the-identity-cannot-read). `update` and `apply`
  write whole rulesets, so they always manage the bypass lists. Bypass lists are
  compared as sets: their order is not drift.
- **A REST refs write refused by a ruleset gives no rule text.** When a
  ruleset blocks a create, update or delete made through the REST refs API
  (`POST`, `PATCH` or `DELETE` on `repos/{owner}/{repo}/git/refs…`), GitHub
  answers only `422 Reference update failed`. The same write done as a
  `git push` names the rule (`GH013: Repository rule violations found for
  refs/heads/v0.0. - Cannot create ref due to creations being restricted.`).
  The 422 is generic, since a stale SHA gives the same answer, so every tool
  that calls the API through `lib/github.py` adds a note to such a failure: it
  names the ruleset as the likely cause, the ref and the repo, and how to see
  the rules (`gh api repos/{owner}/{repo}/rules/branches/{branch}` for a
  branch, `gh api repos/{owner}/{repo}/rulesets`, or this audit). The error is
  still raised. To diagnose a refusal, or to probe whether a ruleset blocks a
  write, use `git push`, which prints the rule.

## GitHub checks — the automation environment

Every repo gets an `automation` deployment environment, declared by
`desired_automation_environment()` in `lib/github_config.py`
([epic vergil-project/.github#1384](https://github.com/vergil-project/.github/issues/1384)).
Every secret a workflow reads that can write or act, the org automation
App's `APP_CLIENT_ID` and `APP_PRIVATE_KEY` first, lives only in this
environment, never at org or repo level, and every job that needs one
declares `environment: automation`. The environment's deployment branch
policy admits only `develop` and `main` (releases run on `main`), so a
workflow running on any other branch, such as an agent's feature branch,
runs without those secrets.

| Field | Desired |
|---|---|
| Deployment branch policy | custom branch policies (not "all branches", not "protected branches") |
| Custom branch policies | exactly `develop` and `main` |
| Custom tag policies | none |
| Required reviewers | none |
| Wait timer | 0 |

- **Each field is compared on its own**, as
  `environments.automation.<field>`. A missing environment is one item,
  `environments.automation: expected='present', actual='missing'`. A
  policy such as `*` shows up as drift on `custom_branches`. Other
  environments in the repo are not read or compared.
- **Reads need `actions=read`.** GitHub's
  [permissions required for GitHub Apps](https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps)
  lists the environment read and its `deployment-branch-policies`
  listing under Actions (read). A permission-gap 403 on the environment
  is reported as `environments.automation` not checked; one on the
  listing as `environments.automation.custom_branches` and
  `.custom_tags` not checked. A 404 is a missing environment. Any other
  error, a malformed answer or a protection rule of a type the audit does
  not know fails the audit rather than read as converged.
- **`update` writes it whole**, which needs Administration write: a
  `PUT` of the environment (no reviewers, no wait timer, custom branch
  policies), then a `POST` of each missing branch policy before a
  `DELETE` of each stray one, so the policy set never passes through
  empty. `repo-init` creates it the same way.
- A `docs-single-branch` repo gets the same policy: one naming a branch
  the repo does not have admits nothing.

## GitHub checks — the required-status-check set

The GitHub half compares the repo's live branch-protection configuration
against the canonical CI-gates rulesets that
[`desired_ci_gates_rulesets()`](../guides/ci-evidence-convention.md#the-evidence-producing-gate-set)
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
  reported" and blocking every PR. Each CI-gates ruleset is checked on its
  own, and one that is desired but not live, or live but not desired, is
  reported as `rulesets.<name>.missing_ci_gates_ruleset`, never skipped.
- **Apply-time producibility guard.** Before `update` or `apply` writes
  anything, it fetches the repo's committed `.github/workflows/ci.yml` from
  `develop` and refuses (exit 2 for `update`, 1 for `apply`) if a desired CI-gates ruleset requires a context no
  `pull_request` job in that file emits — the same guard repo-init runs
  against the ci.yml it generates. A missing `ci.yml` is also a refusal, never
  a skip: with no workflow, no required check could ever report.
- **Classic branch-protection read + scoped cleanup.** The audit reads legacy
  *classic* branch protection in addition to the rulesets API. When it
  reconciles, it removes **only** the stale version-suffixed CI contexts the
  evidence ruleset now owns; every other classic setting (review requirements,
  push restrictions) is **reported, not touched**. The blast radius is minimal
  and intentional protections are preserved.
- **Classic up-to-date flag on `develop`.** The audit always reads
  `develop`'s classic protection, and a `required_status_checks.strict` of
  `true` there is drift, reported as `branch_protection.develop.strict`.
  Otherwise a repo with classic protection would still require branches to be
  up to date on `develop`, which the `CI gates (develop)` ruleset no longer
  does. `update` and `apply` set `strict` to `false` and change no other
  classic setting. `main`'s classic `strict` is never read as drift and never
  written
  ([epic vergil-project/.github#1218](https://github.com/vergil-project/.github/issues/1218)).

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
- **`update` and `apply` cannot fix it.** The ruleset `PUT` leaves out
  the key, so GitHub applies its default again. They still fix any value
  drift, write nothing when the only issues are undeclared parameters,
  and exit 1 with a pointer to the decision.
- **Resolve it in the spec.** Either declare the key with an explicit
  value in the desired ruleset in `lib/github_config.py`, or add it to
  `_STRIP_RULE_PARAMS` if it should be ignored. Keys on that list
  (`do_not_enforce_on_create`, `dismissal_restriction`) are removed before
  comparison and never reported.

A whole live rule whose `type` the desired ruleset lacks is not an
undeclared parameter. It is reported as `.rules` value drift.

## GitHub checks — workflows disabled for inactivity

GitHub turns off a public repository's scheduled workflows after 60 days
with no repository activity, and gives each the workflow state
`disabled_inactivity`
([Disabling and enabling a workflow](https://docs.github.com/en/actions/managing-workflow-runs-and-deployments/managing-workflow-runs/disabling-and-enabling-a-workflow)).
A workflow switched off this way fails silently: it simply stops running.
The audit lists the repo's workflows (`GET /repos/{owner}/{repo}/actions/workflows`)
and reports each one in that state as drift,
`inactive_workflows.<path>`, expected `active`
(vergil-project/vergil-tooling#5226):

```text
    inactive_workflows..github/workflows/epic-sweep.yml: expected='active', actual='disabled_inactivity'
```

- **It is non-compliant.** `audit` exits 1, and so does the nightly
  `update`, which turns the scheduled run red.
- **`update` and `apply` only report it.** They never turn a workflow
  back on: whether to re-enable it is a human's call, and the nightly
  job's token holds Actions read, not write. They still converge any
  other drift and open the sync PR, then exit 1 naming each workflow.
  Re-enable one from the repo's Actions tab or with
  `gh workflow enable <file name> --repo OWNER/REPO`.
- **Reading the list needs `actions=read`.** A token without it gets a
  403 naming the missing permission, and the listing is
  [not checked](#settings-the-identity-cannot-read), as
  `inactive_workflows`. The nightly job's token holds Actions read.
- A workflow turned off by hand (`disabled_manually`) is not reported.

## Related

- [Consuming Repo Setup](../guides/consuming-repo-setup.md) — the
  consuming-repo managed `.gitignore` fence + `ops.yml` contract
- [CI Architecture](../guides/ci-architecture.md#every-check-that-runs-is-a-gate)
  — how the audit gates required-check drift and `vrg-release`
- [CLI Tools Overview](cli-tools-overview.md#vrg-github-repo-config) —
  runtime preconditions and failure modes
- [Claude Config Ownership](../guides/claude-config-ownership.md) — the
  ownership model the `CLAUDE.md` and `.claude/settings.json` checks
  enforce
