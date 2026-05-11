# Documentation Branching Model

## Purpose

Define the branching model for documentation repositories that supports a
staging preview on `develop` and a live published site on `main`.

For the day-to-day git workflow, see
[Git Workflow](../guides/git-workflow.md). This document defines the
structural rules specific to documentation repositories.

## Core invariants

- `develop` and `main` are the two eternal branches.
- `develop` is the default branch and staging target.
- `main` is the promotion target for live/published documentation.
- All changes arrive via short-lived branches merged to `develop`.
- Changes reach `main` only via promotion PR from `develop`.
- Versioning is optional and not required for publication.

## Branch roles

### develop

- default branch for documentation
- integration branch for all changes
- source of the staging/preview documentation site (`dev` version)

### main

- promotion target for reviewed, publish-ready content
- source of the live documentation site (`latest` version)
- receives changes only from `develop` via PR

## Promotion flow

To publish documentation changes to the live site:

1. Merge feature/bugfix branches to `develop` via PR.
2. Verify the staging site (`dev` version) renders correctly.
3. Open a PR from `develop` to `main`.
4. Merge the promotion PR to update the live site.

## Short-lived branches

Use short-lived branches for all changes. Only `feature/*`, `bugfix/*`, and
`chore/*` prefixes are allowed. All branches must include the repository issue
number in the branch name.

## Validation expectations

Documentation repositories must run markdownlint for documentation validation.
Automated test or release validation is not required. Additional validation is
optional unless a specific repository documents a requirement.

## Forbidden operations

- direct commits to `develop` or `main`
- long-lived branches other than `develop` and `main`
- merging to `main` from any branch other than `develop`
- force-pushing to `develop` or `main`
