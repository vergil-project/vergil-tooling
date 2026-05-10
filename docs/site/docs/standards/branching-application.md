# Application Branching and Deployment Model

## Purpose

Define the branching model and deployment semantics for application
repositories that use environment-based deployments and linear promotion.

For the day-to-day git workflow, see
[Git Workflow](../guides/git-workflow.md). This document defines the
structural rules specific to the `application-promotion` branching model.

## Core invariants

1. Each long-lived branch maps to exactly one deployment environment.
2. Promotion is monotonic: development to test to production.
3. Humans decide merges; automation performs deployments.
4. Branch names encode intent, not activity.
5. The system must survive without its original author.

## Deployment environments

There are exactly four environments:

- sandbox
- development
- test
- production

Each environment consists of:

- one database
- one API
- one running application version

Sandbox is a pre-PR environment for feature, bugfix, and hotfix branch work.
It is not tied to an eternal branch and is outside the promotion flow.

## Eternal branches

The following branches always exist and are protected:

| Branch  | Purpose                         | Deployment target |
|---------|---------------------------------|-------------------|
| develop | Integration and rapid iteration | development       |
| release | Qualification and validation    | test              |
| main    | Production truth                | production        |

All eternal branches require pull requests for changes. Direct pushes are
forbidden.

## Short-lived branches

Only the following branch prefixes are allowed for application repositories:

- `feature/*`
- `bugfix/*`
- `hotfix/*`
- `promotion/*`

All feature, bugfix, and hotfix branches must include the repository issue
number in the branch name. See
[Git Workflow](../guides/git-workflow.md#branching-model) for naming rules.

### promotion/*

Use for controlled promotion between eternal branches.

Rules:

- branched from the source eternal branch
- merged only into the target eternal branch
- deleted immediately after merge

Naming:

- `promotion/release-<version>-<yyyymmddhhmmss>` for develop to release
- `promotion/main-<version>-<yyyymmddhhmmss>` for release to main

## Promotion flow

Normal flow:

```text
feature/* or bugfix/* → develop → release → main
```

Promotion semantics:

- develop to release: candidate release, aggressive testing
- release to main: production-ready, ship

## Forbidden operations

- merging feature or bugfix branches directly into release or main
- direct commits to eternal branches
- cherry-picking between eternal branches
- deploying to production without passing through release
- long-lived non-eternal branches
- using branch prefixes not listed above
