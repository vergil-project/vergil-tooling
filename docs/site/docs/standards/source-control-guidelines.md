# Source Control Guidelines

## Purpose

Define overarching source control policies that complement the operational
git workflow. For the day-to-day branching, commit, and PR workflow, see
[Git Workflow](../guides/git-workflow.md).

## AI assistance constraints

- AI-generated code must be reviewed with the same rigor as external
  contributions.
- AI assistance must never become a silent dependency.
- The codebase must remain understandable, auditable, and maintainable without
  AI.

Code correctness, determinism, and maintainability override speed or
convenience.

## Version control platform

Git is the source control system.

GitHub is the initial central repository host, including GitHub Actions for
CI/CD.

This decision is explicitly provisional, not foundational.

### Lock-in awareness

GitHub-specific dependencies must be:

- understood
- tracked
- periodically reassessed

The project must retain enough knowledge to evaluate the cost and feasibility
of migration to an alternative host if required.

## Repository strategy

### Core philosophy

Repositories should be:

- small
- modular
- scoped to a single semantic responsibility

Independent components should live in separate repositories by default.

### Boundary rule

Repository boundaries follow semantic ownership and lifecycle, not convenience.

This improves:

- independent evolution
- reduced cognitive load
- long-term maintainability
- survivability without original authorship

## CI/CD constraints

CI/CD pipelines must be:

- deterministic
- stateless
- reproducible locally

Automation should avoid reliance on opaque or irreducibly platform-specific
behavior.

CI configuration must remain:

- readable
- minimal
- replaceable

Prefer a shared actions library for reusable workflow logic and pin action
references by tag or commit SHA.

### CI gates

Every CI gate is a hard gate. There are no soft gates. A check is either
enforced as a required status check that blocks PR merge, or it is not a
gate at all — there is no middle tier that runs but does not block. A check
worth running is worth failing on; a non-blocking gate silently rots and
gives false confidence.

- Hard gate: blocking. A failing check prevents PR submission and merge.
  This is the only kind of gate.

Every gate that can block a PR must be enforced as a required status check
on the target branches, so a failing GitHub Actions check blocks the merge.
If a check is not worth enforcing as a required status check, it must not
gate the PR — remove it rather than run it as a non-blocking warning. A
report-only or "advisory-permanent" check is not an allowed category.

Each repository must explicitly list its required checks, and the
required-check set must be pinned (for example, by a test or a config audit)
so it cannot silently drift. This mirrors the CI implementation, where the
same configuration that computes a repo's required status checks drives
branch protection. See the guide
[CI Architecture](../guides/ci-architecture.md), section "Every gate is
required — there are no optional PR gates".

Each repository must also document which gates apply per branch. Some gates
may be develop-only, while others must run on all eternal branches.

## GitHub repository settings

The following settings are required defaults for all repositories hosted on
GitHub.

### Automatically delete head branches

**Setting**: Enabled

Under **Settings > General > Pull Requests**, enable **Automatically delete
head branches**.

This ensures merged branches are removed immediately after merge across all
merge paths. PR finalization steps and CLI commands should still request branch
deletion explicitly. The repository-level setting acts as defense in depth.

### GitHub repository rulesets

All repositories must use GitHub rulesets for branch and tag protection.
Rulesets replace legacy branch protection rules. Legacy branch protection must
not be used on any repository.

**Enforce-admins-ON**: Every ruleset has an empty `bypass_actors` list except
Tag protection, which only the repository Admin role (the human release
identity) may bypass, to move or delete a tag. Every other ruleset applies to
all users, including repository administrators.

The rulesets are declared by `compute_desired_state()` in
`lib/github_config.py` and checked by the config audit; the
[rulesets table](../reference/config-audit.md#github-checks-rulesets) lists
each one's includes, rules and bypass.

**Library repositories** require four rulesets:

1. **Branch protection** (targets: `main`, `develop`)
   - Prevent branch deletion
   - Prevent force push
   - Require pull requests (0 approvals, dismiss stale reviews)

2. **CI gates** (targets: `main`, `develop`)
   - Require status checks to pass before merging (`strict` mode)
   - Required checks are repo-specific and must match the CI job names defined
     in the repository's workflow files

3. **Tag protection** (targets: `v*.*.*` release tags and `v*.*` rolling tags)
   - Prevent tag deletion
   - Prevent force-updating tags
   - Prevent modifying existing tags
   - No creation restriction (the publish workflow creates tags)
   - Bypass: the repository Admin role only

4. **Rolling tag branch names** (targets: `v[0-9]*`)
   - Prevent creating a branch named like a rolling tag, since some consumers,
     Claude Code among them, resolve a ref such as `v2.1` as a branch before a
     tag
   - No bypass

**Documentation repositories** require two rulesets:

1. **Branch protection** (targets: eternal branches only)
   - Same rules as library repositories

2. **CI gates** (targets: eternal branches only)
   - Required checks are repo-specific

Rulesets are managed via the GitHub API or the repository settings UI. When
creating or updating rulesets, verify the configuration by checking an open PR
on the target repository to confirm the expected required checks appear.

## Guiding principle

All source code management decisions are evaluated against a single overriding
criterion:

The system must survive without its original author.

Tooling, structure, and process choices are judged by their contribution to
long-term clarity, auditability, and evolutionary capacity.
