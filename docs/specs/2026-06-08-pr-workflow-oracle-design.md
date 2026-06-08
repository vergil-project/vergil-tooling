# PR Workflow Oracle — design

- **Date:** 2026-06-08
- **Issue:** [#1534](https://github.com/vergil-project/vergil-tooling/issues/1534)
- **Status:** Draft (design approved in brainstorming; refined under pushback review;
  implementation plan to follow)
- **Supersedes:** the ad-hoc `.vergil/pr-template.yml` + `.vergil/audit-feedback.yml`
  two-file handshake between the USER and AUDIT agents.

## 1. Context and motivation

The local USER ↔ AUDIT interaction is currently a crude file-based handshake. The
USER agent writes `.vergil/pr-template.yml` as a "done" signal; the AUDIT agent
polls for it, reviews, and writes `.vergil/audit-feedback.yml` back. The workflow
itself — what to do, in what order, who acts next — lives smeared across the prose
of two skills. That is brittle: the two skills must agree, by hand, on a protocol
that nothing enforces, and the audit step itself is an unfilled placeholder.

This design replaces that handshake with a single state file driven by a Python
**oracle** (`vrg-pr-workflow`), and gives the audit step real, useful work to do.

### 1.1 Why an oracle (the Diogenes lesson)

The Diogenes research tool established a pattern worth reusing: a multi-step
workflow is far more reliable and far cheaper in tokens when the agent does **not**
hold the workflow in its head. Instead, the agent repeatedly asks a state machine
"what do I do next?", executes the single instruction it gets back, reports the
result, and asks again. The workflow — the branching, the ordering, the
termination — lives entirely in Python plus a state file. The agent stays dumb.

In Diogenes that state machine is exposed over MCP. Here it is **not**, for a
simple reason: an MCP server is per-session, so two agent sessions would spin up
two server instances anyway. The thing that actually rendezvouses the two agents
is the file on disk, not a server. For this use case the Diogenes "server" reduces
to *a Python function that reads a state file and returns the next action* — which
is exactly a `vrg-*` CLI, matching vergil-tooling's all-CLI architecture and adding
no new runtime. The token-cost win transfers intact: the agent parks inside one
blocking call instead of laying out and tracking a multi-step plan.

### 1.2 Why the audit checks are judgment-only

Vergil's strategy is to mechanize everything that can be mechanized. Anything
deterministic — format linting, suppression accounting, coverage thresholds —
belongs in a `vrg-validate` CI gate, which is where the bulk of the effort has
gone and should continue to go. If a proposed check turns out to be mechanizable,
that is a signal it belongs in `vrg-validate`, not here.

This engine is exclusively for the things that **cannot** be mechanized: semantic
judgments about whether the development record is honest and adequate. The code,
the PR body and its comments, and the commit messages together form the forensic
audit trail of how a change came to be. A linter can check the *format* of that
trail; only judgment can check its *truthfulness*. That is the engine's purpose.

## 2. Goals and non-goals

**Goals**

- Replace the two-YAML handshake with one oracle-driven state file.
- Make the audit step real: a pluggable, extensible registry of non-mechanizable
  judgment checks, seeded with six concrete checks.
- Keep both agents dumb: workflow logic lives entirely in Python.
- Build the engine so the identical loop and identical checks can later run against
  a live PR, not just a local file — without rework.
- Support a `--no-audit` solo mode for small, high-confidence changes, with the
  bypass recorded (not silent) and the PR-phase audit still mandatory.

**Non-goals (deferred)**

- The post-PR GitHub transport implementation (the interface is locked now; the
  implementation is later work).
- Re-homing the suppression/exception check as a deterministic `vrg-validate` gate
  (its own work; it leaves this engine entirely — see §5.1).
- Resumability beyond idempotent takeover of an existing state file.
- Evaluating the *quality* of the judgment checks (eval harness) — later.

## 3. Architecture overview

The system splits along one line: a transport-agnostic **engine** and a pluggable
**transport**.

1. **State file** (`.vergil/pr-workflow.json`) — the single source of truth and the
   rendezvous point. Pure data. The oracle is its only writer; nobody hand-edits
   it, including the human.

2. **The oracle** (`vrg-pr-workflow`, Python in vergil-tooling) — the entire brain.
   It owns every write, enforces turn-taking, snapshots git facts itself, rolls
   per-check results up into transitions, and blocks server-side until it is the
   caller's turn. Reuses the existing `atomic_write` + SHA-256 poll primitives from
   `lib/await_file.py`.

3. **Two identical, dumb skills** — `vergil:implement` (USER) and `vergil:audit`
   (AUDIT) both collapse to: *call `vrg-pr-workflow next --as <role>`, do what it
   says, report back through the verb it names, repeat until done.* No workflow
   branching in prose. Role comes from `vrg-whoami`, never a guessed flag.

4. **The human** — a first-class participant who acts only through `vrg-pr-workflow`
   verbs, and only from a human-identity context (§3.1, §8.3). Both agents block
   while the human holds the turn.

### 3.1 Topology — the two agents share one worktree

This is load-bearing and easy to get wrong, so it is stated explicitly.

- **Sessions start at the repo root, not in a worktree.** The human runs
  `/vergil:implement <issue>` in one session and `/vergil:audit <issue>` in another;
  the issue determines the worktree both attend to. Both sessions operate on the
  **same** worktree on the host mount.
- **AUDIT is a read-only co-tenant of USER's worktree.** It has filesystem write
  access but, by discipline, touches nothing except via the oracle. It must see the
  same git checkout (to review `origin/develop..HEAD`) and the same `.vergil/`
  directory (to rendezvous on the state file). This is the existing reality — the
  audit skill already states *"You share the USER agent's worktree on the host
  mount."*
- **This is a sanctioned exception** to the CLAUDE.md rule "each parallel agent is
  assigned exactly one worktree." Naming it here prevents a future reader from
  "correcting" it.
- **One active issue per worktree.** A worktree holds one branch, so only one
  workflow can be in flight in it. That is why the state file is a **single, generic
  `pr-workflow.json`**, not keyed by issue in its filename. The issue number is
  recorded *inside* the file (§4) for three concrete reasons: `vrg-submit-pr` needs
  to know which issue to attach the history to; it enables a misconfiguration catch
  (AUDIT told to audit a different issue than the file records — §8.1); and the init
  staleness check needs it.

### 3.2 The transport seam (two phases, one engine)

The engine is written once and reused across both the local (pre-PR) and the future
PR (post-PR) phases. Only the *transport* — how the two parties communicate, wait
for work, and decide they are done — differs.

**Engine (transport-agnostic):**

- The check registry and check evaluation. A check takes a **git range/diff** and
  produces a per-check verdict plus findings. It never touches `.vergil/` or GitHub.
- The rollup logic (§8).
- The round concept and the dumb-agent `next → do → report → repeat` loop.

**Transport interface (two implementations):**

```
next_work(role, since)  -> Directive | DONE   # blocks cheaply; see below
record(role, result)                          # commit results + advance
current_range()         -> "base..HEAD"
escalate(role, reason)
resolve(to_role, note)                        # human only
```

The crucial design choice that keeps the PR extension possible: **turn detection
and termination detection are owned by the transport, not the engine.** The engine
loop imposes nothing:

```
since = null
loop:
    work = transport.next_work(role, since)   # blocks; may return DONE
    if work.is_done: break
    result = <run judgment checks on work.range>   # AUDIT
             <or apply the directive>               # USER
    transport.record(role, result)
    since = work.cursor
```

| Concern | LocalFileTransport (build now) | GitHubTransport (defer; interface fits it) |
|---|---|---|
| State store | `.vergil/pr-workflow.json` | Distributed across the PR: check-runs, review comments, head SHA |
| Change detection | `wait_for_file` — SHA-256 poll every 1s (see §9 / `await_file.py`). | Record head SHA at PR creation, then block until head SHA changes — the existing `vrg-pr-await` logic. |
| Whose work | The `owner` token (`user\|audit\|human`) — an **internal detail of this transport**, present only because one writable file needs a mutex. | **No token.** Purely reactive: AUDIT has work iff head SHA moved; USER has work iff a required check failed or a review/human comment appeared. GitHub serializes the writes. |
| Record results | Write findings + flip `owner`. | Post review comments + set `vergil-audit/approved` — the existing `vrg-audit-approve`. |
| Done | Terminal local state reached: **approved / ready-to-submit**. | **PR merged.** |

The asymmetry is explicit and contained: **local is a strict alternating token**
(one file must be serialized); **the PR phase is independent reactive polling**
(GitHub already serializes, and "has work?" reduces to "did the SHA / checks /
comments change?"). The engine never knows the difference — it only calls
`next_work` and checks `is_done`. The audits run on identical input in both phases:
the full `base..HEAD` delta.

**Three constraints honored now so the extension stays cheap (none add work today):**

1. The engine depends on the transport *interface*, never on file paths or `.vergil`.
2. Turn and termination are computed inside the transport per iteration, never as a
   lock the engine holds and releases.
3. Checks operate on a git *range*, never on `.vergil` — so the same check runs
   against a worktree diff or a PR's commits unchanged.

**The seam itself:** `vrg-submit-pr` attaches the engine's final state (the check
ledger plus history) to the issue. That artifact is both the forensic audit trail
*and* the seed for the PR phase — `GitHubTransport` reads it to learn which checks
were already green locally.

## 4. State file schema

`.vergil/pr-workflow.json` — what `LocalFileTransport` serializes. A single, generic
file (§3.1): one branch per worktree means one workflow. Gitignored scratch.

```jsonc
{
  "schema_version": 1,
  "issue": 1534,                    // recorded, not in the filename (§3.1)
  "branch": "feature/1534-pr-workflow-oracle",
  "base": "origin/develop",
  "phase": "local",                 // "local" now; "pr" is the deferred second phase
  "mode": "paired",                 // paired | solo (--no-audit, §6.2)
  "owner": "audit",                 // user | audit | human — LocalFileTransport's mutex only
  "status": "reviewing",            // implementing | reviewing | changes-requested | approved | escalated | error
  "round": 2,
  "created_at": "2026-06-08T15:00:00Z",
  "updated_at": "2026-06-08T15:42:00Z",

  "participants": {                 // presence handshake (§8.1); tokens minted by the oracle
    "user":  { "token": "u-7f3a91", "identity_mode": "user",  "host": "vm-user",  "present_at": "…" },
    "audit": { "token": "a-91c2de", "identity_mode": "audit", "host": "vm-audit", "present_at": "…" }
    // audit is null until it acks; absent entirely in solo mode
  },

  "pr_metadata": {                  // subsumes pr-template.yml; read by vrg-submit-pr
    "title": "feat(pr-workflow): ...",
    "summary": "…",
    "notes": "…",
    "linkage": "Ref"               // only allowed value, as today
  },

  "git": {
    "base_sha": "<merge-base>",
    "head_sha": "a1b2c3",
    "last_reviewed_sha": "9f8e7d"  // audit's cursor; null before first review
  },

  "checks": [                       // the ledger — subsumes audit-feedback.yml
    { "id": "commit-message-fidelity", "status": "fail", "round": 2,
      "findings": [ { "file": "src/x.py", "line": 42, "severity": "warning",
                      "note": "Commit says 'refactor' but the diff changes behavior." } ] },
    { "id": "pr-description-fidelity", "status": "escalate", "round": 2,
      "reason": "Cannot reconcile the summary with the delta; needs a human call." },
    { "id": "scope-coherence", "status": "pass", "round": 2 }
  ],

  "escalation": null,               // when owner=human: { by, check, reason, raised_at }
  "error": null,                    // terminal: { by: user|audit, at, reason } — counterpart stops on seeing this (§9)

  "history": [                      // append-only; the audit trail attached to the issue
    { "round": 0, "at": "…", "actor": "user",  "action": "init", "mode": "paired" },
    { "round": 1, "at": "…", "actor": "user",  "action": "report-ready", "head_sha": "…" },
    { "round": 1, "at": "…", "actor": "audit", "action": "submit-review", "rollup": "changes" },
    { "round": 2, "at": "…", "actor": "user",  "action": "report-fixes", "head_sha": "a1b2c3" }
  ]
}
```

Notes:

- `pr_metadata` subsumes `pr-template.yml`. `vrg-submit-pr` reads PR fields from
  here. `linkage` keeps today's single allowed value (`Ref`).
- The `checks` ledger subsumes `audit-feedback.yml`. There is **no** `kind` field:
  everything here is a judgment check (§5).
- The oracle snapshots all `git` SHAs itself by shelling out (via `lib/git.py`),
  rather than trusting the agent to report them — this keeps "agent emits facts,
  Python derives state" honest.
- `participants` tokens are oracle-minted random session ids, **not** PIDs — a PID
  is meaningless across the two agents' separate VMs. `identity_mode`/`host` are for
  debugging.
- In `solo` mode (`--no-audit`) the init history entry records `mode: "solo"`, so
  the skipped local audit is visible in the forensic trail, not silent.
- `history` is append-only and is the artifact attached to the issue at submission.

## 5. The check registry

The audit step is a registry of **judgment-only** checks. Each entry is a prompt,
not code — the audit's work is to *run a prompt*, not execute a function.

```
check_id  ->  { prompt, output_instructions }
```

- The registry is a plain dictionary. Adding a check is one entry plus one authored
  prompt; the engine iterates the registry and needs no change.
- The oracle **never evaluates a check itself.** There is no Python-side check path.
  The oracle only routes the judgments the agent reports.
- Each prompt is authored so its specified output maps **1:1** into the `review.v1`
  per-check entry (§7), so landing the result in the state file costs minimal token
  processing. This mirrors Diogenes' "read the compiled prompt, produce JSON that
  matches the schema" contract.

### 5.1 Division of labor with `vrg-validate`

| | Mechanizable | Non-mechanizable judgment |
|---|---|---|
| Where | `vrg-validate` CI gate (deterministic) | This engine |
| Examples | format lint, suppression accounting, coverage % | the six below |
| Volume | the bulk; the priority | small, curated |

The suppression/exception check (net-new `# type: ignore`, `# noqa`, `# nosec`
without an approval record) is fully deterministic and therefore **does not belong
here** — it becomes a `vrg-validate` gate as separate work.

### 5.2 Seed checks

All six are genuinely non-mechanizable: a linter can check format; only judgment can
check truthfulness or adequacy. They sanity-check the forensic audit trail (code, PR
body/comments, commit messages) for honesty and completeness.

1. **`site-docs-reflection`** — if site docs exist, are the PR's user-facing changes
   reflected there? (Expensive, high value.)
2. **`docstring-accuracy`** — changed docstrings actually describe what the code now
   does.
3. **`pr-description-fidelity`** — the USER-authored `summary`/`notes` honestly match
   the cumulative delta: no overclaiming, no silent omission of significant changes.
4. **`commit-message-fidelity`** — each commit message truthfully describes what that
   commit changed (not vague, not mislabeled). Conventional-commit *format* stays in
   `vrg-validate`; truthfulness is judgment.
5. **`scope-coherence`** — all changes in the delta relate to the stated issue; no
   unrelated edits snuck in.
6. **`test-adequacy`** — new/changed behavior is meaningfully exercised by tests that
   assert its intent. Coverage % stays in `vrg-validate`; whether an assertion tests
   the right thing is judgment.

The six are a seed. The mechanism — registry plus prompts plus the transport seam —
is the deliverable.

## 6. Command surface and the directive

Both skills only ever *start* by calling `next`. Every report verb is **named back
to them by the directive**, so the agent never chooses a path — it follows the
instruction.

| Verb | Who | What the oracle does |
|---|---|---|
| `next --as <role> [--no-audit]` | USER, AUDIT | Block (SHA poll). First-ever call as USER: **init/takeover** the file and run the startup handshake (§8.1). Return a Directive or DONE. |
| `report-ready --title --summary --notes [--linkage]` | USER | Snapshot HEAD, write `pr_metadata`, `owner→audit`. (Initial done signal.) |
| `report-fixes [--note]` | USER | Verify HEAD moved, snapshot it, `owner→audit`, bump round. |
| `submit-review --payload review.json` | AUDIT | Validate payload vs `review.v1`, update ledger, **roll up** → set owner/status, record `last_reviewed_sha`. |
| `escalate --reason …` | USER, AUDIT | `owner→human`, set `escalation`, status=escalated. |
| `resolve --to user\|audit [--note]` | HUMAN | Hand control back to an agent. **Rejected unless `vrg-whoami --mode == human`** (§8.3). |
| `status` | anyone | Read-only pretty-print. No write. |

The verbs no longer take an issue argument — there is one workflow file per worktree
(§3.1). The issue is recorded at init from the invoking skill's context.

### 6.1 The directive (`next`'s stdout)

```jsonc
// AUDIT, mid-flow:
{ "phase":"local", "role":"audit", "round":2,
  "do":"Review cumulative delta origin/develop..a1b2c3; focus on commits since 9f8e7d. Run the judgment checks below.",
  "checks":["commit-message-fidelity","scope-coherence", "..."],
  "range":"origin/develop..a1b2c3", "since":"9f8e7d",
  "then": { "verb":"submit-review", "schema":"review.v1" } }

// USER, changes round:
{ "phase":"local", "role":"user", "round":2,
  "do":"Address these findings, commit fixes, validate green, then report.",
  "findings":[ { "check":"commit-message-fidelity", "file":"src/x.py", "line":42, "note":"…" } ],
  "then": { "verb":"report-fixes" } }

// USER, init:
{ "phase":"local", "role":"user", "round":0,
  "do":"Implement issue #1534 on this branch. Validate green. Then report PR metadata.",
  "then": { "verb":"report-ready", "schema":"pr-metadata.v1" } }

// DONE (approved):
{ "done":true, "reason":"approved", "next_human_action":"run vrg-submit-pr" }
```

The directive points AUDIT at the registry's check prompts; each prompt specifies
its own output shape so the assembled `review.v1` payload needs minimal massaging.

### 6.2 `--no-audit` (solo mode)

For a small, high-confidence change the human can skip the *local* audit to save
tokens (a real cost today; "just electricity" once this runs on a local model). The
human passes `--no-audit` up front. The oracle then:

- Writes the file with `mode: "solo"`, `owner: "user"`, **no** startup handshake and
  **no** wait for AUDIT.
- Runs USER straight through implement → `report-ready` → approved (no review
  rounds).
- Records the skip in `history` (`mode: "solo"`) so it is visible in the trail.

Guardrails: an AUDIT session accidentally started against a `solo` file reads the
mode and **exits cleanly** ("this workflow is running `--no-audit`; nothing to do").
**The PR-phase audit remains mandatory** — `--no-audit` optimizes only the local
loop, never the merge gate.

## 7. Payload schemas

- **`pr-metadata.v1`** — `{ title, summary, notes, linkage? }`. Validated on
  `report-ready`.
- **`review.v1`** — `{ checks: [ { id, status: pass|fail|escalate,
  findings?: [ { file, line, severity, note } ], reason? } ] }`. Validated on
  `submit-review`. Unknown or missing check ids, or a bad status enum, are rejected
  so the model retries (validation at the call layer, Diogenes-style).

## 8. State machine and transitions

```
init (USER first `next`)
  ├─ --no-audit → mode=solo, owner=user → implement → report-ready → approved (DONE)
  └─ paired → mode=paired, owner=audit, wait for AUDIT ack (§8.1)
       AUDIT acks (writes presence, flips owner→user) → USER implements
       USER ──report-ready──▶ owner=audit
       AUDIT next → runs judgment checks on base..HEAD → submit-review{checks}
         oracle rolls up the ledger:
           any escalate → owner=human, status=escalated, notify    ─┐
           else any fail → owner=user, status=changes-requested      │
           else all pass → status=approved, owner=user → "tell human │
                            to run vrg-submit-pr" (local DONE)        │
       USER changes round: next → findings → fix → report-fixes ──▶ owner=audit ↺
       USER or AUDIT escalate ─────────────────────────────────────┤
                                                                     ▼
                                          HUMAN resolves → resolve --to user|audit
```

Rollup rule, applied on every `submit-review`: **any `escalate` → human; else any
`fail` → user; else all `pass` → approved.**

### 8.1 Startup handshake (paired mode)

Because the two sessions start at the repo root and the human points them at a
worktree by hand, a misconfiguration (two sessions not actually sharing a worktree,
or aimed at different issues) is easy. The handshake catches it and confirms mutual
presence before real work begins:

1. **USER `next` (init):** create the file, mint and record the USER presence token,
   set `owner: audit`, and **block** waiting for `owner` to flip back to `user`.
2. **AUDIT `next`:** expect the file to exist. As the current owner, write the AUDIT
   presence token **and flip `owner: user`** (the ack). If the file does not exist
   within a **short** timeout, raise an exception ("no workflow file — is the
   implement session running in this worktree?"). Also assert the file's recorded
   `issue` matches the issue AUDIT was told to audit; mismatch → exception.
3. **USER unblocks** on `owner == user` and proceeds to implement. If AUDIT never
   acks within the **short** handshake timeout, USER **raises an exception** — it
   does **not** silently fall back to solo. Solo is only ever entered by an explicit
   `--no-audit` (§6.2).

Every steady-state write is still by the current owner. The only writes outside that
rule are USER's one-time bootstrap (creating the file) and AUDIT's ack (which it does
*as* the owner) — both are by design, not races.

### 8.2 The human leg

When an agent escalates, `owner→human` and the escalating agent prints a loud banner
in its own terminal (what, why, which check) before parking. The non-escalating
agent simply stays parked on its blocking `next`. The human resolves through the
same CLI (`resolve --to <role> --note …`), which writes state and flips ownership,
unblocking the right agent. No external/push notifications in the local phase; the
human is watching the two sessions.

### 8.3 Ownership invariant and human authentication

- **Only the current owner writes** (steady state). The bootstrap and ack writes in
  §8.1 are the sole, by-design exceptions.
- **Human verbs require a human identity.** `resolve` (and any future human-only
  verb) is rejected unless `vrg-whoami --mode == human`. Inside an agent VM,
  `vrg-whoami` resolves to `user`/`audit`, so the human must run these from a
  **human-identity context — the host shell** (where, with no agent signals present,
  `identity_mode` falls back to `human`), **not** via `!` inside an agent session.
  This gives the human gate real enforcement instead of relying on convention.

## 9. Edge cases and error handling

- **Change detection uses SHA-256 polling, not mtime.** `wait_for_file` already
  recomputes the SHA-256 each 1s poll and deliberately rejects mtime gating, because
  mtime resolution/semantics vary across filesystems — and this runs on a shared
  host mount where that flakiness is real. The files are tiny; correctness over a
  micro-optimization that does not matter at this size (see `await_file.py`).
- **Two timeout regimes.** The startup handshake (§8.1) uses a **short** timeout
  (seconds — register or fail). Steady-state waits are **long / effectively
  indefinite** (real work takes minutes to hours), under human supervision.
- **Crash propagation (best-effort).** On a *graceful* give-up (a caught exception),
  the agent records a terminal `error: { by, at, reason }` and `status: error`; the
  counterpart sees it on its next poll and stops with a complementary exception. On
  a *hard* death (VM dies, `kill -9`) the agent writes nothing, so the counterpart
  cannot be signaled and falls back to the long timeout plus the supervising human.
  These agents are not autonomous; this limitation is acceptable and stated rather
  than implied away.
- **Out-of-turn write** — the oracle re-reads state before every write; if
  `caller_role != owner`, it rejects non-zero with a clear message.
- **Crash / resume** — all state is in the file; the oracle is stateless between
  calls. A killed agent calls `next` again and resumes from the current
  `owner`/`round`. `next` as USER on an existing file is **takeover, not clobber**;
  if the file records a *different* issue it is treated as stale → refuse and tell
  the human to delete it (the revert path: `rm .vergil/pr-workflow.json` and
  restart).
- **Malformed report** — `submit-review`/`report-ready` payloads are validated
  against their schemas; rejection is non-zero so the model retries.
- **No new commits on `report-fixes`** — the oracle verifies HEAD actually moved
  since `last_reviewed_sha`; if not, it rejects ("nothing to review") to prevent
  empty-round loops.
- **Runaway rounds** — a `round` counter with a configurable cap (in `vergil.toml`);
  exceeding it auto-escalates to the human rather than looping forever.
- **Identity misread** — role comes from `vrg-whoami`; `next` warns on signal
  disagreement (`vrg-whoami --explain`), catching the "both sessions resolved the
  same identity" setup error.

## 10. Testing strategy

- **Engine** — pure transition/rollup tests against an in-memory fake transport (no
  files): given state plus a reported result, assert owner/status/round/rollup.
- **Transport contract test** — one suite that both `LocalFileTransport` (now) and
  `GitHubTransport` (later) must pass. This is what keeps the PR extension honest.
- **LocalFileTransport** — atomic write, SHA-256 change detection, takeover,
  stale-file refusal, malformed-JSON handling.
- **Handshake** — paired ack flips owner and unblocks USER; short-timeout no-show
  raises; issue-mismatch raises; solo mode skips the handshake; AUDIT-on-solo exits
  cleanly; graceful give-up writes terminal `error` and the counterpart stops.
- **Schema** — good and bad `review.v1` and `pr-metadata.v1` payloads.
- **End-to-end** — subprocess `vrg-pr-workflow` against a temporary git repo through
  the full happy path (init → handshake → report-ready → review(changes) →
  report-fixes → review(approve) → DONE), the solo path, and the escalation path;
  assert the resulting `history` ledger.
- **Check prompts** — tested for output-schema conformance against fixture diffs.
  The judgment *quality* itself is eval-style and deferred.

## 11. Integration and migration

- **Subsumes** `.vergil/pr-template.yml` (→ `pr_metadata`) and
  `.vergil/audit-feedback.yml` (→ `checks` ledger). The single state file replaces
  both.
- **`vrg-submit-pr`** reads PR fields from `pr_metadata` and, on success, attaches
  the final state JSON (ledger + history) to the issue before the usual cleanup.
- **`lib/await_file.py`** primitives (`atomic_write`, SHA-256 polling) are reused by
  `LocalFileTransport`. `vrg-await`'s role becomes internal to the transport;
  `vrg-pr-await` and `vrg-audit-approve` become the raw material for the future
  `GitHubTransport`.
- **Skills** `vergil:implement` and `vergil:audit` are rewritten to the dumb loop.

## 12. Implementation phases

Split into three sequential phases; each delivers independent, testable value and
lands as its own plan and PR.

### Phase 1 — Engine core

The state schema; the engine (state machine, rollup, directive generation, startup
handshake, `--no-audit`); the transport interface; `LocalFileTransport`; the shared
transport **contract test**; and the end-to-end subprocess test. Delivers a fully
tested `vrg-pr-workflow` mechanism with **no** agent/skill wiring — driven entirely
by tests.

### Phase 2 — Judgment registry

The six seed check prompts and the registry that holds them, authored so each
prompt's output maps 1:1 into `review.v1`. Delivers the actual audits, pluggable
into the Phase 1 engine.

### Phase 3 — Integration

Rewrite `vergil:implement` and `vergil:audit` to the dumb loop; wire `vrg-submit-pr`
to read `pr_metadata` and attach the final JSON to the issue. Delivers the wired,
end-to-end local workflow.

**Deferred across all phases:** `GitHubTransport` and the `pr-watch` rewrite; the
exception/suppression check as a deterministic `vrg-validate` gate (its own work);
resumability beyond takeover; an eval harness for judgment quality.

## 13. Open questions

- Exact home and format of the check prompts (alongside the engine in
  vergil-tooling, vs. in the plugin skills tree like Diogenes' compiled prompts).
- The `vergil.toml` shape for the runaway-round cap, the handshake/steady-state
  timeout values, and any per-repo check enable/disable.
- Whether `vrg-pr-workflow next` should auto-detect phase (local vs PR) from the
  presence of a PR for the branch, or take it explicitly — relevant once
  `GitHubTransport` lands.
