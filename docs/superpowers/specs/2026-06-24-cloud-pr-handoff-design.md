# Cloud↔Mac PR Handoff via GitHub Relay Design

**Issues:**

- [vergil-tooling #1858 — Cloud↔Mac PR handoff via GitHub relay
  (GitHubTransport + branch-sync-on-handoff)](https://github.com/vergil-project/vergil-tooling/issues/1858)
- Ref [#1796 — shared-worktree over NFS (parked alternative)](https://github.com/vergil-project/vergil-tooling/issues/1796)
- Ref [#1706 — off-platform VM backend dispatch](https://github.com/vergil-project/vergil-tooling/issues/1706)

**Date:** 2026-06-24

**Status:** Design (from brainstorming, 2026-06-24).

## Problem

The sandboxing / dual-role workflow was built on the Mac, where every VM and the
host share one filesystem. That shared filesystem was quietly carrying **two
payloads** on every PR handoff:

1. the feature **branch / working tree**, and
2. the small **handoff metadata** file (`.vergil/pr-workflow.json`).

The agent prepares a PR (records metadata via the `vrg-pr-workflow` oracle); the
human, seeing the same working tree, runs `vrg-submit-pr`, which reads that local
file and submits. This worked because both parties shared the disk.

The IBM MQ work forces x86: the MQ server has no ARM build, and ARM emulation runs
10–20× too slowly to scale. So work has moved to cloud x86 VMs (also serving the
goal of supporting bare-metal x86 client environments). Those VMs have **no shared
filesystem** with the Mac, so the handoff breaks:

- `vrg-submit-pr` reads `.vergil/pr-workflow.json` straight off the local disk
  (`submission.read_pr_fields`, `submission.py:44`); the file is gitignored
  (`.gitignore:24`) and local-only.
- `vrg-submit-pr` requires a live local worktree and pushes the branch itself
  (`git push --force-with-lease`, `vrg_submit_pr.py:135`).
- If the human logs into the agent's cloud VM to run the handoff, the sandbox
  hands them the **agent** identity, not the human one — defeating the identity
  separation the whole design rests on.

The parked NFS approach ([#1796](https://github.com/vergil-project/vergil-tooling/issues/1796))
would rebuild a shared filesystem in the cloud at ~$115–125/month, permanently —
against a backdrop of ~$1000/month of existing personal cloud + AI spend. It also
targets the *agent↔agent* audit experiment (which is not a live priority), not the
*human↔agent* handoff that is the actual blocker.

## Key insight

The branch already travels over GitHub — the agent pushes it. Only the **metadata**
is trapped on local disk. Move the metadata onto GitHub too, and the filesystem
coupling dissolves entirely, at **zero recurring cost**, while the identity boundary
is preserved **structurally**: the human runs `vrg-submit-pr` on the Mac under their
own credentials and never enters the agent's sandbox.

The codebase already anticipates this: the `pr_workflow` `Transport` ABC
(`transport.py:19`) has a `LocalFileTransport` today and an explicitly-named but
unbuilt **`GitHubTransport`** (`transport.py:7`).

## Goals

- **Unblock the MQ x86 work immediately** with no recurring cost (Deliverable A).
- **Dissolve the shared-FS dependency** for the PR handoff so cloud development can
  later hand off to the human on the Mac without NFS (Deliverable B).
- **Preserve the identity boundary** with no new mechanism — it holds because the
  human stays on the Mac.
- **Leave the Lima (local) path byte-for-byte unchanged.** The relay is additive
  and only activates where there is no shared filesystem.

## Non-goals

- **NFS / #1796.** Superseded for the live need; stays parked.
- **Hard mechanical enforcement** of the near-term boundary — documented + advisory
  only.
- **Cloud-side `vrg-submit-pr`.** Submission, merge, and finalization remain
  human-on-Mac actions; the relay feeds the existing human flow, it does not relocate
  it.
- **Full hardening of the dual-agent audit loop across filesystems.** The
  branch-sync mechanism (below) makes the audit loop's *code-review* half
  filesystem-independent as a consequence, but productionizing the full audit loop
  off-platform is a follow-up.
- **Shared project memory cross-filesystem.** On the Mac, `vergil-user` and
  `vergil-audit` shared `~/.claude/projects/<slug>/memory/` over the FS. That is a
  Mac-local nicety the cloud audit case does not expect; it is out of scope by
  nature, not a deferred gap.

## Deliverable A — Near-term cloud/Mac boundary

A workflow convention plus light wiring, not a new subsystem.

- **Scope.** Cloud x86 VMs are for runtime verification, builds, triage/debugging,
  and issue registration. PR-development does not happen there.
- **Communication.** The cloud triage agent writes structured findings, repro
  steps, and diagnosis as comments on the GitHub issue; the Mac development agent
  picks the issue up through the normal flow. The GitHub issue is the cloud→Mac
  message bus.
- **Where development happens.** All PR-development continues on the Mac via Lima,
  where the shared filesystem works and `vrg-submit-pr` is unchanged. The Ubuntu lab
  stack (only the x86 MQ binaries emulated, everything else ARM-native) runs locally;
  pure-x86 / Red Hat stacks that must run natively are exercised on the cloud VM for
  triage only.
- **Enforcement: documented + advisory.** A cloud session is identifiable (the
  off-platform backend). `report-ready` / `vrg-submit-pr` invoked in that context
  emit a clear advisory ("PR-development isn't supported on cloud VMs yet — develop
  on the Mac; see <doc>") rather than a hard block. The existing policy ("agents must
  not run `vrg-submit-pr`") already covers the submission half; this adds the upstream
  "cloud agents don't do PR-development" convention to `CLAUDE.md` (cloud-session
  prompt contract) and this spec.

This ships with essentially no code beyond the advisory and documentation, and
removes the cloud blocker for the MQ work today.

## Deliverable B — GitHubTransport metadata relay

### Relay channel: a reserved git ref

The agent writes the handoff payload to a reserved, namespaced ref:

```
refs/vergil/pr-workflow/<branch>
```

Nested slashes are valid, so `feature/123-x` →
`refs/vergil/pr-workflow/feature/123-x`. The namespace is invisible to the branch
and PR UI and to default `git fetch` refspecs, so it never pollutes history or
clutters the PR list, and the agent does not create a PR (submission stays human).

### What the ref points at

Not a raw blob — GitHub rejects refs that do not resolve to a commit. The ref points
at a **single-file commit**: a tree containing `pr-workflow.json`, committed with a
fixed message. The human reads it with `git show <ref>:pr-workflow.json`, a
plumbing-level read that mirrors today's local-file read. The agent builds it via
git plumbing (`hash-object` → `mktree` → `commit-tree` → `update-ref` → push) or the
equivalent GitHub API blob→tree→commit→ref calls, all behind the transport.

### Transport selection (additive, gracefully degrading)

- `report-ready` **always** writes the local `.vergil/pr-workflow.json` (unchanged).
  **Additionally**, when the session is off-platform *or* a `[pr-workflow] relay =
  "github-ref"` config key is set, it pushes the ref.
- `vrg-submit-pr` reads the local file if present (the Lima co-located path — zero
  change). If absent, or when invoked with `--from-ref <branch>`, it fetches
  `refs/vergil/pr-workflow/<branch>` and reads the payload from there.

This keeps the Lima path byte-for-byte identical and activates the GitHub path only
where there is no shared filesystem. It slots directly into the existing `Transport`
ABC: `GitHubTransport` implements `read` / `write` / `head_sha` against the ref
instead of the local file.

### The human submit flow

On the Mac, against a branch a cloud agent prepared:

```
vrg-git fetch origin 'refs/vergil/pr-workflow/*' <branch>
vrg-git checkout <branch>
vrg-submit-pr            # reads metadata from the fetched ref, submits as HUMAN
```

Safety properties, all cheap to enforce:

1. **Drift guard.** The payload records `head_sha`. `vrg-submit-pr` verifies the
   checked-out branch HEAD matches it; a mismatch fails loudly ("the relayed
   metadata is for SHA abc123 but your checkout is at def456 — re-fetch"). This
   catches the agent pushing more work after the human fetched.
2. **`--force-with-lease` stays honest.** The human fetched the agent's exact
   branch, so the lease matches and the push is a no-op / fast-forward. If the agent
   re-pushed since the fetch, the existing lease-rejection error fires — no silent
   clobber.
3. **Identity preserved structurally.** The human never logs into the agent's VM.
   Branch and metadata both arrive over GitHub; `vrg-submit-pr` runs on the Mac under
   the human's credentials. The identity dilemma cannot arise because the human and
   the agent never share a process or sandbox.

## Branch sync on handoff

Relaying the metadata is necessary but not sufficient: once the parties are on
different filesystems, the **code itself** must be synced on every handoff. The
shared filesystem was silently keeping both working trees identical; cross-filesystem,
the receiving party sees stale code until it pulls.

The `vrg-pr-workflow` oracle gains two responsibilities at the ownership boundary,
reusing the `head_sha` the state already records:

- **On *release* (handing control off):** ensure the working branch is pushed to the
  remote before the state transfer completes, and record `head_sha` = the pushed
  HEAD. A handoff that left local commits unpushed would hand the other side a
  phantom, so the push is part of the release and fails loudly if it cannot push.
- **On *acquire* (control returns):** before the receiving party reads or reviews
  anything, `git fetch` and sync the local working tree to the relayed `head_sha`.
  The receiver never acts on stale code.

This **unifies with the drift guard**: the human verifying HEAD against `head_sha`
is just the acquire-side sync seen from the human's end. One rule, three parties
(user agent, audit agent, human).

**Consequence — the audit loop's code-review half becomes filesystem-independent.**
The audit agent is read-only by contract, so a hard sync to `head_sha` on acquire is
safe (no local edits to clobber). The user agent re-acquiring after feedback syncs to
the same `head_sha` the audit agent did not move — typically a no-op. So the
mechanism built for the human submit case also lets the dual-agent audit loop review
code across filesystems **without NFS**. (Shared project memory remains out of scope,
per Non-goals; the cloud audit case does not expect it.)

## Decision summary

| Decision | Choice | Rationale |
|---|---|---|
| Near-term posture | Cloud = runtime/triage/issue-registration; dev on Mac via Lima | Unblocks MQ x86 work now, zero recurring cost |
| Boundary enforcement | Documented + advisory | Stopgap; Deliverable B dissolves the boundary anyway |
| Relay channel | Reserved git ref `refs/vergil/pr-workflow/<branch>` | Faithful swap for the shared-disk file; no PR created, no history pollution |
| Ref payload | Single-file commit holding `pr-workflow.json` | GitHub requires refs to resolve to commits; clean `git show` read |
| Transport selection | Local always; ref additionally when off-platform/flagged | Lima unchanged; relay additive and gracefully degrading |
| Code sync | Push-on-release / sync-to-`head_sha`-on-acquire in the oracle | Cross-FS handoff needs the code, not just the metadata |
| NFS (#1796) | Superseded for the live need; parked | Targets agent↔agent audit (not a priority) at ~$115–125/mo |

## Code shape

- **`lib/pr_workflow/github_transport.py`** — new `GitHubTransport(Transport)`:
  `read` / `write` / `head_sha` against `refs/vergil/pr-workflow/<branch>` via git
  plumbing (or GitHub API). Ref-name derivation from the branch.
- **`lib/pr_workflow/transport.py`** — transport selection helper: choose
  `LocalFileTransport` vs `GitHubTransport` from backend / `[pr-workflow] relay`
  config.
- **`bin/vrg_pr_workflow.py`** — `report-ready` writes local always, pushes the ref
  when off-platform/flagged; oracle ownership transitions gain push-on-release and
  sync-on-acquire (branch fetch + working-tree sync to `head_sha`).
- **`bin/vrg_submit_pr.py`** — read local metadata if present, else fetch the ref
  (`--from-ref <branch>` / auto when absent); add the `head_sha` drift guard before
  submit.
- **`lib/config.py`** — parse the optional `[pr-workflow] relay` key.
- **`CLAUDE.md`** — cloud-session prompt contract: cloud = triage/runtime only; the
  advisory text.

## Testing

`git` / GitHub mocked at the subprocess boundary; no real cloud in CI.

- `GitHubTransport.write` builds the single-file commit and updates the ref;
  `read` resolves `<ref>:pr-workflow.json`; ref-name derivation from branch.
- `report-ready` writes local always; pushes the ref only when off-platform / flagged.
- `vrg-submit-pr` reads local when present (Lima path unchanged — regression-guarded);
  falls back to the ref when absent / `--from-ref`.
- Drift guard: HEAD ≠ recorded `head_sha` → clear error.
- Oracle handoff: push-on-release records the pushed `head_sha`; acquire-side syncs
  the working tree to `head_sha`.
- Boundary advisory fires in an off-platform context for `report-ready` /
  `vrg-submit-pr`.
- Lima regression: the existing submit / oracle suite stays green.

## Acceptance criteria

1. A repo with no off-platform backend behaves identically (Lima path unchanged):
   `report-ready` and `vrg-submit-pr` use the local file exactly as today.
2. In an off-platform context, `report-ready` pushes the handoff metadata to
   `refs/vergil/pr-workflow/<branch>` as a single-file commit.
3. On the Mac, after fetching the branch and the ref, `vrg-submit-pr` reads the
   relayed metadata and submits the PR under the human identity, with no shared
   filesystem and without logging into the agent's VM.
4. The `head_sha` drift guard fails loudly when the checkout does not match the
   relayed metadata.
5. The oracle pushes on release (recording `head_sha`) and syncs the working tree to
   `head_sha` on acquire, so a receiving party on a different filesystem reviews
   current code.
6. The cloud/Mac boundary is documented in `CLAUDE.md`, and an advisory fires when
   PR-development is attempted in a cloud context.
7. No recurring cloud infrastructure (no NFS / Filestore) is introduced.

## Related

- **Parked alternative:** [#1796 — shared-worktree over NFS](https://github.com/vergil-project/vergil-tooling/issues/1796)
  and `docs/specs/2026-06-23-off-platform-shared-workspace-nfs-design.md`.
- **Off-platform dispatch:** [#1706](https://github.com/vergil-project/vergil-tooling/issues/1706)
  and `docs/specs/2026-06-22-off-platform-vm-dispatch-design.md`.
- **PR workflow oracle:** `docs/specs/2026-06-08-pr-workflow-oracle-design.md`.
- **Security register:** [#1369](https://github.com/vergil-project/vergil-tooling/issues/1369).
