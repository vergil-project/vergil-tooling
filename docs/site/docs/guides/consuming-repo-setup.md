# Consuming Repo Setup

This is the full walkthrough for onboarding a new repository to use
standard-tooling. For the one-screen quickstart, see
[Getting Started](../getting-started.md). For how the workflow
unfolds once you're set up, see [Git Workflow](git-workflow.md).

## Mental model — what you're installing

Standard-tooling is delivered through three coordinated surfaces.
The setup steps below wire up each one:

| Surface | What it is | How you consume it |
|---|---|---|
| **Host tools** | `st-docker-run`, `st-commit`, `st-submit-pr`, and other host-side CLI tools. | `uv tool install` from the standard-tooling git URL; scripts land in `~/.local/bin/`. |
| **Dev container** | `ghcr.io/wphillipmoore/dev-<lang>:<version>` — pre-baked images with language runtimes, validators, and every other `st-*` tool installed. | Pulled automatically by `st-docker-run`; nothing to install manually. |
| **Claude Code plugin** | `standard-tooling-plugin` — hooks, skills, agents, and slash commands that enforce the workflow at the Claude-Code-tool level. | Declared in `.claude/settings.json`; Claude Code loads on session start. |

Plus two layers that aren't installed as packages but are expected
to be present in every consuming repo:

- **Local git hooks** — a `.githooks/pre-commit` env-var gate
  checked into the repo, wired via `git config core.hooksPath
  .githooks`.
- **CI workflow** — the `standards-compliance` composite action
  from `wphillipmoore/standard-actions`, invoked from your repo's
  `.github/workflows/ci.yml`.

Both of these validate the repo profile and enforce branching rules
from two different entry points. See
[Git Workflow → Two enforcement layers](git-workflow.md#two-enforcement-layers)
for how they fit together.

## Step 1: Host prerequisites

Install on your host (macOS or Linux):

**Docker** — the container engine. Docker Desktop on macOS is fine;
Docker Engine on Linux works too.

**uv** — Python package manager. Used to install `standard-tooling`
on the host and (inside the container) to manage Python
dependencies. Install via the
[official installer](https://docs.astral.sh/uv/getting-started/installation/).

**gh** — GitHub CLI. Must be authenticated
(`gh auth login`). Used by `st-submit-pr` to create PRs and, inside
the container, to pass through your GitHub token for push operations.

**Git and Bash** — ship with macOS by default and are standard on
Linux.

You do **not** need to install language runtimes, linters, test
frameworks, markdownlint, or shellcheck on the host. All of those
live inside the container.

## Step 2: Install standard-tooling

```bash
uv tool install 'standard-tooling @ git+https://github.com/wphillipmoore/standard-tooling@v1.4'
```

This installs all `st-*` console scripts into `~/.local/bin/`,
which `uv`'s official installer already configures on `PATH`. No
sibling checkout, no custom PATH entries, no venv bootstrapping.

Verify:

```bash
which st-docker-run    # should resolve to ~/.local/bin/st-docker-run
st-docker-run --help   # should print usage
```

Optional — add `gh`'s token to your environment so it flows into
the container automatically on `st-docker-run` invocations:

```bash
export GH_TOKEN=$(gh auth token)
```

!!! note "Dev-tree override for standard-tooling development"
    If you are developing standard-tooling itself and want to test
    unreleased code on the host, use the `.venv-host` dev-tree
    override described in the standard-tooling `CLAUDE.md`. This
    does not apply to consuming repos.

## Step 3: Git hook setup

Every managed repo checks in a `.githooks/pre-commit` env-var gate.
Create it in your repo:

```bash
#!/usr/bin/env bash
# Admit st-commit-driven commits.
if [[ "${ST_COMMIT_CONTEXT:-}" == "1" ]]; then exit 0; fi
# Admit derived-commit workflows (amend, cherry-pick, revert, rebase, merge).
case "${GIT_REFLOG_ACTION:-}" in
  amend|cherry-pick|revert|rebase*|merge*) exit 0 ;;
esac
echo "ERROR: raw 'git commit' is blocked. Use 'st-commit' instead." >&2
echo "See docs/repository-standards.md" >&2
exit 1
```

Save this as `.githooks/pre-commit`, make it executable, and commit
it. Then enable the hook once per clone:

```bash
chmod +x .githooks/pre-commit
git config core.hooksPath .githooks
```

The gate admits commits from `st-commit` (which sets
`ST_COMMIT_CONTEXT=1`) and from derived workflows like rebase and
cherry-pick. All branch/context checks (detached HEAD, protected
branches, branch prefix, issue number) live in `st-commit` itself.

Full reference: [Git Hooks and Validation][hooks-doc].

## Step 4: Repository profile

Create `docs/repository-standards.md` at your repo root. This is
the primary configuration surface for the validators:

```markdown
# Repository Standards

## Table of Contents

- [AI co-authors](#ai-co-authors)
- [Repository profile](#repository-profile)

## AI co-authors

- Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

## Repository profile

- repository_type: library
- versioning_scheme: semver
- branching_model: library-release
- release_model: tagged-release
- supported_release_lines: 1
- primary_language: python
```

Required attributes and accepted values:

| Attribute | Values | Notes |
|---|---|---|
| `repository_type` | `application`, `library`, `tooling`, `documentation` | Informational; some validators branch on this. |
| `versioning_scheme` | `semver`, `calver`, `none` | How releases are versioned. |
| `branching_model` | `library-release`, `application-promotion`, `docs-single-branch` | Determines which branch prefixes `st-commit` allows. |
| `release_model` | `tagged-release`, `continuous-deploy`, `none` | Affects release-flow tooling expectations. |
| `supported_release_lines` | integer (commonly `1`) | How many concurrent major lines you support. |
| `primary_language` | `python`, `go`, `java`, `rust`, `ruby`, `shell`, `none` | Determines which per-language validators run. |

The `AI co-authors` section defines which `Co-Authored-By` trailer
values `st-commit` accepts for `--agent`. Add one line per accepted
agent identity.

Values containing `<`, `>`, or `|` are rejected as placeholders by
`st-repo-profile`.

## Step 5: Enable the Claude Code plugin

Create `.claude/settings.json` in your repo:

```json
{
  "extraKnownMarketplaces": {
    "standard-tooling-marketplace": {
      "source": {
        "source": "github",
        "repo": "wphillipmoore/standard-tooling-plugin"
      }
    }
  },
  "enabledPlugins": {
    "standard-tooling@standard-tooling-marketplace": true
  }
}
```

Commit this file. It's part of your repo's reproducible
environment — any Claude Code session opened in this repo will
pick up the plugin via this declaration.

What the plugin provides:

- **PreToolUse hooks** on `Bash` that block raw `git commit`, raw
  `gh pr create`, heredoc syntax, and (on repos that have adopted
  the worktree convention) commits originating outside
  `.worktrees/<name>/`.
- **PostToolUse hooks** that remind you to run `st-finalize-repo`
  after `st-submit-pr`, and that surface deprecation warnings in
  test output.
- **Stop hooks** that prevent session exit if a PR was submitted
  but never finalized.

See the plugin's own
[Hooks reference](https://github.com/wphillipmoore/standard-tooling-plugin/blob/develop/docs/site/docs/hooks/index.md)
for the full list.

!!! warning "Plugin install is a known rough edge"
    The plugin itself has not yet been cut as a proper versioned
    release; Claude Code consumes it directly from the repo's
    default branch. Updates can be slow to propagate and the local
    `~/.claude/plugins/marketplaces/` and `…/cache/` directories
    sometimes need manual refreshing. All tracked in
    [standard-tooling-plugin#46](https://github.com/wphillipmoore/standard-tooling-plugin/issues/46).
    The settings above are enough to get going; plan on
    occasionally running `git pull` in
    `~/.claude/plugins/marketplaces/standard-tooling-marketplace/`
    if a hook seems outdated.

## Step 6: Worktree convention

Every managed repo adopts the worktree convention so multiple
Claude Code agents can work in parallel without colliding. Two
tiny changes:

Add `.worktrees/` to your `.gitignore`:

```bash
echo '.worktrees/' >> .gitignore
```

Add a `## Parallel AI agent development` section to your
`CLAUDE.md`. Every managed repo has one you can copy — they differ
only in the repo name and example issue number. The canonical text
lives in the worktree convention spec at
`docs/specs/worktree-convention.md` in standard-tooling; a short
local section in each repo's CLAUDE.md is the on-ramp, and the
plugin's commit-block hook activates against the presence of
`.worktrees/` in `.gitignore`.

For how to actually use worktrees during development, see
[Git Workflow → Parallel work with worktrees](git-workflow.md#parallel-work-with-worktrees).

## Step 7: CI workflow

Use the `standards-compliance` composite action from
`wphillipmoore/standard-actions`. Minimal workflow
(`.github/workflows/ci.yml`):

```yaml
name: CI

on:
  pull_request:

permissions:
  contents: read

jobs:
  standards-compliance:
    name: "ci: standards-compliance"
    runs-on: ubuntu-latest
    container: ghcr.io/wphillipmoore/dev-base:latest
    steps:
      - name: Checkout code
        uses: actions/checkout@v6

      - name: Validate standards
        uses: wphillipmoore/standard-actions/actions/standards-compliance@develop
```

What the composite action runs inside the `dev-base` container:

- `st-repo-profile` — validates `docs/repository-standards.md`
- `st-pr-issue-linkage` — validates PR body has an issue linkage
  keyword (`Fixes`, `Closes`, `Resolves`, `Ref`)

The `dev-base` container already has standard-tooling on PATH, so
no further setup is needed in the workflow.

See the [CI Architecture](ci-architecture.md) guide if you also want
per-language test/lint/audit tiers.

## Step 8: Verify end-to-end

Once the above is in place, sanity-check each layer:

```bash
# 1. Host tools — should print st-docker-run help
st-docker-run --help

# 2. Dev container — pulls an image on first run and runs a
#    tiny command inside it
st-docker-run -- echo "container ok"

# 3. Repo profile — runs st-repo-profile inside the container
st-docker-run -- uv run st-repo-profile

# 4. Git hook — raw git commit should be blocked by the gate
git commit --allow-empty -m "test"     # expected: blocked

# 5. Plugin hook (requires Claude Code session) — in a Claude
#    Code session in this repo, have Claude try to run a raw
#    `git commit`. The plugin should block it and point at
#    st-commit.
```

If any step fails, check the corresponding section above, then
re-run. Common causes: `uv tool install` not run,
`.claude/settings.json` not committed to the branch your Claude
Code session loaded.

## Keeping up to date

After each `standard-tooling` release, upgrade the host tools:

```bash
uv tool upgrade standard-tooling
```

`uv tool upgrade` re-resolves the git reference, pulls the current
tip of the rolling minor tag, and rebuilds the isolated tool venv.
No need to repeat the full git URL.

The dev container images auto-update on `st-docker-run` pulls — the
tag is a minor version (`3.12`, `1.26`, etc.) that tracks upstream
releases. If you need a fresh image, `docker pull
ghcr.io/wphillipmoore/dev-<lang>:<version>` forces a refresh.

The Claude Code plugin can be stale after a release; manual
refresh:

```bash
git -C ~/.claude/plugins/marketplaces/standard-tooling-marketplace pull
# then restart Claude Code
```

## Troubleshooting

- **`st-docker-run: command not found`** — `uv tool install` has
  not been run, or `~/.local/bin` is not on PATH. Re-run the install
  command from Step 2 and confirm `which st-docker-run` resolves.
- **`manifest unknown` when pulling a container image** — the tag
  you're asking for doesn't exist on GHCR. Older docs referenced
  `ghcr.io/wphillipmoore/dev-docs:latest` (renamed to `dev-base` in
  standard-tooling#252); make sure your workflow files use the new
  name.
- **Plugin hooks don't fire** — check that `.claude/settings.json`
  is present, committed, and syntactically valid JSON. Restart
  Claude Code; the plugin is loaded at session start. If still
  stuck, refresh the local plugin marketplace clone per "Keeping
  up to date" above.
- **Commits blocked with "originate from inside .worktrees/"** —
  intentional. This repo has adopted the worktree convention.
  Create a worktree for your work; see
  [Git Workflow → Parallel work with worktrees](git-workflow.md#parallel-work-with-worktrees).
- **`st-submit-pr` threw a `CalledProcessError` on auto-merge** —
  expected since 2026-04-22. Auto-merge is disabled org-wide; the
  PR itself was created successfully. Tracked in
  [standard-tooling#268](https://github.com/wphillipmoore/standard-tooling/issues/268).
- **"raw 'git commit' is blocked"** — the `.githooks/pre-commit`
  gate is working as intended. Use `st-commit` instead of raw `git
  commit`.

For a broader troubleshooting index see
[Git Workflow → Troubleshooting](git-workflow.md#troubleshooting).

## Related

- [Git Workflow](git-workflow.md) — how the per-change cycle
  actually unfolds once setup is done
- [Git Hooks and Validation][hooks-doc] —
  pre-commit hook + validator reference
- [CI Architecture](ci-architecture.md) — per-language test/lint/audit
  tier wiring
- [Releasing](releasing.md) — release workflow
- [Worktree convention spec][worktree-spec]
  — canonical reference for the parallel-agent convention

[hooks-doc]: https://github.com/wphillipmoore/standard-tooling/blob/develop/docs/git-hooks-and-validation.md
[worktree-spec]: https://github.com/wphillipmoore/standard-tooling/blob/develop/docs/specs/worktree-convention.md
