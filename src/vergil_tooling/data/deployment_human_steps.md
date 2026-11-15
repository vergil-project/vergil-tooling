## Human-run steps (need a human identity)

List here every step the agent identity cannot run. For example,
`vrg-github-repo-config update` refuses the agent identity modes (`user`,
`audit`) and exits 2. For each step, the agent checks the step's preconditions,
gives the human the paste-ready command to run, waits for it, then records the
step's check. For repo config, the check is the human's
`vrg-github-repo-config update` exiting 0 (in sync, or its sync PR merged):
`update` refuses, exiting 2 and writing nothing, when it cannot read every
setting first. A `vrg-github-repo-config diff` the agent runs afterwards is
informational only. Under the agent identity `diff` exits 2, because the
admin-scoped settings are not checked; that is expected and is not a failure of
the update. Never treat `diff`'s exit code as the verdict on an update.
Never run a human-run step under the agent identity just to record "blocked".
Delete this section when every step is agent-safe.

{steps}
