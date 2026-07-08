{intro}

This is a **validation task**: run the checklist below and record PASS/FAIL as a
comment. It is not PR-workable — it has no code PR and never auto-closes. Close
it only on PASS; on FAIL, record the evidence, file follow-on fix task(s), and
leave this task (and its epic) open.

## Preconditions (self-check — run first, do not fabricate)

Declare this task's preconditions here and check them before anything else. They
are author-defined and generic — a machine-checkable probe (a health/status
command, or a check that the dependency change is actually deployed) *or* a
human-attested statement (e.g. "the target has been rebuilt to include the
dependency below"). The framework prescribes no mechanism.

- \<precondition — probe or human-attested\>

If any precondition is unmet: comment "blocked: preconditions not met —
\<which\>" and stop. Do not run the checklist. Never fabricate a result.

{blocked_by}## Commands to run

- \<concrete validation command(s)\>

## Acceptance criteria

- \<explicit pass/fail condition(s)\>

## Results

Post the outcome as a comment on this issue, then close only on PASS.

- Outcome: PASS / FAIL
- Evidence: \<command output / observations\>
- On FAIL: file follow-on fix task(s); leave this task and the epic open.
