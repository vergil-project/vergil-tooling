# Blocked-by storage spike — finding & decision

**Issue:** vergil-project/vergil-tooling#2184 · **Epic:** vergil-project/.github#115 · **Date:** 2026-07-08

## Question

For the validation-task dependency link ("validation task X is blocked-by task
Y"), should we store it as a **native GitHub issue dependency** or as a portable
**`Blocked-by: owner/repo#N` body reflink**? (Epic #115, Task 4 implements the
chosen storage; Task 6's runnable-vs-blocked rollup reads it.)

Decision rule (from the plan): adopt native **only if** write *and* read both
succeed under the App-installation token our tooling uses; otherwise reflink.

## Method

- Introspected the GitHub GraphQL schema for a dependency/block mutation or
  Issue field — via `vrg-gh api graphql` (agent CLI) and via
  `lib.github.graphql` inside `vrg-container-run`.
- Checked the REST surface for dependency endpoints.
- Reviewed authoritative GitHub documentation on issue dependencies.
- Reviewed how the existing codebase models issue relationships.

## Findings

1. **Native issue dependencies exist, but only via REST.** GitHub shipped issue
   dependencies (blocked-by / blocking) in Aug 2025, exposed through REST
   endpoints such as `/repos/{owner}/{repo}/issues/{n}/dependencies/blocked_by`
   (API version `2026-03-10`). No blocked-by/dependency **GraphQL** mutation or
   `Issue` field surfaced in introspection. Sources:
   - <https://docs.github.com/en/rest/issues/issue-dependencies?apiVersion=2026-03-10>
   - <https://github.blog/changelog/2025-08-21-dependencies-on-issues/>

2. **The agent surface cannot reach the raw API.** `vrg-gh api` (REST *and*
   `graphql`) is denied for the `user`/agent identity ("broad write-capable
   escape hatch"). So an agent cannot set or read a native dependency through the
   sanctioned CLI. (Library code can — `lib.github.run` / `.graphql` invoke the
   `gh` binary directly, bypassing the `vrg-gh` wrapper — but that is a new
   REST-based helper, not the path any agent touches.)

3. **The codebase standardizes relationships on GraphQL, with a reflink
   fallback.** `lib/epics.py` implements sub-issues via the `addSubIssue`
   GraphQL mutation and already carries a portable `Parent: owner/repo#N`
   body-reflink fallback for forges without native support. There is precedent
   and machinery for the reflink shape; there is none for REST dependencies.

4. **Native write could not be empirically confirmed under the App token.** The
   agent CLI is denied, and the ad-hoc container probe has no token
   (`gh auth login` required). A real write+read test would need a human to run
   raw `gh api …` calls. Per the decision rule, *unconfirmed* ⇒ not adopted.

## Decision: **reflink**

Store the dependency as a `Blocked-by: owner/repo#N` line in the validation
task's body.

**Rationale.**

- Guaranteed to work with no `api` surface and no identity carve-outs; created
  by `vrg-issue-create` (which we control) and read by parsing the body.
- Consistent with the existing `Parent:` sub-issue reflink fallback — one shape,
  proven machinery, no new REST/version-header surface.
- Satisfies the requirement fully: the runnable-vs-blocked rollup only needs to
  know each blocker's ref and open/closed state, both available from the reflink
  + a normal issue-state read.

**Native is not precluded — it is deferred and additive.** Task 4's `blockers_of`
prefers a native source and falls back to the reflink, so a future
`lib.github`-level REST helper (with the `X-GitHub-Api-Version: 2026-03-10`
header) can be added *without* changing the concept or the rollup. The gate for
that future work is unchanged: a human first confirms the App token can write
**and** read a native dependency, and we decide the GitHub-UI benefit justifies
the REST/identity surface.

## Downstream impact

- **Task 4** implements the reflink baseline (`render_blocked_by`, `blockers_of`,
  `all_blockers_closed`) as planned; the native `_native_blockers` stub stays
  empty (returns `[]`), so the fallback is always taken.
- **Task 5/6** unaffected — they already read whichever source `blockers_of`
  returns.
