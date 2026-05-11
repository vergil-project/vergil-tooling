# VERGIL Rename Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the standard-tooling project suite to VERGIL across
all four core repos, a new GitHub org, and all consumer repos.

**Architecture:** Two-phase execution. Phase 1 creates the
`vergil-project` GitHub org and processes the four core repos in
dependency order (docker → actions → tooling → plugin), transferring,
renaming, updating internal references, and releasing v2.0.0 of each.
Phase 2 sweeps all consumer repos to update their references and
releases a minor version bump for each.

**Tech Stack:** GitHub CLI (`gh`), Python (pyproject.toml, setuptools),
Git, GHCR (container registry), GitHub Actions workflows

**Spec:**
[`docs/specs/2026-05-11-vergil-rename-design.md`](../specs/2026-05-11-vergil-rename-design.md)

---

## File Map — Key Changes by Repo

### vergil-docker (standard-tooling-docker)

| File | Change |
|---|---|
| `.github/workflows/cd-docker-publish.yml` | `ghcr.io/wphillipmoore` → `ghcr.io/vergil-project`; `wphillipmoore/standard-actions` → `vergil-project/vergil-actions` |
| `.github/workflows/ci.yml`, `cd.yml`, `ops.yml` | `wphillipmoore/standard-actions` → `vergil-project/vergil-actions` |
| `.githooks/pre-commit` | `st-commit` → `vrg-commit`, `ST_COMMIT_CONTEXT` → `VRG_COMMIT_CONTEXT` |
| `docker/build.sh` | Header comment: repo URL |
| `CLAUDE.md`, `README.md`, `CHANGELOG.md` | All `standard-tooling-docker` → `vergil-docker` references |
| `standard-tooling.toml` | Rename file → `vergil.toml`; update `[dependencies]` key |

### vergil-actions (standard-actions)

| File | Change |
|---|---|
| `actions/shared/setup/standard-tooling/action.yml` | Rename dir → `actions/shared/setup/vergil/action.yml`; update sed patterns and install URLs |
| `.github/workflows/*.yml` | Self-references and `wphillipmoore/standard-actions` → `vergil-project/vergil-actions`; `ghcr.io/wphillipmoore` → `ghcr.io/vergil-project` |
| `actions/**/*.yml` | All action files: `standard-tooling` → `vergil-tooling`, `standard-actions` → `vergil-actions` |
| `.githooks/pre-commit` | `st-commit` → `vrg-commit`, `ST_COMMIT_CONTEXT` → `VRG_COMMIT_CONTEXT` |
| `CLAUDE.md`, `AGENTS.md`, `README.md` | All name references |
| `standard-tooling.toml` | Rename → `vergil.toml`; update fields |

### vergil-tooling (standard-tooling)

| File | Change |
|---|---|
| `src/standard_tooling/` | Rename dir → `src/vergil_tooling/` |
| `src/vergil_tooling/bin/st_*.py` (×18) | Rename each → `vrg_*.py` |
| `tests/standard_tooling/` | Rename dir → `tests/vergil_tooling/` |
| `tests/vergil_tooling/test_st_*.py` (×18) | Rename each → `test_vrg_*.py` |
| `pyproject.toml` | Package name, all console scripts |
| `src/vergil_tooling/lib/config.py` | `CONFIG_FILE`, dependency key, `st_install_tag` → `vrg_install_tag`, `ST_DOCKER_INSTALL_TAG` → `VRG_DOCKER_INSTALL_TAG` |
| `src/vergil_tooling/lib/git.py` | `_GATE_ENV_VAR = "ST_COMMIT_CONTEXT"` → `"VRG_COMMIT_CONTEXT"` |
| `src/vergil_tooling/lib/docker.py` | `_GHCR = "ghcr.io/wphillipmoore"` → `"ghcr.io/vergil-project"` |
| `src/vergil_tooling/lib/docker_cache.py` | Import `vrg_install_tag` |
| All `.py` files | `from standard_tooling` → `from vergil_tooling`; `import standard_tooling` → `import vergil_tooling` |
| All `.py` bin files | String refs: `st-validate` → `vrg-validate`, `st-docker-run` → `vrg-docker-run`, etc. |
| `.githooks/pre-commit` | `st-commit` → `vrg-commit`, `ST_COMMIT_CONTEXT` → `VRG_COMMIT_CONTEXT` |
| `standard-tooling.toml` | Rename → `vergil.toml`; update fields |
| `.github/workflows/*.yml` | `wphillipmoore/standard-actions` → `vergil-project/vergil-actions` |
| `CLAUDE.md`, `AGENTS.md`, `README.md` | All references |
| All docs under `docs/` | All `standard-tooling`, `st-*` references |

### vergil-claude-plugin (standard-tooling-plugin)

| File | Change |
|---|---|
| `.claude-plugin/plugin.json` | `"name": "standard-tooling"` → `"vergil"` |
| `.claude-plugin/marketplace.json` | Update name, repository URL |
| `skills/*/SKILL.md` (×8) | All `standard-tooling` references, all `st-*` command references |
| `hooks/hooks.json` | Update any `standard-tooling` references |
| `hooks/scripts/*.sh` | `st-commit` → `vrg-commit`, `standard-tooling` → `vergil-tooling` |
| `hooks/scripts/lib/*.sh` | `standard-tooling.toml` → `vergil.toml`, `st-*` → `vrg-*` |
| `.githooks/pre-commit` | `st-commit` → `vrg-commit`, `ST_COMMIT_CONTEXT` → `VRG_COMMIT_CONTEXT` |
| `.github/workflows/*.yml` | `wphillipmoore/standard-actions` → `vergil-project/vergil-actions` |
| `CLAUDE.md`, `AGENTS.md`, `README.md` | All references |
| `standard-tooling.toml` | Rename → `vergil.toml`; update fields |

---

## Task 1: Pre-flight Checks and Org Creation

**Files:** None (GitHub admin operations)

- [ ] **Step 1: Verify all repos are clean**

For each repo, confirm no open PRs and clean working state:

```bash
for repo in standard-tooling standard-actions standard-tooling-docker standard-tooling-plugin; do
  echo "=== $repo ==="
  gh pr list --repo "wphillipmoore/$repo" --state open
done
```

Expected: Zero open PRs on each repo. If any exist, merge or close
them before proceeding.

- [ ] **Step 2: Verify consumer repos are clean**

```bash
gh repo list wphillipmoore --limit 100 --json name,isArchived \
  --jq '.[] | select(.isArchived == false) | .name' \
  | while read -r repo; do
    count=$(gh pr list --repo "wphillipmoore/$repo" --state open --json number --jq 'length' 2>/dev/null)
    if [ "${count:-0}" -gt 0 ]; then
      echo "OPEN PRs: $repo ($count)"
    fi
  done
```

Expected: No open PRs on any active consumer repo.

- [ ] **Step 3: Create the vergil-project GitHub org**

This must be done via the GitHub web UI (Settings → Organizations →
New organization). Select the free plan.

1. Go to <https://github.com/organizations/plan>
2. Choose "Free"
3. Org name: `vergil-project`
4. Contact email: your GitHub email
5. Select "My personal account" as the owner

- [ ] **Step 4: Verify org creation**

```bash
gh api orgs/vergil-project --jq '.login'
```

Expected: `vergil-project`

- [ ] **Step 5: Commit checkpoint**

No files changed — this is an admin-only task. Proceed to Task 2.

---

## Task 2: Org Infrastructure Setup

**Files:** None (GitHub admin operations)

This task configures the org-level infrastructure that the repos'
CI/CD pipelines depend on. Without this, transferred repos will fail
to publish images, create releases, or run workflows. This setup is
reusable for future org creations (e.g., diogenes-project).

- [ ] **Step 1: Configure GitHub Actions permissions**

Via GitHub UI: `vergil-project` → Settings → Actions → General

1. Actions permissions: "Allow vergil-project, and select
   non-vergil-project, actions and reusable workflows"
2. Under "Allow specified actions and reusable workflows", add:
   - `actions/*`
   - `astral-sh/*`
   - `docker/*`
   - `github/*`
   - `pypa/*`
   - `vergil-project/*`
3. Workflow permissions: "Read and write permissions"
4. Check "Allow GitHub Actions to create and approve pull requests"

Verify via API:

```bash
gh api orgs/vergil-project/actions/permissions --jq '.'
```

- [ ] **Step 2: Install the GitHub App on the org**

The automation GitHub App (used for elevated-permission tokens in
release workflows and cross-repo operations) must be installed on the
`vergil-project` org.

Via GitHub UI: go to the app's settings page → Install → select
`vergil-project` → grant access to all repositories.

Verify:

```bash
gh api orgs/vergil-project/installations --jq '.[].app_slug'
```

- [ ] **Step 3: Create org-level secrets**

Set up secrets at the org level so transferred repos inherit them
automatically:

```bash
gh secret set APP_ID --org vergil-project --body "<value>"
gh secret set APP_PRIVATE_KEY --org vergil-project --body "<value>"
gh secret set PROJECT_TOKEN --org vergil-project --body "<value>"
```

For the vergil-actions-specific secret:

```bash
gh secret set PR_BUMP_TOKEN --org vergil-project --body "<value>"
```

Note: retrieve the current secret values from the existing repos
before transferring them. Secrets cannot be read via API — check
your password manager or regenerate them.

Verify secrets are set:

```bash
gh secret list --org vergil-project
```

Expected: `APP_ID`, `APP_PRIVATE_KEY`, `PROJECT_TOKEN`,
`PR_BUMP_TOKEN` all listed.

- [ ] **Step 4: Remove stale SONAR_TOKEN secrets**

SonarCloud was dropped (not cost-effective at scale, and open-source
tools provide equivalent coverage). Clean up the stale secrets from
repos that still have them:

```bash
gh secret delete SONAR_TOKEN --repo wphillipmoore/standard-tooling 2>/dev/null
gh secret delete SONAR_TOKEN --repo wphillipmoore/standard-actions 2>/dev/null
```

- [ ] **Step 5: Enable GitHub Pages at the org level**

Via GitHub UI: `vergil-project` → Settings → Pages

Enable Pages for the org. All repos publish docs via GitHub Pages by
default.

- [ ] **Step 6: Verify GitHub Packages / GHCR access**

Ensure the org has Packages enabled and that workflows can publish
container images:

Via GitHub UI: `vergil-project` → Settings → Packages

Verify that "Inherit access from source repository" is enabled for
container images (this is the default).

- [ ] **Step 7: Proceed to Task 3**

Org infrastructure is ready. Repos transferred after this point will
inherit Actions permissions, secrets, and Packages access.

---

## Task 3: vergil-docker — Transfer, Rename, Update, and Republish

**Repo:** `wphillipmoore/standard-tooling-docker` →
`vergil-project/vergil-docker`

- [ ] **Step 1: Transfer repo to org**

```bash
gh api repos/wphillipmoore/standard-tooling-docker/transfer \
  -f new_owner=vergil-project --silent
```

Wait ~30 seconds for GitHub to process.

- [ ] **Step 2: Rename repo**

```bash
gh repo rename vergil-docker --repo vergil-project/standard-tooling-docker --yes
```

- [ ] **Step 3: Verify transfer and rename**

```bash
gh repo view vergil-project/vergil-docker --json nameWithOwner --jq '.nameWithOwner'
```

Expected: `vergil-project/vergil-docker`

- [ ] **Step 4: Clone fresh and create release branch**

```bash
cd ~/dev/github
git clone git@github.com:vergil-project/vergil-docker.git
cd vergil-docker
git checkout -b release/2.0.0
```

- [ ] **Step 5: Discover all references to update**

```bash
grep -rn "standard-tooling\|standard_tooling\|wphillipmoore\|st-commit\|ST_COMMIT_CONTEXT\|ghcr.io/wphillipmoore" \
  --include="*.yml" --include="*.yaml" --include="*.sh" \
  --include="*.md" --include="*.toml" --include="*.json" .
```

Review the output and update each file. The known targets are listed
in the File Map above. Key changes:

- [ ] **Step 6: Update workflow files**

In `.github/workflows/cd-docker-publish.yml`, replace all:
- `ghcr.io/wphillipmoore` → `ghcr.io/vergil-project`
- `wphillipmoore/standard-actions` → `vergil-project/vergil-actions`

Repeat for `ci.yml`, `cd.yml`, `ops.yml`.

- [ ] **Step 7: Update pre-commit hook**

In `.githooks/pre-commit`:
- `st-commit` → `vrg-commit`
- `ST_COMMIT_CONTEXT` → `VRG_COMMIT_CONTEXT`

- [ ] **Step 8: Rename config file and update contents**

```bash
git mv standard-tooling.toml vergil.toml
```

In `vergil.toml`:
- `[dependencies]` key: `standard-tooling = "..."` → `vergil = "v2.0"`

- [ ] **Step 9: Update docs and README**

Replace all `standard-tooling-docker` with `vergil-docker` and all
`wphillipmoore` with `vergil-project` in `README.md`, `CLAUDE.md`,
and any files under `docs/`.

- [ ] **Step 10: Update VERSION file**

```bash
echo "2.0.0" > VERSION
```

- [ ] **Step 11: Commit all changes**

```bash
git add -A
vrg-commit  # or st-commit if host tools not yet updated
```

Commit message: `feat!: rename to vergil-docker under vergil-project org`

- [ ] **Step 12: Push and open PR**

```bash
git push -u origin release/2.0.0
```

Open PR, wait for CI. Fix any failures, iterate.

- [ ] **Step 13: Re-publish container images**

After merging, trigger the CD workflow to publish images to the new
GHCR scope (`ghcr.io/vergil-project/*`). The old images at
`ghcr.io/wphillipmoore/*` remain available — do NOT delete them until
all consumers are swept.

Verify at least one image is available:

```bash
gh api orgs/vergil-project/packages?package_type=container --jq '.[].name'
```

- [ ] **Step 14: Tag release**

Follow the repo's release workflow to create the `v2.0.0` tag and
GitHub release.

---

## Task 4: vergil-actions — Transfer, Rename, Update, and Release

**Repo:** `wphillipmoore/standard-actions` →
`vergil-project/vergil-actions`

- [ ] **Step 1: Transfer repo to org**

```bash
gh api repos/wphillipmoore/standard-actions/transfer \
  -f new_owner=vergil-project --silent
```

- [ ] **Step 2: Rename repo**

```bash
gh repo rename vergil-actions --repo vergil-project/standard-actions --yes
```

- [ ] **Step 3: Verify**

```bash
gh repo view vergil-project/vergil-actions --json nameWithOwner --jq '.nameWithOwner'
```

Expected: `vergil-project/vergil-actions`

- [ ] **Step 4: Clone fresh and create release branch**

```bash
cd ~/dev/github
git clone git@github.com:vergil-project/vergil-actions.git
cd vergil-actions
git checkout -b release/2.0.0
```

- [ ] **Step 5: Rename the setup action directory**

The setup action at `actions/shared/setup/standard-tooling/` must be
renamed:

```bash
git mv actions/shared/setup/standard-tooling actions/shared/setup/vergil
```

- [ ] **Step 6: Update the setup action**

In `actions/shared/setup/vergil/action.yml`:
- Name: `Install standard-tooling` → `Install vergil-tooling`
- Description: update references
- `pyproject.toml` detection: `standard-tooling` → `vergil-tooling`
- Config file: `standard-tooling.toml` → `vergil.toml`
- sed pattern: `standard-tooling` → `vergil`
- Install URL: `standard-tooling @ git+https://github.com/wphillipmoore/standard-tooling` → `vergil-tooling @ git+https://github.com/vergil-project/vergil-tooling`

Updated action content:

```yaml
name: Install vergil-tooling
description: >-
  Reads the pinned vergil version from vergil.toml
  and installs it via uv tool install. When the consumer repo is
  vergil-tooling itself, runs uv sync to install from source so CI
  validates the PR's code instead of the released version.

runs:
  using: composite
  steps:
    - name: Mark workspace safe for git
      shell: bash
      run: git config --global --add safe.directory "${GITHUB_WORKSPACE:-$(pwd)}"

    - name: Install vergil-tooling
      shell: bash
      run: |
        if grep -q '^name = "vergil-tooling"' pyproject.toml 2>/dev/null; then
          echo "Consumer repo is vergil-tooling itself — installing from source"
          uv sync --frozen --group dev
          echo "$GITHUB_WORKSPACE/.venv/bin" >> "$GITHUB_PATH"
          exit 0
        fi

        TAG=$(sed -n 's/^[[:space:]]*vergil[[:space:]]*=[[:space:]]*"\(.*\)"/\1/p' vergil.toml)
        if [ -z "${TAG:-}" ]; then
          echo "::error::vergil.toml not found or missing version tag"
          exit 1
        fi
        uv tool install "vergil-tooling @ git+https://github.com/vergil-project/vergil-tooling@${TAG}"
```

- [ ] **Step 7: Update all workflow files**

Across all `.github/workflows/*.yml`:
- `wphillipmoore/standard-actions` → `vergil-project/vergil-actions`
  (self-references)
- `ghcr.io/wphillipmoore` → `ghcr.io/vergil-project`
- `standard-tooling` → `vergil-tooling` where it refers to the
  package

```bash
find .github/workflows -name '*.yml' -exec grep -l 'wphillipmoore\|standard-actions\|standard-tooling' {} \;
```

Update each file found.

- [ ] **Step 8: Update all action.yml files**

```bash
find actions -name 'action.yml' -exec grep -l 'wphillipmoore\|standard-tooling\|standard-actions' {} \;
```

Update each file: references to `standard-tooling` (the package),
`standard-actions` (self-references), and `wphillipmoore` (org).

- [ ] **Step 9: Update pre-commit hook, config, docs**

- `.githooks/pre-commit`: `st-commit` → `vrg-commit`,
  `ST_COMMIT_CONTEXT` → `VRG_COMMIT_CONTEXT`
- `git mv standard-tooling.toml vergil.toml` and update contents
- Update `CLAUDE.md`, `AGENTS.md`, `README.md`, all `docs/` files
- Update `VERSION` → `2.0.0`

- [ ] **Step 10: Commit, push, and open PR**

```bash
git add -A
```

Commit message: `feat!: rename to vergil-actions under vergil-project org`

Push, open PR, wait for CI. Note: CI workflows reference themselves,
so there may be a bootstrap issue — the PR's workflows reference
`vergil-project/vergil-actions` but the tag `v2.0` doesn't exist yet.
If this happens, temporarily pin to the branch name or the commit SHA,
then update to `@v2.0` after the first release.

- [ ] **Step 11: Merge and release v2.0.0**

After CI passes, merge. Create the `v2.0.0` tag and `v2.0` tracking
tag.

---

## Task 5: vergil-tooling — Transfer, Rename, and Directory Renames

**Repo:** `wphillipmoore/standard-tooling` →
`vergil-project/vergil-tooling`

- [ ] **Step 1: Transfer repo to org**

```bash
gh api repos/wphillipmoore/standard-tooling/transfer \
  -f new_owner=vergil-project --silent
```

- [ ] **Step 2: Rename repo**

```bash
gh repo rename vergil-tooling --repo vergil-project/standard-tooling --yes
```

- [ ] **Step 3: Verify**

```bash
gh repo view vergil-project/vergil-tooling --json nameWithOwner --jq '.nameWithOwner'
```

Expected: `vergil-project/vergil-tooling`

- [ ] **Step 4: Update local remote**

From the existing checkout:

```bash
cd ~/dev/github/standard-tooling
git remote set-url origin git@github.com:vergil-project/vergil-tooling.git
git fetch origin
```

- [ ] **Step 5: Create release branch**

```bash
git checkout -b release/2.0.0
```

- [ ] **Step 6: Rename Python source directory**

```bash
git mv src/standard_tooling src/vergil_tooling
```

- [ ] **Step 7: Rename test directory**

```bash
git mv tests/standard_tooling tests/vergil_tooling
```

- [ ] **Step 8: Commit directory renames**

```bash
git add -A
```

Commit message: `refactor!: rename standard_tooling module to vergil_tooling`

This is committed separately to preserve `git log --follow` history
for the directories.

---

## Task 6: vergil-tooling — Rename Bin and Test Files

**Files:**
- Rename: `src/vergil_tooling/bin/st_*.py` (×18) → `vrg_*.py`
- Rename: `tests/vergil_tooling/test_st_*.py` (×18) → `test_vrg_*.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Rename all bin files**

```bash
cd src/vergil_tooling/bin
for f in st_*.py; do
  new="vrg_${f#st_}"
  git mv "$f" "$new"
done
cd ../../..
```

This renames:
- `st_check_pr_merge.py` → `vrg_check_pr_merge.py`
- `st_commit.py` → `vrg_commit.py`
- `st_docker_cache.py` → `vrg_docker_cache.py`
- `st_docker_docs.py` → `vrg_docker_docs.py`
- `st_docker_run.py` → `vrg_docker_run.py`
- `st_docker_test.py` → `vrg_docker_test.py`
- `st_ensure_label.py` → `vrg_ensure_label.py`
- `st_finalize_repo.py` → `vrg_finalize_repo.py`
- `st_generate_commands.py` → `vrg_generate_commands.py`
- `st_github_config.py` → `vrg_github_config.py`
- `st_merge_when_green.py` → `vrg_merge_when_green.py`
- `st_pr_issue_linkage.py` → `vrg_pr_issue_linkage.py`
- `st_prepare_release.py` → `vrg_prepare_release.py`
- `st_repo_profile.py` → `vrg_repo_profile.py`
- `st_submit_pr.py` → `vrg_submit_pr.py`
- `st_validate.py` → `vrg_validate.py`
- `st_version.py` → `vrg_version.py`
- `st_wait_until_green.py` → `vrg_wait_until_green.py`

Note: `validate_common.py` has no `st_` prefix — leave it as-is.

- [ ] **Step 2: Rename all test files**

```bash
cd tests/vergil_tooling
for f in test_st_*.py; do
  new="test_vrg_${f#test_st_}"
  git mv "$f" "$new"
done
cd ../..
```

Note: test files without `st_` prefix (`test_config.py`,
`test_docker.py`, `test_docker_cache.py`, `test_git.py`,
`test_github.py`, `test_github_config_lib.py`, `test_labels.py`,
`test_release.py`, `test_validate_commands.py`,
`test_validate_common.py`, `test_version.py`) stay as-is.

- [ ] **Step 3: Update pyproject.toml — package name, metadata, and package-data**

Change the `[project]` section:

```toml
[project]
name = "vergil-tooling"
version = "2.0.0"
description = "VERGIL — Validation Engine for Repository Governance, Integration & Lifecycle"
```

Update `[tool.setuptools.package-data]`:

```toml
[tool.setuptools.package-data]
vergil_tooling = ["data/*.json", "configs/*.yaml", "configs/*.toml", "configs/ruby/*.yml"]
```

- [ ] **Step 4: Update pyproject.toml — console scripts**

Replace the entire `[project.scripts]` section:

```toml
[project.scripts]
vrg-check-pr-merge = "vergil_tooling.bin.vrg_check_pr_merge:main"
vrg-commit = "vergil_tooling.bin.vrg_commit:main"
vrg-docker-cache = "vergil_tooling.bin.vrg_docker_cache:main"
vrg-docker-docs = "vergil_tooling.bin.vrg_docker_docs:main"
vrg-docker-run = "vergil_tooling.bin.vrg_docker_run:main"
vrg-docker-test = "vergil_tooling.bin.vrg_docker_test:main"
vrg-ensure-label = "vergil_tooling.bin.vrg_ensure_label:main"
vrg-finalize-repo = "vergil_tooling.bin.vrg_finalize_repo:main"
vrg-generate-commands = "vergil_tooling.bin.vrg_generate_commands:main"
vrg-github-config = "vergil_tooling.bin.vrg_github_config:main"
vrg-merge-when-green = "vergil_tooling.bin.vrg_merge_when_green:main"
vrg-pr-issue-linkage = "vergil_tooling.bin.vrg_pr_issue_linkage:main"
vrg-prepare-release = "vergil_tooling.bin.vrg_prepare_release:main"
vrg-repo-profile = "vergil_tooling.bin.vrg_repo_profile:main"
vrg-submit-pr = "vergil_tooling.bin.vrg_submit_pr:main"
vrg-validate = "vergil_tooling.bin.vrg_validate:main"
vrg-version = "vergil_tooling.bin.vrg_version:main"
vrg-wait-until-green = "vergil_tooling.bin.vrg_wait_until_green:main"
```

- [ ] **Step 5: Commit file renames and pyproject.toml**

```bash
git add -A
```

Commit message: `refactor!: rename st-* CLI entry points to vrg-*`

---

## Task 7: vergil-tooling — Update Python Imports and Code Constants

**Files:**
- Modify: All `.py` files under `src/vergil_tooling/` and
  `tests/vergil_tooling/`

- [ ] **Step 1: Update all Python imports**

```bash
find src tests -name '*.py' -exec sed -i '' \
  -e 's/from standard_tooling/from vergil_tooling/g' \
  -e 's/import standard_tooling/import vergil_tooling/g' \
  {} +
```

Verify no old imports remain:

```bash
grep -rn "standard_tooling" src/ tests/ --include="*.py"
```

Expected: Zero matches.

- [ ] **Step 2: Update config.py constants**

In `src/vergil_tooling/lib/config.py`:

```python
CONFIG_FILE = "vergil.toml"
```

Update the validation logic:

```python
    if "vergil" not in deps:
        msg = f"{CONFIG_FILE}: [dependencies] must contain 'vergil'"
```

Rename the function and update its internals:

```python
def vrg_install_tag(repo_root: Path) -> str:
    """Return the ``[dependencies].vergil`` value for runtime install.

    Checks ``VRG_DOCKER_INSTALL_TAG`` env var first (override).
    """
    override = os.environ.get("VRG_DOCKER_INSTALL_TAG")
    if override:
        return override
    cfg = read_config(repo_root)
    return cfg.dependencies["vergil"]
```

- [ ] **Step 3: Update git.py env var**

In `src/vergil_tooling/lib/git.py`:

```python
_GATE_ENV_VAR = "VRG_COMMIT_CONTEXT"
```

- [ ] **Step 4: Update docker.py GHCR prefix**

In `src/vergil_tooling/lib/docker.py`:

```python
_GHCR = "ghcr.io/vergil-project"
```

- [ ] **Step 5: Update docker_cache.py import**

In `src/vergil_tooling/lib/docker_cache.py`:

```python
from vergil_tooling.lib.config import vrg_install_tag
```

And update the call site:

```python
        tag = vrg_install_tag(repo_root)
```

- [ ] **Step 6: Verify no old constant references remain**

```bash
grep -rn "ST_COMMIT_CONTEXT\|ST_DOCKER_INSTALL_TAG\|st_install_tag\|ghcr.io/wphillipmoore" \
  src/ --include="*.py"
```

Expected: Zero matches.

- [ ] **Step 7: Commit**

```bash
git add -A
```

Commit message: `refactor!: update imports, constants, and env vars for vergil rename`

---

## Task 8: vergil-tooling — Update String References in CLI Tools

**Files:**
- Modify: All `src/vergil_tooling/bin/vrg_*.py` files

- [ ] **Step 1: Bulk-replace st- command names in string literals**

```bash
find src/vergil_tooling/bin -name '*.py' -exec sed -i '' \
  -e 's/st-validate-custom/vrg-validate-custom/g' \
  -e 's/st-validate/vrg-validate/g' \
  -e 's/st-docker-run/vrg-docker-run/g' \
  -e 's/st-docker-test/vrg-docker-test/g' \
  -e 's/st-docker-cache/vrg-docker-cache/g' \
  -e 's/st-docker-docs/vrg-docker-docs/g' \
  -e 's/st-commit/vrg-commit/g' \
  -e 's/st-submit-pr/vrg-submit-pr/g' \
  -e 's/st-prepare-release/vrg-prepare-release/g' \
  -e 's/st-merge-when-green/vrg-merge-when-green/g' \
  -e 's/st-finalize-repo/vrg-finalize-repo/g' \
  -e 's/st-ensure-label/vrg-ensure-label/g' \
  -e 's/st-wait-until-green/vrg-wait-until-green/g' \
  -e 's/st-check-pr-merge/vrg-check-pr-merge/g' \
  -e 's/st-pr-issue-linkage/vrg-pr-issue-linkage/g' \
  -e 's/st-generate-commands/vrg-generate-commands/g' \
  -e 's/st-github-config/vrg-github-config/g' \
  -e 's/st-repo-profile/vrg-repo-profile/g' \
  -e 's/st-version/vrg-version/g' \
  {} +
```

- [ ] **Step 2: Update docstrings referencing standard-tooling**

```bash
find src/vergil_tooling -name '*.py' -exec sed -i '' \
  -e 's/standard-tooling\.toml/vergil.toml/g' \
  -e 's/standard-tooling/vergil-tooling/g' \
  {} +
```

- [ ] **Step 3: Spot-check key files**

Review these files manually to confirm string references are correct:
- `src/vergil_tooling/bin/vrg_validate.py` — help text, error
  messages, `prog=` argument
- `src/vergil_tooling/bin/vrg_docker_run.py` — usage string, help
  text
- `src/vergil_tooling/bin/vrg_commit.py` — docstring referencing
  config file
- `src/vergil_tooling/bin/vrg_finalize_repo.py` — subprocess calls to
  `vrg-docker-run` and `vrg-validate`

- [ ] **Step 4: Update dependency-key references in test fixtures**

The TOML dependency key renames to `vergil` (not `vergil-tooling`).
These targeted replacements must run **before** the blanket
`standard-tooling` → `vergil-tooling` pattern in Step 5, or the
wrong substitution wins.

```bash
find tests/vergil_tooling -name '*.py' -exec sed -i '' \
  -e 's/standard-tooling = "v1\.4"/vergil = "v2.0"/g' \
  -e 's/dependencies\["standard-tooling"\]/dependencies["vergil"]/g' \
  -e 's/match="standard-tooling"/match="vergil"/g' \
  {} +
```

Review `test_config.py` and `test_docker_cache.py` manually — both
have TOML fixture strings where the dependency key appears.

- [ ] **Step 5: Update string references in lib and test files**

```bash
find src/vergil_tooling/lib tests/vergil_tooling -name '*.py' -exec sed -i '' \
  -e 's/standard-tooling\.toml/vergil.toml/g' \
  -e 's/standard-tooling/vergil-tooling/g' \
  -e 's/st-commit/vrg-commit/g' \
  -e 's/st-validate/vrg-validate/g' \
  -e 's/st-docker-run/vrg-docker-run/g' \
  -e 's/st-finalize-repo/vrg-finalize-repo/g' \
  -e 's/ST_COMMIT_CONTEXT/VRG_COMMIT_CONTEXT/g' \
  -e 's/ST_DOCKER_INSTALL_TAG/VRG_DOCKER_INSTALL_TAG/g' \
  -e 's/st_install_tag/vrg_install_tag/g' \
  {} +
```

- [ ] **Step 6: Verify no old string references remain**

```bash
grep -rn '"st-' src/ tests/ --include="*.py"
grep -rn "standard-tooling" src/ tests/ --include="*.py"
grep -rn "ST_COMMIT_CONTEXT\|ST_DOCKER_INSTALL_TAG\|st_install_tag" tests/ --include="*.py"
```

Expected: Zero matches (excluding any that legitimately reference the
old name in a historical context — these should be rare).

- [ ] **Step 7: Commit**

```bash
git add -A
```

Commit message: `refactor!: update CLI command names and string references to vrg-*`

---

## Task 9: vergil-tooling — Config, Hooks, Docs, and Workflows

**Files:**
- Modify: `standard-tooling.toml` (rename), `.githooks/pre-commit`,
  `CLAUDE.md`, `AGENTS.md`, `README.md`, all `docs/`, all
  `.github/workflows/`

- [ ] **Step 1: Rename config file**

```bash
git mv standard-tooling.toml vergil.toml
```

- [ ] **Step 2: Update vergil.toml contents**

Update the `[dependencies]` key and `[publish] consumer-refresh`:

```toml
[dependencies]
vergil = "v2.0"
```

Update the `consumer-refresh` field to reference the new package name,
org, and version:

```
consumer-refresh = """\
uv tool install --python 3.14 'vergil-tooling @ git+https://github.com/vergil-project/vergil-tooling@v<VERSION>'
"""
```

Note: `[project.co-authors]` bot account names (`wphillipmoore-claude`,
`wphillipmoore-codex`) stay as-is — they are GitHub usernames, not
repo/org references. Rename separately if needed.

- [ ] **Step 3: Update pre-commit hook**

In `.githooks/pre-commit`, replace:
- `st-commit` → `vrg-commit` (all occurrences)
- `ST_COMMIT_CONTEXT` → `VRG_COMMIT_CONTEXT` (all occurrences)
- `standard_tooling` → `vergil_tooling` (comment references)
- `standard-tooling-plugin` → `vergil-claude-plugin` (comment
  references)

- [ ] **Step 4: Update workflow files**

In all `.github/workflows/*.yml`:
- `wphillipmoore/standard-actions` → `vergil-project/vergil-actions`

```bash
find .github/workflows -name '*.yml' -exec sed -i '' \
  's|wphillipmoore/standard-actions|vergil-project/vergil-actions|g' \
  {} +
```

Also update version tags if needed (`@v1.5` → `@v2.0` once
vergil-actions v2.0 exists).

- [ ] **Step 5: Update CLAUDE.md**

This is a large file with extensive references. Perform a targeted
replacement:

```bash
sed -i '' \
  -e 's/standard-tooling\.toml/vergil.toml/g' \
  -e 's/standard-tooling-docker/vergil-docker/g' \
  -e 's/standard-tooling-plugin/vergil-claude-plugin/g' \
  -e 's/standard-tooling/vergil-tooling/g' \
  -e 's/standard_tooling/vergil_tooling/g' \
  -e 's/standard-actions/vergil-actions/g' \
  -e 's|wphillipmoore/vergil|vergil-project/vergil|g' \
  -e 's/st-commit/vrg-commit/g' \
  -e 's/st-validate/vrg-validate/g' \
  -e 's/st-docker-run/vrg-docker-run/g' \
  -e 's/st-docker-test/vrg-docker-test/g' \
  -e 's/st-submit-pr/vrg-submit-pr/g' \
  -e 's/st-prepare-release/vrg-prepare-release/g' \
  -e 's/st-merge-when-green/vrg-merge-when-green/g' \
  -e 's/st-finalize-repo/vrg-finalize-repo/g' \
  -e 's/st-ensure-label/vrg-ensure-label/g' \
  -e 's/ST_COMMIT_CONTEXT/VRG_COMMIT_CONTEXT/g' \
  -e 's/`st-\*/`vrg-\*/g' \
  CLAUDE.md
```

Review the result manually — `CLAUDE.md` has nuanced references that
may need manual adjustment.

- [ ] **Step 6: Update AGENTS.md**

Same replacement pattern as CLAUDE.md.

- [ ] **Step 7: Update all documentation**

```bash
find docs -name '*.md' -exec sed -i '' \
  -e 's/standard-tooling\.toml/vergil.toml/g' \
  -e 's/standard-tooling-docker/vergil-docker/g' \
  -e 's/standard-tooling-plugin/vergil-claude-plugin/g' \
  -e 's/standard-tooling/vergil-tooling/g' \
  -e 's/standard_tooling/vergil_tooling/g' \
  -e 's/standard-actions/vergil-actions/g' \
  -e 's|wphillipmoore/vergil|vergil-project/vergil|g' \
  -e 's/st-commit/vrg-commit/g' \
  -e 's/st-validate/vrg-validate/g' \
  -e 's/st-docker-run/vrg-docker-run/g' \
  -e 's/st-docker-test/vrg-docker-test/g' \
  -e 's/st-submit-pr/vrg-submit-pr/g' \
  -e 's/st-prepare-release/vrg-prepare-release/g' \
  -e 's/st-merge-when-green/vrg-merge-when-green/g' \
  -e 's/st-finalize-repo/vrg-finalize-repo/g' \
  -e 's/st-ensure-label/vrg-ensure-label/g' \
  -e 's/ST_COMMIT_CONTEXT/VRG_COMMIT_CONTEXT/g' \
  {} +
```

- [ ] **Step 8: Final grep for any remaining old references**

```bash
grep -rn "standard-tooling\|standard_tooling\|standard-actions\|wphillipmoore\|st-commit\|st-validate\|st-docker\|ST_COMMIT" \
  --include="*.py" --include="*.md" --include="*.toml" \
  --include="*.yml" --include="*.yaml" --include="*.json" \
  --include="*.sh" . \
  | grep -v CHANGELOG.md | grep -v releases/ | grep -v .git/
```

Expected: Zero matches outside of historical files (CHANGELOG, release
notes). Fix any remaining references.

- [ ] **Step 9: Commit**

```bash
git add -A
```

Commit message: `refactor!: update config, hooks, docs, and workflows for vergil rename`

---

## Task 10: vergil-tooling — Validate and Release v2.0.0

- [ ] **Step 1: Install dev dependencies**

```bash
UV_PROJECT_ENVIRONMENT=.venv-host uv sync --group dev
export PATH="$(pwd)/.venv-host/bin:$PATH"
```

- [ ] **Step 2: Run validation**

```bash
vrg-docker-run -- uv run vrg-validate
```

If this fails because the host tools are still named `st-*`, use the
dev-tree override:

```bash
python -m vergil_tooling.bin.vrg_docker_run -- uv run python -m vergil_tooling.bin.vrg_validate
```

Or simply run tests directly:

```bash
uv run pytest tests/ -v
```

- [ ] **Step 3: Fix any test failures**

Tests may fail due to:
- Hardcoded `standard_tooling` or `st-*` references in test fixtures
- Mock paths referencing old module names
- Config file name expectations

Fix each failure, re-run until green.

- [ ] **Step 4: Run linting and type checking**

```bash
uv run ruff check src/ tests/
uv run mypy src/
```

Fix any issues.

- [ ] **Step 5: Commit fixes if any**

```bash
git add -A
```

Commit message: `fix: resolve test and lint failures from vergil rename`

- [ ] **Step 6: Push and open PR**

```bash
git push -u origin release/2.0.0
```

Open PR, wait for CI. Note: CI uses the setup action from
vergil-actions which now reads `vergil.toml` — if the action was
released correctly in Task 3, this should work.

- [ ] **Step 7: Iterate on failures**

If CI fails, fix issues, commit, push. Each fix increments the
potential patch level (v2.0.1, v2.0.2, ...).

- [ ] **Step 8: Merge and release**

After CI passes, merge. Follow the repo's release workflow to create
the `v2.0.0` tag (or `v2.0.x` if patches were needed).

---

## Task 11: vergil-claude-plugin — Transfer, Rename, Update, and Release

**Repo:** `wphillipmoore/standard-tooling-plugin` →
`vergil-project/vergil-claude-plugin`

- [ ] **Step 1: Transfer and rename**

```bash
gh api repos/wphillipmoore/standard-tooling-plugin/transfer \
  -f new_owner=vergil-project --silent
```

Wait, then:

```bash
gh repo rename vergil-claude-plugin --repo vergil-project/standard-tooling-plugin --yes
```

Verify:

```bash
gh repo view vergil-project/vergil-claude-plugin --json nameWithOwner --jq '.nameWithOwner'
```

- [ ] **Step 2: Clone fresh and create release branch**

```bash
cd ~/dev/github
git clone git@github.com:vergil-project/vergil-claude-plugin.git
cd vergil-claude-plugin
git checkout -b release/2.0.0
```

- [ ] **Step 3: Update plugin.json**

In `.claude-plugin/plugin.json`:

```json
{
  "name": "vergil",
  "description": "VERGIL — shared hooks, skills, agents, and commands for all managed repositories.",
  "version": "2.0.0",
  "author": {
    "name": "vergil-project"
  },
  "repository": "https://github.com/vergil-project/vergil-claude-plugin",
  "license": "MIT"
}
```

This changes the skill namespace from `standard-tooling:*` to
`vergil:*` (e.g., `vergil:publish`, `vergil:handoff`).

- [ ] **Step 4: Update marketplace.json**

In `.claude-plugin/marketplace.json`, update the name, description,
and repository URL to match.

- [ ] **Step 5: Update all skill files**

```bash
find skills -name 'SKILL.md' -exec sed -i '' \
  -e 's/standard-tooling\.toml/vergil.toml/g' \
  -e 's/standard-tooling-plugin/vergil-claude-plugin/g' \
  -e 's/standard-tooling/vergil-tooling/g' \
  -e 's/standard_tooling/vergil_tooling/g' \
  -e 's/standard-actions/vergil-actions/g' \
  -e 's|wphillipmoore/vergil|vergil-project/vergil|g' \
  -e 's/st-commit/vrg-commit/g' \
  -e 's/st-validate/vrg-validate/g' \
  -e 's/st-docker-run/vrg-docker-run/g' \
  -e 's/st-submit-pr/vrg-submit-pr/g' \
  -e 's/st-prepare-release/vrg-prepare-release/g' \
  -e 's/st-merge-when-green/vrg-merge-when-green/g' \
  -e 's/st-finalize-repo/vrg-finalize-repo/g' \
  -e 's/ST_COMMIT_CONTEXT/VRG_COMMIT_CONTEXT/g' \
  {} +
```

Review each skill file — skills may reference specific command names
or config paths in their instructions.

- [ ] **Step 6: Update hook scripts**

```bash
find hooks -name '*.sh' -o -name '*.json' | xargs sed -i '' \
  -e 's/standard-tooling\.toml/vergil.toml/g' \
  -e 's/standard-tooling/vergil-tooling/g' \
  -e 's/st-commit/vrg-commit/g' \
  -e 's/ST_COMMIT_CONTEXT/VRG_COMMIT_CONTEXT/g' \
  2>/dev/null
```

Also check `hooks/scripts/lib/managed-repo-check.sh` — this may
detect managed repos by looking for `standard-tooling.toml`.

- [ ] **Step 7: Update pre-commit hook, config, docs, workflows**

- `.githooks/pre-commit`: `st-commit` → `vrg-commit`,
  `ST_COMMIT_CONTEXT` → `VRG_COMMIT_CONTEXT`
- `git mv standard-tooling.toml vergil.toml` and update contents
- Update `CLAUDE.md`, `AGENTS.md`, `README.md`, all `docs/` files
- Update `.github/workflows/*.yml`: `wphillipmoore/standard-actions` →
  `vergil-project/vergil-actions`

- [ ] **Step 8: Verify no old references remain**

```bash
grep -rn "standard-tooling\|standard_tooling\|wphillipmoore\|st-commit\|st-validate\|ST_COMMIT" \
  --include="*.md" --include="*.toml" --include="*.yml" \
  --include="*.json" --include="*.sh" . \
  | grep -v CHANGELOG.md | grep -v releases/
```

Expected: Zero matches.

- [ ] **Step 9: Verify plugin namespace resolution**

Install the plugin from the new repo and confirm skills resolve under
the new `vergil:*` namespace:

```bash
claude mcp add-plugin vergil-project/vergil-claude-plugin
```

Verify at least one skill is callable:

```bash
# In a Claude Code session, confirm /vergil:publish (or similar) loads
```

If the namespace doesn't resolve, check `plugin.json` `"name"` field
and the plugin registration mechanism.

- [ ] **Step 10: Commit, push, PR, merge, release v2.0.0**

Commit message: `feat!: rename to vergil-claude-plugin under vergil-project org`

---

## Task 12: Consumer Sweep

**Phase 2 checkpoint:** All four core repos must be stable and
released at v2.0.x before starting this task.

For each consumer repo in the manifest below, apply the following
template. The repos can be processed in any order.

### Consumer repo manifest

| Repository | Notes |
|---|---|
| `ai-research-methodology` | Future Diogenes rename candidate |
| `career-strategy` | |
| `cognition` | |
| `home-equity-project` | |
| `lunatick-racing` | |
| `mempalace` | |
| `mnemosys-core` | |
| `mnemosys-ios` | |
| `mnemosys-operations` | |
| `mq-rest-admin-common` | |
| `mq-rest-admin-dev-environment` | |
| `mq-rest-admin-go` | |
| `mq-rest-admin-java` | |
| `mq-rest-admin-python` | |
| `mq-rest-admin-ruby` | |
| `mq-rest-admin-rust` | |
| `paad` | Claude Code plugin — check skill namespace refs |
| `the-infrastructure-mindset` | |
| `renegade-dotfiles` | Verify if it consumes vergil |

### Template — per consumer repo

For each repo `$REPO`:

- [ ] **Step A: Clone or update**

```bash
cd ~/dev/github/$REPO
git fetch origin
git checkout develop  # or main, depending on repo
git pull
```

- [ ] **Step B: Create branch**

```bash
git checkout -b chore/vergil-rename
```

- [ ] **Step C: Rename config file**

```bash
git mv standard-tooling.toml vergil.toml
```

- [ ] **Step D: Update vergil.toml**

Update `[dependencies]`:
- `standard-tooling = "v1.x"` → `vergil = "v2.0"`

Update `[publish] consumer-refresh` if present:
- Package name: `standard-tooling` → `vergil-tooling`
- GitHub org: `wphillipmoore` → `vergil-project`
- Repo name: `standard-tooling` → `vergil-tooling`

- [ ] **Step E: Update pre-commit hook**

In `.githooks/pre-commit`:
- `st-commit` → `vrg-commit`
- `ST_COMMIT_CONTEXT` → `VRG_COMMIT_CONTEXT`
- `standard_tooling` → `vergil_tooling` (comment references)

- [ ] **Step F: Update workflow files**

```bash
find .github/workflows -name '*.yml' -exec sed -i '' \
  -e 's|wphillipmoore/standard-actions|vergil-project/vergil-actions|g' \
  -e 's|ghcr.io/wphillipmoore|ghcr.io/vergil-project|g' \
  {} +
```

Also update version tags: `@v1.x` → `@v2.0`.

- [ ] **Step G: Update CLAUDE.md / AGENTS.md**

Replace references to `standard-tooling`, `st-*` commands, and
`wphillipmoore` org with the new names. Each repo's CLAUDE.md is
different — review manually after sed.

Also check for plugin skill namespace references:

```bash
grep -rn "standard-tooling:" --include="*.md" .
```

Replace `standard-tooling:` → `vergil:` (e.g.,
`standard-tooling:publish` → `vergil:publish`).

- [ ] **Step H: Update any other references**

```bash
grep -rn "standard-tooling\|st-commit\|st-validate\|st-docker\|wphillipmoore" \
  --include="*.md" --include="*.toml" --include="*.yml" \
  --include="*.yaml" --include="*.json" --include="*.sh" . \
  | grep -v CHANGELOG | grep -v releases/
```

Fix any remaining references.

- [ ] **Step I: Commit, push, open PR**

Commit message: `chore: update references for vergil rename`

Push, open PR, wait for CI.

- [ ] **Step J: Merge and release**

After CI passes, merge. Create a minor version release (1.x → 1.x+1).

---

## Task 13: Final Verification and Local Cleanup

- [ ] **Step 1: Verify all releases**

For each of the four core repos, confirm the release exists and
artifacts are correct:

```bash
for repo in vergil-docker vergil-actions vergil-tooling vergil-claude-plugin; do
  echo "=== $repo ==="
  gh release list --repo "vergil-project/$repo" --limit 1
done
```

- [ ] **Step 2: Verify consumer releases**

For each consumer repo, confirm the minor version release exists:

```bash
gh repo list wphillipmoore --limit 100 --json name,isArchived \
  --jq '.[] | select(.isArchived == false) | .name' \
  | while read -r repo; do
    echo "=== $repo ==="
    gh release list --repo "wphillipmoore/$repo" --limit 1 2>/dev/null
  done
```

- [ ] **Step 3: Verify host tool installation**

```bash
uv tool install --python 3.14 \
  'vergil-tooling @ git+https://github.com/vergil-project/vergil-tooling@v2.0'
```

Verify the tools are available:

```bash
which vrg-commit vrg-validate vrg-docker-run
```

- [ ] **Step 4: Update local git remotes**

For each local clone that still points at `wphillipmoore`:

```bash
cd ~/dev/github/standard-tooling
git remote set-url origin git@github.com:vergil-project/vergil-tooling.git

# Repeat for each repo:
cd ~/dev/github/standard-actions  # or wherever cloned
git remote set-url origin git@github.com:vergil-project/vergil-actions.git
# etc.
```

- [ ] **Step 5: Update Claude Code plugin installation**

Re-install the plugin from the new location. The exact mechanism
depends on how the plugin is registered in Claude Code — update the
plugin source URL from `wphillipmoore/standard-tooling-plugin` to
`vergil-project/vergil-claude-plugin`.

- [ ] **Step 6: Smoke test end-to-end**

Pick one consumer repo and verify the full workflow:

```bash
cd ~/dev/github/<consumer-repo>
vrg-docker-run -- uv run vrg-validate  # or language-appropriate command
```

This proves: host tools installed correctly, config file parsed,
docker images pulled from new GHCR scope, validation runs.

- [ ] **Step 7: Clean up old GHCR images (optional, deferred)**

Old images at `ghcr.io/wphillipmoore/*` can be removed once all
consumers are verified. This is optional and can be done later.
