# PR Workflow Oracle — design

- **Date:** 2026-06-08
- **Issue:** [#1534](https://github.com/vergil-project/vergil-tooling/issues/1534)
- **Status:** Draft (design approved in brainstorming; implementation plan to follow)
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
**oracle**, and gives the audit step real, useful work to do.

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

1. **State file** (`.vergil/pr-<issue>.json`) — the single source of truth and the
   rendezvous point. Pure data. The oracle is its only writer; nobody hand-edits
   it, including the human.

2. **The oracle** (`vrg-prw`, Python in vergil-tooling) — the entire brain. It owns
   every write, enforces turn-taking, snapshots git facts itself, rolls per-check
   results up into transitions, and blocks server-side until it is the caller's
   turn. Reuses the existing `atomic_write` + SHA-poll primitives from
   `lib/await_file.py`.

3. **Two identical, dumb skills** — `vergil:implement` (USER) and `vergil:audit`
   (AUDIT) both collapse to: *call `vrg-prw next --as <role>`, do what it says,
   report back through the verb it names, repeat until done.* No workflow branching
   in prose. Role comes from `vrg-whoami`, never a guessed flag.

4. **The human** — a first-class participant who acts only through `vrg-prw` verbs.
   Both agents block while the human holds the turn.

### 3.1 The transport seam (two phases, one engine)

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
| State store | `.vergil/pr-<issue>.json` | Distributed across the PR: check-runs, review comments, head SHA |
| Change detection | Gate on file **mtime**; only on change do we hash (SHA); only on hash change do we parse JSON. Cheap; no spin lock. | Record head SHA at PR creation, then block until head SHA changes — the existing `vrg-pr-await` logic. |
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

`.vergil/pr-<issue>.json` — what `LocalFileTransport` serializes. One file per
issue (the issue number is the join key between the two agents). Gitignored scratch.

```jsonc
{
  "schema_version": 1,
  "issue": 1534,
  "branch": "feature/1534-pr-workflow-oracle",
  "base": "origin/develop",
  "phase": "local",                 // "local" now; "pr" is the deferred second phase
  "owner": "audit",                 // user | audit | human — LocalFileTransport's mutex only
  "status": "reviewing",            // derived: implementing | reviewing | changes-requested | approved | escalated
  "round": 2,
  "created_at": "2026-06-08T15:00:00Z",
  "updated_at": "2026-06-08T15:42:00Z",

  "pr_metadata": {                  // subsumes pr-template.yml; read by vrg-submit-pr
    "title": "feat(prw): ...",
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

  "history": [                      // append-only; the audit trail attached to the issue
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
- The oracle snapshots all `git` SHAs itself by shelling out, rather than trusting
  the agent to report them — this keeps "agent emits facts, Python derives state"
  honest.
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
| `next --as <role> <issue>` | USER, AUDIT | Block (mtime → sha → parse). First-ever call as USER: **init/takeover** the file. Return a Directive or DONE. |
| `report-ready <issue> --title --summary --notes [--linkage]` | USER | Snapshot HEAD, write `pr_metadata`, `owner→audit`. (Initial done signal.) |
| `report-fixes <issue> [--note]` | USER | Verify HEAD moved, snapshot it, `owner→audit`, bump round. |
| `submit-review <issue> --payload review.json` | AUDIT | Validate payload vs `review.v1`, update ledger, **roll up** → set owner/status, record `last_reviewed_sha`. |
| `escalate <issue> --reason …` | USER, AUDIT | `owner→human`, set `escalation`, status=escalated. |
| `resolve <issue> --to user\|audit [--note]` | HUMAN | Hand control back to an agent. |
| `status <issue>` | anyone | Read-only pretty-print. No write. |

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

## 7. Payload schemas

- **`pr-metadata.v1`** — `{ title, summary, notes, linkage? }`. Validated on
  `report-ready`.
- **`review.v1`** — `{ checks: [ { id, status: pass|fail|escalate,
  findings?: [ { file, line, severity, note } ], reason? } ] }`. Validated on
  `submit-review`. Unknown or missing check ids, or a bad status enum, are rejected
  so the model retries (validation at the call layer, Diogenes-style).

## 8. State machine and transitions

```
init (USER first `next`, creates/takes over file, owner=user)
  └─ USER implements ──report-ready──▶ owner=audit
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

### 8.1 The human leg

When an agent escalates, `owner→human` and the escalating agent prints a loud banner
in its own terminal (what, why, which check) before parking. The non-escalating
agent simply stays parked on its blocking `next`. The human resolves through the
same CLI (`resolve --to <role> --note …`), which writes state and flips ownership,
unblocking the right agent. No external/push notifications in the local phase; the
human is watching the two sessions. This is consistent with "Python owns every
write" — the human never hand-edits the file.

## 9. Edge cases and error handling

- **Out-of-turn write** — the oracle re-reads state before every write; if
  `caller_role != owner`, it rejects non-zero with a clear message. Defensive: the
  agent follows directives and should not reach this.
- **Crash / resume** — all state is in the file; the oracle is stateless between
  calls. A killed agent calls `next` again and resumes from the current
  `owner`/`round`. `next --as user` on an existing file is **takeover, not
  clobber**.
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
- **LocalFileTransport** — atomic write, mtime → sha → parse change detection,
  takeover, malformed-JSON handling.
- **Schema** — good and bad `review.v1` and `pr-metadata.v1` payloads.
- **End-to-end** — subprocess `vrg-prw` against a temporary git repo through the
  full happy path (init → report-ready → review(changes) → report-fixes →
  review(approve) → DONE) and the escalation path; assert the resulting `history`
  ledger.
- **Check prompts** — tested for output-schema conformance against fixture diffs.
  The judgment *quality* itself is eval-style and deferred.

## 11. Integration and migration

- **Subsumes** `.vergil/pr-template.yml` (→ `pr_metadata`) and
  `.vergil/audit-feedback.yml` (→ `checks` ledger). The single state file replaces
  both.
- **`vrg-submit-pr`** reads PR fields from `pr_metadata` and, on success, attaches
  the final state JSON (ledger + history) to the issue before the usual cleanup.
- **`lib/await_file.py`** primitives (`atomic_write`, SHA polling) are reused by
  `LocalFileTransport`. `vrg-await`'s role becomes internal to the transport;
  `vrg-pr-await` and `vrg-audit-approve` become the raw material for the future
  `GitHubTransport`.
- **Skills** `vergil:implement` and `vergil:audit` are rewritten to the dumb loop.

## 12. Scope of the first implementation

**In scope:** the engine (state machine, rollup, registry mechanism, directive
generation); `LocalFileTransport`; the `vrg-prw` verbs; the six seed check prompts;
`vrg-submit-pr` integration (read `pr_metadata`, attach final JSON to the issue);
rewrite of the `implement`/`audit` skills to the dumb loop.

**Deferred:** `GitHubTransport` and the `pr-watch` rewrite; the
exception/suppression check as a deterministic `vrg-validate` gate (its own work);
resumability beyond takeover; an eval harness for judgment quality.

## 13. Open questions

- Exact home and format of the check prompts (alongside the engine in
  vergil-tooling, vs. in the plugin skills tree like Diogenes' compiled prompts).
- The `vergil.toml` shape for the runaway-round cap and any per-repo check
  enable/disable.
- Whether `vrg-prw next` should auto-detect phase (local vs PR) from the presence of
  a PR for the branch, or take it explicitly — relevant once `GitHubTransport`
  lands.
