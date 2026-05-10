# Hotfix Policy

## Purpose

Define the hotfix process as a controlled failure mode.

Hotfixes exist to correct production-blocking issues when normal promotion
flow is insufficient. They are expected to be rare.

## Definition

A hotfix is a change that:

- addresses a production outage or critical defect
- cannot wait for standard develop to release to main promotion
- requires immediate correction in production

## Branching rules

Hotfixes use the `hotfix/*` branch prefix with an issue number:

```text
hotfix/42-critical-auth-failure
```

Rules:

- branch from main
- fix only the production issue
- no unrelated refactors or enhancements

## Merge requirements

A hotfix branch must:

1. be merged into main
2. be forward-merged into release
3. be forward-merged into develop
4. produce a new release version
5. be deleted immediately

Skipping any step is forbidden.

## Cultural invariant

Creating a hotfix is an explicit signal of process failure upstream.

The system is intentionally designed to make hotfixes:

- visible
- slightly painful
- operationally expensive

This discourages normalization.

## Postmortem requirement

Every hotfix requires a brief written postmortem addressing:

- root cause
- why the issue escaped test
- what process change prevents recurrence

No blame. Only system correction.

## Forbidden practices

- using hotfixes for convenience
- long-lived hotfix branches
- bypassing release validation post-hotfix
- treating hotfixes as normal workflow

## Guiding principle

Hotfixes are allowed, not accepted.
