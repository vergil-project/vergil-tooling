{intro}

This is a **deployment task**: run the deploy steps below so the merged change
is usable, then record the result as a comment. The agent runs the agent-safe
steps itself and hands each human-run step to the human. It is not
PR-workable and never auto-closes. Close it only on SUCCESS; on FAILURE, retry
(the deploy is idempotent), and file a fix task only if it cannot succeed without
a code change — leaving this task (and its epic) open.

## Preconditions (self-check — run first, do not fabricate)

Declare this task's preconditions and check them before anything else. If
deploying requires a **release** (bump / tag / publish), that release is a
**human-gated precondition** — attest it here; `issue-deploy` never cuts a
release. Also confirm the target is reachable.

- \<precondition — e.g. "release vX.Y.Z is published"; target reachable\>

If any precondition is unmet: comment "blocked: preconditions not met —
\<which\>" and stop. Do not run the deploy. Never fabricate a result.

{blocked_by}## Deploy steps (agent-safe; idempotent)

- \<install / sync / restart command(s) the agent identity can run\>

## Human-run steps (need a human identity)

List here every step the agent identity cannot run. For example,
`vrg-github-repo-config apply` needs `administration=read` and refuses under an
agent identity. For each step, the agent checks the step's preconditions, gives
the human the paste-ready command to run, waits for it, then records the step's
check. For repo config, the check is the human's `vrg-github-repo-config apply`
exiting 0: `apply` refuses, exiting 2 and writing nothing, when it cannot read
every setting first. A `vrg-github-repo-config diff` the agent runs afterwards
is informational only. Under the agent identity `diff` exits 2, because the
admin-scoped settings are not checked; that is expected and is not a failure of
the apply. Never treat `diff`'s exit code as the verdict on an apply.
Never run a human-run step under the agent identity just to record "blocked".
Delete this section when every step is agent-safe.

- \<command the human runs\> — check: \<what shows it succeeded, e.g. the
  human's command exits 0\>

## Acceptance criteria

- \<how you know the change is deployed and usable\>

## Results

Post the outcome as a comment on this issue, then close only on SUCCESS.

- Outcome: SUCCESS / FAILURE
- Evidence: \<command output / observations\>
- On FAILURE: retry (idempotent); file a fix task only for a genuine defect;
  leave this task and the epic open.
