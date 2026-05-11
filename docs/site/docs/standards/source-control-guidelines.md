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

Every CI check must be classified as either a hard gate or a soft gate.

- Hard gate: blocking. A failing check prevents PR submission and merge.
- Soft gate: warning-only. A failing check does not block merge, but must be
  surfaced in the PR with rationale and any follow-up tracking.

Each repository must explicitly list its checks and their gate type. If a
check is not classified, treat it as a hard gate until documented.

Hard gates must be enforced as required status checks on the target branches
so failing GitHub Actions block PR merges.

Each repository must also document which hard gates apply per branch. Some
hard gates may be develop-only, while others must run on all eternal branches.

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

**Enforce-admins-ON**: Every ruleset must have an empty `bypass_actors` list.
This means ruleset enforcement applies to all users including repository
administrators. There is no escape hatch for admins.

**Library repositories** require three rulesets:

1. **Branch protection** (targets: `main`, `develop`)
   - Prevent branch deletion
   - Prevent force push
   - Require pull requests (0 approvals, dismiss stale reviews)

2. **CI gates** (targets: `main`, `develop`)
   - Require status checks to pass before merging (`strict` mode)
   - Required checks are repo-specific and must match the CI job names defined
     in the repository's workflow files

3. **Tag protection** (targets: `v*`)
   - Prevent tag deletion
   - Prevent force-updating tags
   - Prevent modifying existing tags
   - No creation restriction (the publish workflow creates tags)

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
