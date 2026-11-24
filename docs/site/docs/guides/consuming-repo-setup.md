# Consuming Repo Setup

This is the full walkthrough for putting a repository under
vergil-tooling. For the one-screen quickstart, see
[Getting Started](../getting-started.md). For how the workflow
unfolds once you're set up, see [Git Workflow](git-workflow.md).

## Mental model — what you're installing

Vergil-tooling is delivered through three coordinated surfaces:

| Surface | What it is | How you consume it |
|---|---|---|
| **Host tools** | `vrg-container-run`, `vrg-commit`, `vrg-submit-pr`, `vrg-github-repo-init` and the other `vrg-*` CLI tools. | `uv tool install` from the vergil-tooling git URL; scripts land in `~/.local/bin/`. |
| **Dev container** | `ghcr.io/vergil-project/prod-*` images (`dev-*` with `vrg-container-run --prefix dev`) with the language runtimes and validators. | Pulled by `vrg-container-run`, which builds a per-branch cached image with vergil-tooling installed; nothing to install manually. |
| **Claude Code plugin** | `vergil-claude-plugin` — hooks, skills, agents and slash commands that enforce the workflow at the Claude Code tool level. | Declared in `.claude/settings.json`; Claude Code loads it on session start. |

Every managed repository also carries files that are not installed
as packages but checked in:

- **`vergil.toml`** — the repository profile and tooling config.
- **Claude Code hook guard** — a `.claude/hooks/guard.sh` shim, wired
  in `.claude/settings.json` as a `PreToolUse` hook.
- **GitHub workflows** — a thin `ci.yml` that calls the reusable
  workflows in `vergil-project/vergil-actions`, plus `ops.yml`,
  `epic-rollup.yml` and, when the repo publishes, `cd.yml`.

`vrg-github-repo-init` generates all of them (Step 3). See
[Git Workflow → Two enforcement layers](git-workflow.md#two-enforcement-layers)
for how the hook guard and `vrg-commit` fit together.

## Step 1: Host prerequisites

Install on your host (macOS or Linux):

**Docker** — the container engine. Docker Desktop on macOS is fine;
Docker Engine on Linux works too.

**uv** — Python package manager. Used to install `vergil-tooling`
on the host and (inside the container) to manage Python
dependencies. On macOS, install it with Homebrew (`brew install uv`),
which is how `vrg-host` reads and upgrades it. On Linux, use the
[official installer](https://docs.astral.sh/uv/getting-started/installation/).

**gh** — GitHub CLI, logged in as you (`gh auth login`). Your own
credentials drive `vrg-github-repo-init`, `vrg-submit-pr`,
`vrg-finalize-pr` and `vrg-release`. AI agents do not use `gh auth`;
they run as GitHub Apps — see [Identity Setup](account-setup.md) and
[Credential Management](credential-management.md).

**Git and Bash** — standard on Linux. On macOS, install the Xcode
Command Line Tools (`xcode-select --install`; Homebrew needs them)
and then Homebrew's `git` and `bash` (`brew install git bash`).
Apple's `/bin/bash` is 3.2 and is never updated, and the Homebrew
copies are what `vrg-host` tracks and upgrades.

You do **not** need to install language runtimes, linters, test
frameworks, markdownlint, or shellcheck on the host. All of those
live inside the container.

Once vergil-tooling is installed (Step 2), `vrg-host` on macOS
checks these prerequisites — and the agent-VM tools `vrg-vm` needs —
for you; see [Keeping up to date](#keeping-up-to-date).

## Step 2: Install vergil-tooling

Set `VERSION` to the newest release on the
[releases page](https://github.com/vergil-project/vergil-tooling/releases)
(for example `2.1.10`, with no leading `v`), then run:

```bash
VERSION=X.Y.Z
env -u UV_PYTHON -u UV_PYTHON_PREFERENCE -u UV_NO_MANAGED_PYTHON \
  uv tool install --force --no-config --python 3.15 --managed-python \
  "vergil-tooling @ git+https://github.com/vergil-project/vergil-tooling@v${VERSION}"
vrg-tooling-install "v${VERSION}"
```

The first line installs the tag `v<VERSION>` on uv's managed Python 3.15,
with no uv config file in the working directory or your user config able
to change the interpreter. The second,
[`vrg-tooling-install`](../reference/cli-tools-overview.md#vrg-tooling-install),
reinstalls the same tag with its dependencies pinned by that tag's own
`uv.lock` (fetched over git).

This installs all `vrg-*` console scripts into `~/.local/bin/`.
`uv`'s official installer already puts that directory on `PATH`; with
a Homebrew-installed `uv`, run `uv tool update-shell` once to add it.

Verify:

```bash
which vrg-container-run    # should resolve to ~/.local/bin/vrg-container-run
vrg-container-run --help   # should print usage
```

!!! note "Dev-tree override for vergil-tooling development"
    If you are developing vergil-tooling itself and want to test
    unreleased code on the host, use the single-`.venv` dev-tree
    override described in the vergil-tooling `CLAUDE.md` (`uv sync
    --python 3.15 --group dev`, then prepend `.venv/bin` to `PATH`). A single
    `.venv` is safe because the dev container masks the host `.venv`
    with an anonymous volume, so host and container never clobber
    each other's environment. This does not apply to consuming repos.

## Step 3: Bootstrap with `vrg-github-repo-init`

`vrg-github-repo-init` is the entry point for every managed repo. It
writes each managed file in its canonical form, so a repo it creates
passes `vrg-validate` and the config audit from its first commit.

Run it in a human shell. It uses your `gh` credentials for
`gh repo create` and the org-level settings, and it refuses to run
under an agent identity.

### A new repository

```bash
cd ~/dev/projects/<org>        # or any subdirectory of the <org> directory
vrg-github-repo-init <org>/<name>
```

Managed repos live at `<projects>/<org>/<name>`, so the clone goes
next to the org's other repos: the wizard walks up from the current
directory to the first one named `<org>`. Pass `--target-dir <parent>`
to name the clone's parent directory explicitly. The clone uses SSH
(`git@github.com:<org>/<name>.git`).

The wizard runs eleven steps, each committed as `chore(init): step N - …`
so an interrupted run resumes where it stopped:

1. Create the GitHub repository, always public.
2. Clone it.
3. Generate `vergil.toml` from your answers (see Step 4).
4. Scaffold `VERSION`, `CLAUDE.md`, `.claude/settings.json`,
   `.claude/hooks/guard.sh`, `README.md`, `.gitignore` and `LICENSE`.
   A TypeScript repo also gets its three managed files and a minimal
   `package.json` (see the
   [TypeScript standards](../standards/development/typescript/overview.md#a-new-repositorys-packagejson)).
5. Scaffold the language skeleton, where the language has one.
6. Generate the workflows under `.github/workflows/` (see Step 6).
7. Scaffold the MkDocs site under `docs/site/` (when docs publishing
   is on).
8. Create and push `develop` and `main`, and make `develop` the
   default branch. Then check that GitHub registered every workflow
   file on the default branch. GitHub sometimes misses some, so when any
   are missing the wizard switches the default branch to the other
   branch and back, which makes GitHub register them, and checks again.
   Any still missing after a short wait stop the run, named. A resumed
   run checks again.
9. Apply the GitHub settings, rulesets and labels.
10. Create the agent audit markers, `vergil/agent-audit/develop` and
    `vergil/agent-audit/main`, at the tips of `develop` and `main`. A
    marker that already exists is left as it is.
11. Configure GitHub Pages (when docs publishing is on).

Every prompt has a matching flag (`--description`,
`--repository-type`, `--language`, `--branching-model`,
`--versioning-scheme`, `--release-model`, `--versions`,
`--license` and others). `--non-interactive` runs with no prompts and
fails loudly if a required flag is missing; run
`vrg-github-repo-init --help` for the full list. The
[CLI Tools Overview](../reference/cli-tools-overview.md#vrg-github-repo-init)
covers the born-green language skeleton.

A resumed run reads step 3's answers back from the committed
`vergil.toml`. It stops if that file is missing or invalid, or if a
flag you pass contradicts it. `vergil.toml` does not record the
license or the initial version, so a run that resumes before step 4
asks for them again; under `--non-interactive` it needs `--license`
and `--initial-version`.

### An existing repository

From the root of the repo's clone, on a feature branch:

```bash
vrg-github-repo-init --adopt
```

Adopt is an idempotent "make it canonical" run. It overwrites every
Vergil-managed file — `vergil.toml`, `CLAUDE.md`,
`.claude/settings.json`, `.claude/hooks/guard.sh`, `LICENSE`,
`README.md`, `.gitignore`, the CI/CD workflows and the docs site
config — applies the GitHub settings, rulesets and labels, and creates
any agent audit marker that is missing. It asks
for confirmation first (`--non-interactive` accepts it). Values in an
existing `vergil.toml` become the prompt defaults, and any table the
wizard does not own (`[package]`, `[container]`, `[validation]`, …)
is kept verbatim.

Adopt commits its file changes to the branch you have checked out.
Review them — especially `README.md` and `LICENSE` — and land them
through a PR. Re-run adopt whenever you want to bring the managed
files back to canonical, for example after adding a `[package]` table.

## Step 4: `vergil.toml` — the repository profile

`vergil.toml` at the repo root is the primary configuration surface
for the tooling. `vrg-github-repo-init` writes this shape:

```toml
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "python"

[ci]
integration-tests = false

[publish]
release = true
docs = true

[dependencies]
vergil = "v2.1"
```

The wizard writes no `[ci].versions` line unless you pass
`--versions` (a comma-separated pin), so a new repo follows the
version window.

Required keys and accepted values:

| Key | Values | Notes |
|---|---|---|
| `[project].repository-type` | `application`, `documentation`, `infrastructure`, `library`, `tooling` | Required. A `library` builds every version in the version window; every other type resolves as an app and builds one version, the newest in the window. |
| `[project].versioning-scheme` | `application`, `library`, `none`, `semver` | Required. |
| `[project].branching-model` | `application-promotion`, `docs-single-branch`, `library-release` | Required. Determines which branch prefixes `vrg-commit` allows. |
| `[project].release-model` | `artifact-publishing`, `environment-promotion`, `none`, `tagged-release` | Required. |
| `[project].primary-language` | `cpp`, `go`, `python`, `rust`, `typescript` | Optional. Omit it for a repo with no primary language; it selects the per-language checks, container image and `.gitignore` fragment. |
| `[ci].versions` | non-empty list of strings | Optional pin. Omit it to follow the version window. Every pinned version must be in the window, and an app may pin only one version; either violation is an error. A pin is an exception: only a language whose window has more than one family must pin, which today is C++ (Clang and GCC). |
| `[dependencies].vergil` | `vX.Y` | Required. The vergil release line; new repos get the line of the vergil-tooling that created them. |

`[publish]` (`release`, default `false`; `docs`, default `true`; `vrg-release`
refuses a repo whose `release` is not `true`; `install`, the shell lines the
release's `install` stage runs, with `<VERSION>` and `<MINOR>` expanded to
the release's `X.Y.Z` and `X.Y`, none by default; its deprecated name
`consumer-refresh` is still read, with a warning, and setting both is an
error) and
`[ci].integration-tests` (default `false`) are optional, as is
`[ci].images` (a boolean, default `false`), which only a repo whose `ci.yml`
calls a repo-local `ci-images.yml` as job `images` sets; it makes
`images / evidence` a required check. The `[ci]` table itself is
required, even when none of its keys apply: the loader refuses a
`vergil.toml` without it. Optional
tables have their own references:
[`[container]`](../reference/container-config.md),
[`[test]`](../reference/test-config.md),
[`[package]`](../reference/package-config.md),
[`[dependency-update]`](../reference/dependency-update-config.md),
[`[vm]`](../reference/vm-spec.md) and
[`[merge-review]`](merge-review.md#change-classes-and-the-docs-allow-list)
(the paths a merge review treats as documentation).

### The documentation profile: an org `.github` repo

An org's `.github` profile repo holds the org profile README and the
community health files. It has no code, no primary language, and
nothing to version or release. Its canonical `vergil.toml` is the file
`vrg-github-repo-init` writes for those answers (no `--language`,
`--repository-type documentation`, `--versioning-scheme none`,
`--branching-model docs-single-branch`, `--release-model none`,
`--no-publish-docs`):

```toml
[project]
repository-type = "documentation"
versioning-scheme = "none"
branching-model = "docs-single-branch"
release-model = "none"

[ci]
integration-tests = false

[publish]
release = false
docs = false

[dependencies]
vergil = "v2.1"
```

What each key drives in such a repo:

| Key | What it drives |
|---|---|
| `[project].repository-type` | Required. It only decides how versions resolve, and a repo with no primary language resolves none, so here it describes the repo and drives nothing else. |
| `[project].versioning-scheme` | Required. vergil-tooling does not read it. The vergil-actions docs deploy reads it (`none` publishes one site with no versions), and a profile repo deploys no docs. |
| `[project].branching-model` | The branch prefixes `vrg-commit` accepts (`feature/*`, `bugfix/*`, `chore/*`), the eternal branches `vrg-finalize-pr` keeps (`develop`, `gh-pages`) and the integration branch `vrg-repo-status` reports (`develop`). |
| `[project].release-model` | `none` keeps `version / version-bump` out of the required checks, `vrg-github-repo-init` writes no version job, and `ci-version-bump.yml` skips. |
| `[ci].integration-tests` | `false`, the default, which `vrg-github-repo-init` always writes. `true` would require a `test / integration / <version>` check per resolved version, and this repo resolves none. |
| `[publish].release` | `false`: `vrg-release` refuses the repo, and `vrg-repo-status` shows no pending-release section. With `docs` also `false`, `vrg-github-repo-init` writes no `cd.yml`. |
| `[publish].docs` | `false` keeps `docs / docs` out of the required checks, and `vrg-github-repo-init` then writes no docs job and no docs site. Leaving the key out means `true`, which requires a `docs / docs` check that the repo's `ci.yml` must report on every pull request. |
| `[dependencies].vergil` | The Vergil release line: the ref the version window is read at, the pin on every vergil-actions workflow, and the vergil-tooling that CI installs. |

Leave two keys out:

- `[project].primary-language`. Its absence is how a repo says it has
  no language. `"none"` is not a value: the loader warns on every read
  and treats it as unset.
- `[ci].versions`. A repo with no primary language never reads it, and
  a pin such as `["latest"]` names no window version, so it would fail
  version resolution once a language was declared.

The config audit checks only that `vergil.toml` loads; it does not
rewrite the file. A profile repo whose `vergil.toml` has drifted from
this shape is corrected by a change of its own. `vrg-github-repo-init
--adopt` does not restore the shape by itself: it re-renders the file
starting from its current values, so it keeps any drift whose answer
is not overridden.

`vrg-validate` validates `vergil.toml` (required keys and enum
values) and the `README.md` structure with its repo-profile check,
the first of its common checks. See the
[repo-profile reference](../reference/lint/repo-profile.md).

## Step 5: Claude Code hook guard and settings

### Hook guard

`.claude/hooks/guard.sh` blocks raw `git` and `gh` commands in Claude
Code agent sessions, and agents' raw `vrg-gh` reads of issue and PR
content (see
[Raw content reads](../reference/hooks/pre-commit.md#raw-content-reads)).
It delegates to `vrg-hook-guard` when
vergil-tooling is installed and otherwise falls back to a `jq`-based
hard deny. `vrg-github-repo-init` writes it, executable, from the
packaged template
[`hook_guard_shim.sh`](https://github.com/vergil-project/vergil-tooling/blob/develop/src/vergil_tooling/data/hook_guard_shim.sh).
No per-clone configuration is required. The file is fully managed: the
config audit requires it to match the packaged shim byte for byte, and
`vrg-github-repo-config update` (run nightly by `ops.yml`) replaces a
stale or edited copy.

All branch/context checks (detached HEAD, protected branches,
branch prefix, issue number) live in `vrg-commit` itself. See the
[Claude Code Hook Guard reference](../reference/hooks/pre-commit.md).

### `.claude/settings.json`

`vrg-github-repo-init` writes `.claude/settings.json` as the packaged
template
[`claude_settings.json`](https://github.com/vergil-project/vergil-tooling/blob/develop/src/vergil_tooling/data/claude_settings.json),
unchanged. It has five parts:

| Key | Purpose |
|---|---|
| `permissions.allow` | Allows `Bash(vrg-*)`, so the `vrg-*` tools run without a permission prompt. |
| `hooks.PreToolUse` | Runs `${CLAUDE_PROJECT_DIR}/.claude/hooks/guard.sh` on every `Bash` tool call. |
| `enabledPlugins` | Enables `vergil@vergil-marketplace`. The `vergil-marketplace` definition (`vergil-project/vergil-claude-plugin`, `ref: "v2.1"`) lives only in the user settings: a second definition in the repo file would replace the user's whole entry. See [The `vergil-marketplace` definition](#the-vergil-marketplace-definition). |
| `attribution` | Turns off Claude Code's AI co-author trailer on commits and PRs. |
| `idleCompaction` | `false`: Claude Code doesn't compact a session while it sits idle, so the human decides when to compact or clear (#3795). Needs Claude Code v2.1.290 or later; older versions ignore it. Context-full auto-compaction and `/compact` are unchanged. |

AI co-author attribution is not used (#3200). The `attribution` block
uses empty strings, which work on every Claude Code version:

```json
"attribution": {
  "commit": "",
  "pr": ""
}
```

Commit the file. Any Claude Code session opened in the repo then
loads the hook guard and the plugin. `vrg-github-repo-config audit`
requires the file to equal the template after JSON parsing (see
[GitHub Config Audit](../reference/config-audit.md)).
For which file owns each
Claude Code setting, see
[Claude Config Ownership](claude-config-ownership.md).

The plugin adds these hooks:

- **PreToolUse on `Bash`** — blocks raw `git commit`, raw
  `gh pr create`, commits from the main tree once the worktree
  convention is adopted (on `main`/`develop` otherwise), heredoc syntax, bash associative arrays,
  host/container tool misrouting, auto-close linkage keywords,
  agent PR merges and GitHub Contents API writes.
- **PreToolUse on `Write`/`Edit`** — blocks writes to the main
  worktree once the worktree convention is adopted, and limits the
  audit identity to writing its own artifacts.
- **PostToolUse on `Bash`** — surfaces deprecation warnings in
  command output.

See the plugin's own
[Hooks reference](https://github.com/vergil-project/vergil-claude-plugin/blob/develop/docs/site/docs/hooks/index.md)
for the details.

### The `vergil-marketplace` definition

The marketplace that `enabledPlugins` names is defined once, in the user
settings, `~/.claude/settings.json` on the host:

```json
{
  "extraKnownMarketplaces": {
    "vergil-marketplace": {
      "source": {
        "source": "github",
        "repo": "vergil-project/vergil-claude-plugin",
        "ref": "v2.1"
      }
    }
  }
}
```

`v2.1` is the plugin's floating rolling tag, as for vergil-tooling and
vergil-actions consumers. Each plugin release moves it forward to the new
`v2.1.Z`, and a rollback moves it back to an earlier one (see
[Rolling back a release](releasing.md#rolling-back-a-release)). `main` is
still the plugin's release branch, but consumers follow the tag, not the
branch. `vrg-vm` copies the host file into each agent VM, so the host entry
is the only one to maintain. Don't define `vergil-marketplace` in a repo's
`.claude/settings.json`: that entry would replace the user entry as a whole.
The design is epic
[vergil-project/.github#760](https://github.com/vergil-project/.github/issues/760)
([spec](https://github.com/vergil-project/.github/blob/develop/epics/760-plugin-tag-distribution/spec.md)).

#### Switching an existing machine

Editing the `ref` of a marketplace that is already registered is a change
of source, not an update: `claude plugin marketplace update` alone keeps the
old registration. After you edit the `ref` in the host
`~/.claude/settings.json`, run, in a human shell:

```bash
vrg-host plugins        # the host
vrg-vm update --all     # every running agent VM
```

Both run one shared refresh. It re-adds each declared marketplace at the
declared ref, which re-clones it and rewrites its registration, then
updates each of those marketplaces by name (`claude plugin marketplace
update <name>`), then the installed plugins. `claude-plugins-official` is
re-added and updated the same way when an enabled plugin needs it and the
settings do not declare it. Any other registered marketplace, such as an
old one that no settings file declares, is never updated and cannot fail
the refresh. So the settings must declare the marketplace of every plugin
they enable, `claude-plugins-official` aside: a plugin enabled from an
undeclared marketplace is an error, raised before any `claude` command runs,
that names the plugin and its marketplace (its catalog would otherwise never
be refreshed). So is an install that `claude plugin list --json` reports
enabled from such a marketplace, for example one a project's own settings
enable: the refresh stops before updating any plugin. It then verifies
that each declared
GitHub marketplace is registered, as
`claude plugin marketplace list --json` reports it, at the declared repo
and ref. A mismatch or a missing registration is a reported failure:
`vrg-host plugins` exits `2`, and `vrg-vm update --all` reports the failed
box and goes on to the rest. The check covers only what the settings
declare, so a missing or misspelled `vergil-marketplace` entry is not
caught; after a switch, confirm the ref in `claude plugin marketplace list
--json` yourself.

`vrg-vm update` updates vergil-tooling in each running VM, then copies the
host's current `settings.json` into it, then refreshes the plugins. A box
whose vergil-tooling update fails gets neither the new settings nor the
refresh, so it still declares the old `ref`; run `vrg-vm update` on it again
once the cause is fixed. `vrg-vm update --all` skips and reports a VM
that is not running; a single-VM `vrg-vm update` on one fails with exit 1.
A skipped Lima VM's next `vrg-vm start` refreshes its plugins, but on start
a failed refresh or verification is only a warning: check the start output
for an `update-plugins` warning, or run `vrg-vm update` on that VM once it is
up. An off-platform (cloud) box does not support `vrg-vm start`. Bring a
stopped one back with `vrg-vm rebuild <org>/<repo> --identity <identity>`,
which replaces the disposable VM and reattaches its persistent volume, then
run `vrg-vm update` on it.

Restart Claude Code afterwards: a running session keeps the plugin version
it loaded.

Both commands are human actions. `vrg-host plugins` and every `vrg-vm`
subcommand refuse an agent identity. No machine is switched by hand-editing
Claude Code's own registration files. See
[`vrg-host plugins`](../reference/dev/host.md#plugins).

#### Moving to a new minor line

When the plugin's first `2.2.0` release creates the `v2.2` rolling tag,
`v2.1` stops moving. The fleet stays on the last `v2.1.Z` until you change
the host `ref` to `"v2.2"` and run the same two commands. Moving to a new
minor line is a deliberate act, never automatic, as it is for
vergil-tooling and vergil-actions (see
[Minor release](releasing.md#minor-release-opt-in-for-new-features)).
A [rollback](releasing.md#rolling-back-a-release) stays within one line:
`vrg-promote` moves the rolling tag of the target's own line, so it cannot
take the fleet from `v2.2` back to a `v2.1.Z`. To leave a bad first release
of a new line, change the host `ref` back and run the same two commands.

### `CLAUDE.md`

`vrg-github-repo-init` writes `CLAUDE.md` with the packaged template
[`claude_md_consumer.md`](https://github.com/vergil-project/vergil-tooling/blob/develop/src/vergil_tooling/data/claude_md_consumer.md)
between the markers `<!-- vergil:template:claude-md:begin -->` and
`<!-- vergil:template:claude-md:end -->`. The template region holds the
fleet-wide agent policy: worktrees, `vrg-git`/`vrg-gh`, the validation
command, the PR handoff and freeze, the memory rule, the local-settings
rule, the engineering principles and the paad policy pointer. Don't edit
it by hand. Add what an agent needs for your repo (overview, commands,
constraints) after the end marker, and link reference material from your
site docs rather than copying it in.

The config audit requires the region to match the template
verbatim and the whole file to stay at or under **200 lines**. The repo
has no `AGENTS.md`: `CLAUDE.md` is the one instruction file. Keep
`.claude/settings.local.json` absent or exactly `{}`. In repos in the
in-scope orgs, `vrg-commit`, `vrg-pr-workflow report-ready` and
`vrg-container-run` refuse an agent and warn a human while it holds
anything else; elsewhere nothing checks it. See
[Local settings](claude-config-ownership.md#local-settings) for which
copies are checked and the known limit.

### Worktree convention

Every managed repo adopts the worktree convention so multiple Claude
Code agents can work in parallel without colliding. The generated
files already carry both halves:

- `.worktrees/` is in the `base` fragment of the managed `.gitignore`
  block (see Step 7). The plugin's worktree hooks activate on that
  line.
- The `CLAUDE.md` template region includes "Worktrees", the local
  on-ramp to the canonical
  [worktree convention spec][worktree-spec].

!!! tip "The managed block already ignores your validation/build output"
    Validation and build artifacts (coverage reports, compiled output,
    cache dirs, temporary venvs) are part of the composed managed block in
    your `.gitignore`. Keeping them ignored matters: un-gitignored output
    dirties a worktree, and a *merged* worktree left dirty is one
    `vrg-finalize-pr` cannot auto-remove — it surfaces as
    **needs-attention** in `vrg-repo-status` and requires the opt-in
    `--clean-dirty` (or a manual clean) to clear. Carrying the managed
    block means merged worktrees sweep automatically.

For how to use worktrees during development, see
[Git Workflow → Parallel work with worktrees](git-workflow.md#parallel-work-with-worktrees).

## Step 6: CI workflow

`vrg-github-repo-init` generates `.github/workflows/ci.yml` as a thin
caller of the reusable workflows in `vergil-project/vergil-actions`,
pinned to the `[dependencies].vergil` line. It passes only
`language:` and `container-suffix:`; the reusable workflows read the
version matrix from `vrg-ci-resolve` themselves (see
[Version resolution](ci-architecture.md#version-resolution)). Depending on the
profile, it calls `ci-audit`, `ci-quality`, `ci-security`, `ci-docs`,
`ci-test`, `ci-version-bump` and `ci-package`. Step 9 of the wizard
creates the CI-gates ruleset that requires the checks these jobs
report, and refuses to apply one that requires a check `ci.yml`
never produces.

Alongside it:

- **`ops.yml`** — runs `vrg-github-repo-config update` on a daily, per-repo
  staggered `cron` schedule (see Step 7).
- **`epic-rollup.yml`** — on any issue close, closes the parent epic
  once all of its tasks are closed.
- **`cd.yml`** — the release and docs publishing workflow, generated
  when `[publish]` enables either. Its `release` job runs on every push to
  `main` except the one that creates the branch: the wizard creates `main`
  by a direct push, which no release PR produced, so there is nothing to
  release. A later push to `main` with no release PR still fails the
  release's CI-evidence gate.

The jobs of these three that call a workflow reading the automation
App's keys pass `secrets: inherit` in a `vergil-project` repo. In a repo
of any other organization they pass the keys by name, since GitHub
passes `inherit` only to a reusable workflow in the caller's own
organization or enterprise (see
[CD and ops: reusable-workflow secrets](ci-architecture.md#cd-and-ops-reusable-workflow-secrets)).

To regenerate them after a `vergil.toml` change, re-run
`vrg-github-repo-init --adopt`. See the
[CI Architecture](ci-architecture.md) guide for the CI tiers.

### Optional: OS packages

To ship signed `.deb`/`.rpm` packages from releases:

1. Add a `[package]` table to `vergil.toml` (see
   [Package configuration](../reference/package-config.md)).
2. Re-run `vrg-github-repo-init --adopt` from the repo root. It keeps
   `[package]` (and any other hand-written table) and regenerates the
   workflows: `ci.yml` gains a `package` job calling `ci-package.yml`. The
   `cd.yml` `release` job of a `vergil-project` repo already passes
   `secrets: inherit`, which carries the signing secrets. In a repo of any
   other organization the job passes only the App keys (and a rust repo's
   `CARGO_REGISTRY_TOKEN`) by name, so it does not carry the signing
   secrets. The GitHub config step then makes `package / evidence` a
   required check.
3. Create a `package-signing` environment, restricted to `main`, holding
   `PACKAGE_SIGNING_KEY` and `PACKAGE_SIGNING_PASSPHRASE`.
4. Make sure the org's GitHub App can dispatch to `<org>/packages`, which
   indexes each released package.

### Optional: dependency-update hook

Every `vrg-release` updates the repo's dependencies once the release is
tagged and published (see
[Dependency updates](releasing.md#dependency-updates)). The built-in
updaters cover Python (`uv.lock`), Rust (`Cargo.lock`), TypeScript
(`package-lock.json`), vergil ecosystem refs, the packaged
runtime, and container image tags. If the repo has other dependencies
to keep current, such as binary tool versions or provider pins, add a
hook to `vergil.toml`:

```toml
[dependency-update]
hook = "scripts/dev/update-deps.sh"
```

The hook runs in the dev container after the built-in updaters. It
edits files but never commits, and it reports anything it holds back as
JSON lines in `$VRG_DEPS_REPORT`. The full contract is in the
[dependency-update reference](../reference/dependency-update-config.md).

Dependency-update PRs carry the `dependency-update` label. The update
creates the label the first time it needs it, but provisioning the full
label set up front keeps the repo in line with the registry:

```bash
vrg-ensure-label --sync --repo <owner>/<repo>
```

## Step 7: Managed `.gitignore` block and config audit

Every managed repo owes two things to the fleet's self-policing config
audit, and both are checked nightly:

1. **Your `.gitignore` must carry the vergil-managed block.** The fleet's
   ignore vocabulary is *composed* — a language-agnostic `base` fragment
   plus the fragment for your repo's `[project].primary-language` — into a
   fenced **managed block**: a `# >>> vergil-managed: base + <language> …`
   begin marker, the composed pattern lines (editors, OS, secrets, Vergil
   internals, all build/validation output, and your language's artifacts),
   and a `# <<< vergil-managed <<<` end marker. The audit requires that
   block to be present and to match the composed block for your language
   **exactly**, with no managed pattern left loose outside the fence. A
   language with no fragment of its own — or a repo that declares none —
   gets the base-only block. Genuinely repo-local entries live outside the
   fence and are never touched; `vrg-github-repo-config update` writes or
   refreshes the block.

2. **Your repo must carry a scheduled `ops.yml` wired to the config
   job.** `.github/workflows/ops.yml` must call the reusable
   `ops-github-config.yml` workflow **on a `cron` schedule**. That
   scheduled job is what runs `vrg-github-repo-config update` each
   night.

The nightly `update` fixes a drifted managed block through a sync PR
that merges within the run. The `ops.yml` wiring has no fixer: a run
that finds it wrong exits 1 and turns red, as it does for any other
drift with no fixer. `vrg-github-repo-init` writes
both, so a new or adopted repo starts conforming.

You never bump a pin to receive fragment changes: every repo tracks the
rolling `vergil-tooling@vX.Y` tag, so an updated fragment released under
that line is picked up on the next `vrg-github-repo-config update` run,
which the nightly `ops.yml` job makes, opening one sync PR for the
drifted files.

Repos also carry a canonical, **report-only** `.github/dependabot.yml`
(epic vergil-project/.github#420): Dependabot alerts stay on, but every
`updates` entry sets `open-pull-requests-limit: 0`, so Dependabot never
opens a PR. The file is an exact match of the render for your
`[project].primary-language` — no local additions. `vrg-github-repo-init`
writes it, and `vrg-github-repo-config update` writes or refreshes it.
`vrg-validate` rejects any `dependabot.yml` entry whose
`open-pull-requests-limit` is missing (Dependabot then defaults to 5) or
not `0`, and the nightly `update` rewrites a missing or non-canonical
file.

The same nightly run also fails while the repo has **any** open
Dependabot alert: its `dependabot-audit` job runs
`vrg-dependabot-audit`, which exits 1 on an open alert and 2 when it
cannot read the alerts. Fix the dependency or dismiss the alert on
GitHub to clear it. See the
[Dependabot Alert Audit reference](../reference/dependabot-audit.md).

The audit also checks `vergil.toml`, `CLAUDE.md`,
`.claude/settings.json`, `.claude/hooks/guard.sh`, the reusable-workflow
pins and the GitHub settings. Check your repo locally before relying on
the nightly run:

```bash
vrg-github-repo-config audit   # exit 0 compliant, 1 drift, 2 could-not-complete
```

See the [GitHub Config Audit reference](../reference/config-audit.md)
for the full check list and the managed-block propagation model.

## Step 8: Verify end-to-end

Once the repo is bootstrapped, sanity-check each layer:

```bash
# 1. Host tools — should print vrg-container-run help
vrg-container-run --help

# 2. Full validation inside the dev container, including
#    the repo-profile check of vergil.toml
vrg-container-run -- vrg-validate

# 3. Config audit — local files and GitHub settings
vrg-github-repo-config audit

# 4. Hook guard (requires a Claude Code session) — in a Claude
#    Code session in this repo, have Claude try to run a raw
#    `git commit`. The hook guard should block it.
```

If any step fails, check the corresponding section above, then
re-run. Common causes: `uv tool install` not run,
`.claude/settings.json` not committed to the branch your Claude
Code session loaded, or a managed file edited by hand (re-run
`vrg-github-repo-init --adopt`).

## Keeping up to date

Each `vergil-tooling` release installs itself on the Mac that runs it:
its `[publish].install` runs `vrg-tooling-install` with the
just-released version. On any other host, run
`vrg-tooling-install vX.Y.Z` with the new release. If it fails, reinstall
with the [Step 2](#step-2-install-vergil-tooling) lines. Do not use
`uv tool upgrade vergil-tooling`: it re-resolves the moving tag without
the release's lock.

On macOS, `vrg-host` covers the other host prerequisites (`uv`,
`gh`, Docker, and the `vrg-vm` tools such as Lima, `gcloud` and
OpenTofu), and reports vergil-tooling's installed version:

```bash
vrg-host                   # report drift, missing tools and failed health checks
vrg-host upgrade --all     # upgrade every auto-managed tool, respecting pins
```

`vrg-host` never upgrades vergil-tooling itself: the vergil-tooling
release installs it. Docker Desktop and macOS itself are reported but never upgraded by
`vrg-host`. When a new release of a host tool breaks your workflow,
pin it with `vrg-host pin` rather than skipping upgrades — see the
[vrg-host reference](../reference/dev/host.md#pinning-host-dependencies).

`vrg-container-run` pulls the base dev image on every run and rebuilds
its per-branch cached image when the base image, the language
lockfile or `vergil.toml` changes, or when a new vergil-tooling release
lands on the line `vergil.toml` pins, so there is nothing to refresh by
hand. See
[How the cache tracks vergil-tooling releases](../reference/cli-tools-overview.md#how-the-cache-tracks-vergil-tooling-releases).
`vrg-container-cache status` shows the cache state for the current
branch.

After a plugin release, refresh the host and every agent VM with
`vrg-host plugins` and `vrg-vm update --all`, in a human shell, then
restart Claude Code (see
[Switching an existing machine](#switching-an-existing-machine)). The same
commands pick up a changed `ref` and a
[rollback](releasing.md#rolling-back-a-release) (see the
[Pick the move up on each machine](releasing.md#pick-the-move-up-on-each-machine)
runbook). They verify only the marketplace repo and ref, which is `v2.1`
before and after a release, so confirm the installed version on each
machine with `claude plugin list --json`. The plugin README's
[Update section](https://github.com/vergil-project/vergil-claude-plugin#update)
describes the same pick-up of a release or a rollback, and its
[Verify the update](https://github.com/vergil-project/vergil-claude-plugin#verify-the-update)
subsection gives that check. Do not use the in-session
`/plugin marketplace update` and `/reload-plugins` pair instead: it does not
switch a changed `ref` and checks nothing.

## Troubleshooting

- **`vrg-container-run: command not found`** — `uv tool install` has
  not been run, or `~/.local/bin` is not on PATH. Re-run the install
  command from Step 2 and confirm `which vrg-container-run` resolves.
- **`vrg-github-repo-init` refuses to run** — it is human-credential
  only; run it in a human shell, not an agent session. For a new repo,
  run it from inside the `<org>` directory or pass `--target-dir`.
- **Plugin hooks don't fire** — check that `.claude/settings.json`
  is present, committed, and syntactically valid JSON. Restart
  Claude Code; the plugin is loaded at session start. If still
  stuck, refresh the plugin per "Keeping up to date" above.
- **`vrg-host plugins` fails with `marketplace update <name> failed`** —
  `<name>` is a marketplace the refresh re-added: a declared one, or
  `claude-plugins-official`. The `claude` command's stdout and stderr,
  printed just above the error, give the cause. Every add runs before any
  update, so an earlier `marketplace add <source> failed` error, where
  `<source>` is what the refresh tried to add, is the likelier cause: fix
  that first.
  Then re-run `vrg-host plugins`. Any other registered marketplace, one the
  refresh did not re-add, is never updated, so it cannot cause this. See
  [`vrg-host plugins`](../reference/dev/host.md#plugins).
- **Commits blocked with "originate from inside .worktrees/"** —
  intentional. This repo has adopted the worktree convention.
  Create a worktree for your work; see
  [Git Workflow → Parallel work with worktrees](git-workflow.md#parallel-work-with-worktrees).
- **"raw git/gh blocked"** — the Claude Code hook guard is working
  as intended. Use `vrg-commit` instead of raw `git commit`, and
  `vrg-git`/`vrg-gh` instead of raw `git`/`gh`.

For a broader troubleshooting index see
[Git Workflow → Troubleshooting](git-workflow.md#troubleshooting).

## Related

- [Git Workflow](git-workflow.md) — how the per-change cycle
  actually unfolds once setup is done
- [Validation Matrix](validation-matrix.md) — every check and
  where it runs
- [CI Architecture](ci-architecture.md) — per-language test/lint/audit
  tier wiring
- [Releasing](releasing.md) — release workflow
- [Worktree convention spec][worktree-spec]
  — canonical reference for the parallel-agent convention

[worktree-spec]: https://github.com/vergil-project/vergil-tooling/blob/develop/docs/specs/worktree-convention.md
