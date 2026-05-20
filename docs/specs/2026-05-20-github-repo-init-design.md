# GitHub Repo Init Design (vrg-github-repo-init)

**Issue:** #807
**Date:** 2026-05-20
**Status:** Draft

## Problem

New VERGIL-managed repositories require manual bootstrapping steps
that are fragile, undocumented, and order-dependent. The process
involves: creating the repo, cloning, writing config files, making
an initial commit (which `vrg-commit` can't do on an empty repo),
setting up branch structure, and applying GitHub configuration.
Each step has its own failure mode, and the ordering matters —
rulesets require branches to exist, branches require commits, and
`vrg-commit` crashes on repos with no HEAD.

The workaround today is manual intervention: bootstrap shell
scripts, temporarily disabling rulesets via the GitHub web UI, etc.

## Solution

A human-facing interactive wizard (`vrg-github-repo-init`) that
walks through the full bootstrap sequence for a new
VERGIL-managed repository, from `gh repo create` to "ready for
PRs." The wizard commits each step as a checkpoint, enabling
idempotent resume on failure.

## Command Interface

### Entry Modes

| Mode | Invocation | Behavior |
|------|-----------|----------|
| New repo | `vrg-github-repo-init <org>/<name>` | Creates repo on GitHub, clones locally, runs wizard |
| Adopt existing | `vrg-github-repo-init --adopt` | Run from inside an existing clone. Overwrites all managed files to canonical state. |

### Flags

- `--adopt` — required for existing repos; confirms intent to
  vergilize. Without this flag, running inside an existing repo
  is an error.
- `--visibility {public,private}` — for new repos. Prompted
  interactively if omitted.

## Wizard Steps

The wizard runs nine steps in sequence. Each step that modifies the
local repo commits its work with a marker message:
`chore(init): step N - <description>`. On re-run, the tool scans
`git log` for these markers and skips completed steps.

| Step | Description | Checkpoint? | Idempotency check |
|------|-------------|-------------|-------------------|
| 1 | Repo creation/verification (with description) | No (remote) | `gh repo view` succeeds |
| 2 | Clone & working directory | No (local) | `.git/` exists with correct remote |
| 3 | Interactive vergil.toml generation | Yes | Commit marker in log |
| 4 | Scaffold local config files | Yes | Commit marker in log |
| 5 | CI + CD workflow generation | Yes | Commit marker in log |
| 6 | Docs site scaffold | Yes | Commit marker in log |
| 7 | Branch structure | No (remote) | Both branches exist on remote |
| 8 | GitHub config + labels | No (remote) | `vrg-github-repo-config audit` passes, standard labels exist |
| 9 | GitHub Pages | No (remote) | Pages source set, homepage URL set |

### Sequencing Constraint

Step 8 (rulesets) must come after step 7 (branches exist). This is
the core chicken-and-egg fix: branches are created before rulesets
are applied.

### Initial Commit Bypass

Steps 3-6 commit directly via `git commit` (not `vrg-commit`),
since `vrg-commit` requires a HEAD, a non-protected branch, and
a valid branch prefix — none of which exist during bootstrap.
The `.githooks/pre-commit` gate is not yet active (it's one of
the files being scaffolded), so raw `git commit` works. The
checkpoint commits use `chore(init):` prefix to stay
conventional-commit-compliant.

## Interactive Prompts

Prompts are split across steps — each value is collected when
it's first needed.

### Pre-step 1 prompts (new repos only)

1. **Visibility** — `public | private`
2. **Project description** — free-text, one paragraph. Used in
   README.md and as the GitHub repo description.

### Step 3 prompts (vergil.toml + file generation)

Each prompt presents valid enum values from the existing
`config.py` schema. Later prompts adjust defaults based on earlier
answers.

3. **Repository type** —
   `library | application | infrastructure | tooling | documentation`
4. **Primary language** —
   `python | go | java | ruby | rust | shell | none | claude-plugin`
5. **Branching model** —
   `library-release | application-promotion | docs-single-branch`
6. **Versioning scheme** —
   `library | semver | application | none`
7. **Release model** —
   `artifact-publishing | tagged-release | environment-promotion | none`
8. **CI versions** — free-text, comma-separated. Defaults by
   language (Python: `3.12, 3.13, 3.14`; shell: `latest`).
9. **Integration tests?** — yes/no (default: no)
10. **Publish releases?** — yes/no (default: yes if
    tagged-release, no if release-model is none)
11. **Publish docs?** — yes/no (default: yes)
12. **Vergil dependency version** — default: current latest
    (e.g., `v2.0`)
13. **License** — `GPL-3.0 | MIT | Apache-2.0 | none`

Defaults are shown in brackets (e.g., `[MIT]`) and accepted with
Enter.

### Adopt Mode: Pre-filling from Existing Config

When `--adopt` is used and a `vergil.toml` already exists, the
wizard pre-fills answers from the existing file. The user can
accept or change each value.

## Generated Files

### vergil.toml

Assembled from wizard answers:

```toml
[project]
repository-type = "tooling"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "shell"

[ci]
versions = ["latest"]
integration-tests = false

[publish]
release = true
docs = true

[dependencies]
vergil = "v2.0"
```

### .githooks/pre-commit

Identical to the existing gate script: admits
`VRG_COMMIT_CONTEXT=1` and derived workflows (amend, cherry-pick,
revert, rebase, merge), rejects raw `git commit`. Stored as a
template in `src/vergil_tooling/data/githooks_pre_commit.sh`.
After writing the file, step 4 also runs
`git config core.hooksPath .githooks` to activate the hook.

### CLAUDE.md

Repo-specific header (project name, validation command) followed
by the consumer template from `claude_md_consumer.md`. The header
is minimal — just enough to orient an agent.

### .claude/settings.json

Canonical structure:

```json
{
  "permissions": {
    "allow": ["Bash(vrg-*)"],
    "deny": [
      "Bash(git *)", "Bash(*/git *)",
      "Bash(gh *)", "Bash(*/gh *)"
    ]
  },
  "extraKnownMarketplaces": {
    "vergil-marketplace": {
      "source": {
        "source": "github",
        "repo": "vergil-project/vergil-claude-plugin"
      }
    }
  },
  "enabledPlugins": {
    "vergil@vergil-marketplace": true
  }
}
```

### README.md

Structured template with placeholder prose. The wizard prompts
for the one-paragraph description; the rest is derived from
context (project name, license, Pages URL).

```markdown
# <Project Name>

<one-paragraph description — what this is and why it exists>

## Status

<project status: e.g., "Early development", "Stable (v1.x)">

## Overview

<what the project does, its place in the ecosystem, key concepts>

## Getting Started

See the [documentation](<pages-url>).

## License

<license name> — see [LICENSE](LICENSE).
```

### .gitignore

Baseline patterns common to all Vergil-managed repos. Does not
include language-specific patterns — those are handled by
`vrg-scaffold` (#914).

```gitignore
# Editors
*.swp
*.swo
*~
.idea/
.vscode/

# OS
.DS_Store
Thumbs.db

# Vergil
.venv-host/
.worktrees/
```

### LICENSE

Full license text for the chosen license (GPL-3.0, MIT, or
Apache-2.0), with year and copyright holder derived from the
GitHub org. Skipped if `none`.

### .github/workflows/ci.yml

Uses `vergil-project/vergil-actions` reusable workflows. Jobs
derived from language and CI config: quality, security, test,
version-bump. Language determines CodeQL language and container
image suffix. Built programmatically, not from a Jinja template.

### .github/workflows/cd.yml

Generated when `publish.docs = true` (the default). Contains
the docs deployment job calling
`vergil-project/vergil-actions/cd-docs.yml` for deployment
via Mike to the `gh-pages` branch. Triggered on pushes to
`develop` and `main`.

Release publishing (package artifacts) is not configured by
this tool — it is language-specific and belongs in
`vrg-scaffold` (#914).

### docs/site/mkdocs.yml

Material theme, standard extensions (admonition, pymdownx
highlight/superfences/tabbed/snippets, tables, toc), strict mode,
standard navigation skeleton. `site_name` derived from repo name.

### docs/site/docs/index.md

Minimal placeholder page with the repo name as heading.

## GitHub Pages Setup (Step 9)

Skipped when `publish.docs = false`.

1. **Create `gh-pages` branch** — an empty orphan branch that
   Mike will deploy into. Push it to the remote.
2. **Configure Pages source** — `POST /repos/{owner}/{repo}/pages`
   with `{"source": {"branch": "gh-pages", "path": "/"}}`.
3. **Set homepage URL** — `PATCH /repos/{owner}/{repo}` with
   `{"homepage": "https://{org}.github.io/{name}/"}`.

The `gh-pages` branch is protected from deletion by
`vrg-finalize-repo` (eternal branch list). The actual docs
content is deployed by the `cd.yml` workflow via Mike on pushes
to `develop` and `main`.

## Adopt Mode

### Pre-flight Checks

1. Verify CWD is a git repo with a remote.
2. Verify the remote repo exists on GitHub.
3. Print what will be overwritten and require y/n confirmation.

### Differences from New-Repo Mode

- **Step 1:** verify remote instead of create.
- **Step 2:** verify CWD instead of clone.
- **Steps 3-6:** identical — generate and commit, overwriting
  existing files.
- **Step 7:** create only missing branches. If `main` or
  `develop` already exist, skip. Set default branch to `develop`
  if not already.
- **Steps 8-9:** identical.
### Existing vergil.toml Handling

If one exists, the wizard pre-fills prompts from it. The user can
accept or change each value.

## Error Handling & Idempotency

### Checkpoint Detection on Resume

The tool scans `git log --oneline` for `chore(init): step N -`
markers. Each detected marker means that step is complete:

```
Step 3 (vergil.toml): already completed, skipping
Step 4 (config files): already completed, skipping
Step 5 (CI workflow): resuming from here...
```

### Remote-Only Steps

Steps without commit markers query actual state:

- **Step 1:** `gh repo view org/name` succeeds
- **Step 2:** `.git/config` remote matches expected repo
- **Step 7:** both branches exist on remote
- **Step 8:** `vrg-github-repo-config audit` returns compliant
- **Step 9:** Pages source is set to `gh-pages`, homepage URL is set

### Failure Recovery

| Failure | Recovery on re-run |
|---------|-------------------|
| `gh repo create` fails | Step 1 retries — repo doesn't exist |
| File generation crashes mid-step | No marker written — step reruns |
| `git push` fails after local commits | Checkpoints exist locally, remote steps retry |
| Ruleset apply fails | Step 8 retries — `apply_desired_state` is idempotent |

### Atomicity Within Steps

Each step stages all its files and commits atomically. If the
process dies between staging and committing, there are no markers
and the step reruns cleanly.

### Resume Behavior

The tool auto-detects checkpoint state on every run. If
checkpoint commits or completed remote state are found, the
tool prints what it's skipping and resumes from the next
incomplete step. No special flag is needed.

## Code Structure

### Entry Point

`src/vergil_tooling/bin/vrg_github_repo_init.py` — registered as
`vrg-github-repo-init` in `pyproject.toml`.

### Internal Organization

```
src/vergil_tooling/bin/vrg_github_repo_init.py    # argparse, main()
src/vergil_tooling/lib/repo_init.py                # step logic, wizard, templates
```

### repo_init.py Contents

- `RepoInitContext` dataclass — wizard state (org, name,
  visibility, config choices, working directory, completed steps)
- `detect_completed_steps()` — scans git log and remote state,
  returns set of completed step numbers
- One function per step: `step_create_repo()`,
  `step_clone()`, `step_generate_config()`, etc.
- `run_wizard()` — orchestrator calling steps in sequence,
  skipping completed ones
- Prompt helpers — wrappers around `input()` presenting choices
  with defaults

### Template Files

Added to `src/vergil_tooling/data/`:

- `githooks_pre_commit.sh` — the pre-commit gate script
- `claude_settings.json` — canonical `.claude/settings.json`
- `claude_md_consumer.md` — already exists
- `licenses/gpl-3.0.txt`, `licenses/mit.txt`,
  `licenses/apache-2.0.txt` — full license texts

### Existing Code Reuse

- `vergil_tooling.lib.github` — `gh` CLI wrappers for repo
  creation, branch queries
- `vergil_tooling.lib.github_config` — `apply_desired_state()`
  for step 8
- `vrg-ensure-label` — applies standard label set from
  `labels.json` in step 8
- `vergil_tooling.lib.config` — `StConfig` dataclass and enum
  validation

### CI Workflow Generation

Built programmatically from language and CI config. No Jinja
templates — the tool constructs the YAML structure following
vergil-actions conventions (quality, security, test, version-bump
jobs with language-specific inputs).

## Runtime

This is a host-side tool (like `vrg-commit`, `vrg-submit-pr`).
It runs directly on macOS/Linux and requires `gh` auth and `git`
on the host. It needs GitHub admin access for repo creation,
ruleset management, and Pages configuration.

## Test Case

The first real-world use will be creating `vergil-project/vergil-vm`
— a shell-language, library-release, tagged-release tooling repo
modeled after `vergil-docker`.
