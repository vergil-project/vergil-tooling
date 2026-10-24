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

```bash
uv tool install --python 3.14 'vergil-tooling @ git+https://github.com/vergil-project/vergil-tooling@v2.1'
```

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
    --group dev`, then prepend `.venv/bin` to `PATH`). A single
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

The wizard runs ten steps, each committed as `chore(init): step N - …`
so an interrupted run resumes where it stopped:

1. Create the GitHub repository (`--visibility`, default public).
2. Clone it.
3. Generate `vergil.toml` from your answers (see Step 4).
4. Scaffold `VERSION`, `CLAUDE.md`, `.claude/settings.json`,
   `.claude/hooks/guard.sh`, `README.md`, `.gitignore` and `LICENSE`.
5. Scaffold the language skeleton, where the language has one.
6. Generate the workflows under `.github/workflows/` (see Step 6).
7. Scaffold the MkDocs site under `docs/site/` (when docs publishing
   is on).
8. Create and push `develop` and `main`, and make `develop` the
   default branch.
9. Apply the GitHub settings, rulesets and labels.
10. Configure GitHub Pages (when docs publishing is on).

Every prompt has a matching flag (`--description`,
`--repository-type`, `--language`, `--branching-model`,
`--versioning-scheme`, `--release-model`, `--versions`,
`--license` and others). `--non-interactive` runs with no prompts and
fails loudly if a required flag is missing; run
`vrg-github-repo-init --help` for the full list. The
[CLI Tools Overview](../reference/cli-tools-overview.md#vrg-github-repo-init)
covers the born-green language skeleton.

### An existing repository

From the root of the repo's clone, on a feature branch:

```bash
vrg-github-repo-init --adopt
```

Adopt is an idempotent "make it canonical" run. It overwrites every
Vergil-managed file — `vergil.toml`, `CLAUDE.md`,
`.claude/settings.json`, `.claude/hooks/guard.sh`, `LICENSE`,
`README.md`, `.gitignore`, the CI/CD workflows and the docs site
config — and applies the GitHub settings, rulesets and labels. It asks
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
| `[project].primary-language` | `cpp`, `go`, `java`, `python`, `rust`, `typescript` | Optional. Omit it for a repo with no primary language; it selects the per-language checks, container image and `.gitignore` fragment. |
| `[ci].versions` | non-empty list of strings | Optional pin. Omit it to follow the version window. Every pinned version must be in the window, and an app may pin only one version; either violation is an error. C++ and TypeScript repos must pin. |
| `[dependencies].vergil` | `vX.Y` | Required. The vergil release line; new repos get the line of the vergil-tooling that created them. |

`[publish]` (`release`, default `false`; `docs`, default `true`) and
`[ci].integration-tests` (default `false`) are optional. Optional
tables have their own references:
[`[container]`](../reference/container-config.md),
[`[test]`](../reference/test-config.md),
[`[package]`](../reference/package-config.md),
[`[dependency-update]`](../reference/dependency-update-config.md) and
[`[vm]`](../reference/vm-spec.md).

`vrg-repo-profile` validates `vergil.toml` (required keys and enum
values) and the `README.md` structure; `vrg-validate` runs it as part
of its common checks. See the
[repo-profile reference](../reference/lint/repo-profile.md).

## Step 5: Claude Code hook guard and settings

### Hook guard

`.claude/hooks/guard.sh` blocks raw `git` and `gh` commands in Claude
Code agent sessions. It delegates to `vrg-hook-guard` when
vergil-tooling is installed and otherwise falls back to a `jq`-based
hard deny. `vrg-github-repo-init` writes it, executable, from the
packaged template
[`hook_guard_shim.sh`](https://github.com/vergil-project/vergil-tooling/blob/develop/src/vergil_tooling/data/hook_guard_shim.sh).
No per-clone configuration is required.

All branch/context checks (detached HEAD, protected branches,
branch prefix, issue number) live in `vrg-commit` itself. See the
[Claude Code Hook Guard reference](../reference/hooks/pre-commit.md).

### `.claude/settings.json`

`vrg-github-repo-init` writes `.claude/settings.json` from the
packaged template
[`claude_settings.json`](https://github.com/vergil-project/vergil-tooling/blob/develop/src/vergil_tooling/data/claude_settings.json)
and pins the plugin marketplace to `ref: "main"`, the plugin's single
released channel. It has four parts:

| Key | Purpose |
|---|---|
| `permissions.allow` | Allows `Bash(vrg-*)`, so the `vrg-*` tools run without a permission prompt. |
| `hooks.PreToolUse` | Runs `${CLAUDE_PROJECT_DIR}/.claude/hooks/guard.sh` on every `Bash` tool call. |
| `extraKnownMarketplaces` / `enabledPlugins` | Declares the `vergil-marketplace` marketplace (`vergil-project/vergil-claude-plugin`, `ref: "main"`) and enables `vergil@vergil-marketplace`. |
| `attribution` | Turns off Claude Code's AI co-author trailer on commits and PRs. |

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
checks the marketplace entry, its `ref`, the enabled plugin and the
`attribution` block against the template.

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

### Worktree convention

Every managed repo adopts the worktree convention so multiple Claude
Code agents can work in parallel without colliding. The generated
files already carry both halves:

- `.worktrees/` is in the `base` fragment of the managed `.gitignore`
  block (see Step 7). The plugin's worktree hooks activate on that
  line.
- The `CLAUDE.md` consumer section includes "Parallel AI agent
  development", the local on-ramp to the canonical
  [worktree convention spec][worktree-spec].

!!! tip "The managed block already ignores your validation/build output"
    Validation and build artifacts (coverage reports, compiled output,
    cache dirs, temporary venvs) are part of the composed managed block in
    your `.gitignore`. Keeping them ignored matters: un-gitignored output
    dirties a worktree, and a *merged* worktree left dirty is one
    `vrg-finalize-pr` cannot auto-remove — it surfaces as
    **needs-attention** in `vrg-worktree-status` and requires the opt-in
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

- **`ops.yml`** — runs the config audit on a daily, per-repo
  staggered `cron` schedule (see Step 7).
- **`epic-rollup.yml`** — on any issue close, closes the parent epic
  once all of its tasks are closed.
- **`cd.yml`** — the release and docs publishing workflow, generated
  when `[publish]` enables either.

To regenerate them after a `vergil.toml` change, re-run
`vrg-github-repo-init --adopt`. See the
[CI Architecture](ci-architecture.md) guide for the CI tiers.

### Optional: OS packages

To ship signed `.deb`/`.rpm` packages from releases:

1. Add a `[package]` table to `vergil.toml` (see
   [Package configuration](../reference/package-config.md)).
2. Re-run `vrg-github-repo-init --adopt` from the repo root. It keeps
   `[package]` (and any other hand-written table) and regenerates the
   workflows: `ci.yml` gains a `package` job calling `ci-package.yml`, and the
   `cd.yml` `release` job switches to `secrets: inherit`. The GitHub config
   step then makes `package / evidence` a required check.
3. Create a `package-signing` environment, restricted to `main`, holding
   `PACKAGE_SIGNING_KEY` and `PACKAGE_SIGNING_PASSPHRASE`.
4. Make sure the org's GitHub App can dispatch to `<org>/packages`, which
   indexes each released package.

### Optional: dependency-update hook

Every `vrg-release` ends by updating the repo's dependencies (see
[Dependency updates](releasing.md#dependency-updates)). The built-in
updaters cover Python (`uv.lock`), vergil ecosystem refs, the packaged
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
   fence and are never touched; run `vrg-gitignore-sync` to write or
   refresh the block.

2. **Your repo must carry a scheduled `ops.yml` wired to the config
   audit.** `.github/workflows/ops.yml` must call the reusable
   `ops-github-config.yml` workflow **on a `cron` schedule**. That
   scheduled job is what runs the audit each night.

Both are **fatal in the nightly audit**: any drift returns exit 1 and
turns the scheduled `ops.yml` run red. `vrg-github-repo-init` writes
both, so a new or adopted repo starts conforming.

You never bump a pin to receive fragment changes: every repo tracks the
rolling `vergil-tooling@vX.Y` tag, so an updated fragment released under
that line is picked up on the next `vrg-gitignore-sync` run — and the
nightly audit flags the drift until you run it.

Repos also carry a canonical, **report-only** `.github/dependabot.yml`
(epic vergil-project/.github#420): Dependabot alerts stay on, but every
`updates` entry sets `open-pull-requests-limit: 0`, so Dependabot never
opens a PR. The file is an exact match of the render for your
`[project].primary-language` — no local additions. `vrg-github-repo-init`
writes it, and `vrg-dependabot-sync --write` writes or refreshes it.
`vrg-validate` rejects any `dependabot.yml` entry whose
`open-pull-requests-limit` is missing (Dependabot then defaults to 5) or
not `0`, and the nightly audit flags a missing or non-canonical file
until you run `vrg-dependabot-sync --write`.

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
#    vrg-repo-profile against vergil.toml
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

After each `vergil-tooling` release, upgrade the host tools:

```bash
uv tool upgrade vergil-tooling
```

`uv tool upgrade` re-resolves the git reference, pulls the current
tip of the rolling minor tag, and rebuilds the isolated tool venv.
No need to repeat the full git URL.

On macOS, `vrg-host` covers vergil-tooling and the other host
prerequisites together (`uv`, `gh`, Docker, and the `vrg-vm` tools
such as Lima, `gcloud` and OpenTofu):

```bash
vrg-host                   # report drift, missing tools and failed health checks
vrg-host upgrade --all     # upgrade every auto-managed tool, respecting pins
```

`vrg-host upgrade vergil-tooling` runs the same `uv tool upgrade`.
Docker Desktop and macOS itself are reported but never upgraded by
`vrg-host`. When a new release of a host tool breaks your workflow,
pin it with `vrg-host pin` rather than skipping upgrades — see the
[vrg-host reference](../reference/dev/host.md#pinning-host-dependencies).

`vrg-container-run` pulls the base dev image on every run and rebuilds
its per-branch cached image when the base image, the language
lockfile or `vergil.toml` changes, so there is nothing to refresh by
hand.
`vrg-container-cache status` shows the cache state for the current
branch.

After a plugin release, refresh the Claude Code plugin from inside
Claude Code:

```text
/plugin marketplace update vergil-marketplace
/reload-plugins
```

See the plugin README's
[Update section](https://github.com/vergil-project/vergil-claude-plugin#update)
for what each step does.

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
