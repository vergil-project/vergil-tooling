# Permission Model

VERGIL constrains AI agents to a validated set of operations
through layered enforcement. Every system interaction flows through
wrapper tools that validate, constrain, and log what happens. Raw
shell access to `git` and `gh` is denied mechanically — not by
convention, not by instruction, but by the permission system
itself.

For the identity model that permissions protect, see
[Identity Architecture](identity-architecture.md). For how
credentials are selected per-operation, see
[Credential Management](credential-management.md).

## Base Permission Mode

**`bypassPermissions`** is the standard mode — but only because the
agent runs inside a sandboxed, ephemeral VM (see
[The VM Sandbox Boundary](#the-vm-sandbox-boundary)). Inside that
sandbox the agent reads, edits, and runs shell commands without
per-action prompts, which is what makes it usable for real work.

### Why not `acceptEdits`

`acceptEdits` was the original target: auto-approve file edits, prompt
on any shell command not in the allowlist. It proved impractical for
the work VERGIL actually does. An agent doing infrastructure work runs
a constant stream of shell commands — container builds, cloud CLIs,
system inspection, ad-hoc scripts — and under `acceptEdits` every
non-allowlisted one stops for human approval. The prompts never end,
and autonomous work becomes impossible. Chasing an ever-growing bash
allowlist was a losing race.

So VERGIL moved the boundary outward instead of inward: rather than
constrain *which commands* the agent may run, **sandbox the whole agent
in a disposable VM** and let it run in `bypassPermissions` there. The
blast radius is the VM, which is ephemeral and reproducible.

### Bypass is not "no enforcement"

`bypassPermissions` removes the *prompts*, not the *guardrails*. The
`vrg-*` wrappers, the deny rules, and the hook guard all still apply —
a hook hard-block (exit code 2) overrides even bypass mode — and the
server-side GitHub App permission shapes remain the ultimate boundary
(see [The Actual Security Boundary](#the-actual-security-boundary)).
Bypass changes the agent's *experience inside the sandbox*; it does not
widen what the agent can do to the outside world.

## `vrg-git` Wrapper

A Python CLI tool that validates subcommands and flags before
executing `git`. Arguments arrive as a Python argv list — no
shell expansion, no pipes, no redirection.

### Subcommand Allowlist

| Subcommand | Denied Flags | Notes |
|---|---|---|
| `status` | — | Read-only |
| `log` | — | Read-only |
| `diff` | — | Read-only |
| `show` | — | Read-only |
| `branch` | `--force`; `-D` conditional | `-d` allowed; `-D` allowed when upstream is `[gone]` |
| `ls-remote` | — | Read-only |
| `rev-parse` | — | Read-only |
| `annotate` | — | Read-only — line-level authorship |
| `blame` | — | Read-only — line-level authorship |
| `cat-file` | — | Read-only — object type/size/content |
| `cherry` | — | Read-only — commits not yet upstream |
| `count-objects` | — | Read-only — repo object stats |
| `describe` | — | Read-only — nearest reachable tag |
| `diff-files` | — | Read-only — plumbing diff |
| `diff-index` | — | Read-only — plumbing diff |
| `diff-tree` | — | Read-only — plumbing diff |
| `for-each-ref` | — | Read-only — enumerate refs |
| `grep` | — | Read-only — search tracked content |
| `ls-files` | — | Read-only — list tracked files |
| `ls-tree` | — | Read-only — list a tree's contents |
| `merge-base` | — | Read-only — common-ancestor lookup |
| `name-rev` | — | Read-only — symbolic names for commits |
| `reflog` | `expire`, `delete` | Read-only show/exists allowed; mutating sub-ops denied |
| `rev-list` | — | Read-only — list commits |
| `shortlog` | — | Read-only — summarized log |
| `show-branch` | — | Read-only — compare branches |
| `show-ref` | — | Read-only — list refs |
| `symbolic-ref` | `<name> <ref>`, `-d`/`--delete`, `-m` | Read form only — one ref, optionally `-q`/`--quiet`/`--short` |
| `remote` | `add`, `remove`, `rename`, `set-url`, `set-head`, `prune`, `update` and every other form | Read forms only — `remote [-v\|--verbose]`, `remote get-url [--push] [--all] <name>`, `remote show -n <name>...` |
| `config` | every write (`<name> <value>`, `--add`, `--unset`, `--replace-all`, section edits, `-e`, and the `set`, `unset`, `rename-section`, `remove-section` and `edit` subcommands) | Read forms only — one of `--get`, `--get-all`, `--get-regexp`, `--list`/`-l`, or the `get <name>` and `list` subcommands (`get` with `--all`, `--regexp`, `--fixed-value`, `--show-names`, `--value=`, `--url=`, `--default=`), with scope and output flags |
| `tag` | `-a`, `-s`, `-u`, `-m`, `-F`, `-f`, `-d`, `-v`, `-e`, and a tag name without a list flag | List forms only — no tag name, or `-l`/`--list`, `--contains`, `--no-contains`, `--points-at`, `--merged`, `--no-merged`, `-n<num>` |
| `var` | — | Read-only — logical git variables |
| `verify-commit` | — | Read-only — signature verification |
| `verify-tag` | — | Read-only — signature verification |
| `whatchanged` | — | Read-only — log with file changes |
| `worktree add` | — | Parallel agent work |
| `worktree list` | — | Read-only |
| `worktree remove` | — | Cleanup after merge |
| `add` | — | Staging files |
| `push` | `--force`, `-f`; `--force-with-lease` conditional | `--force-with-lease` allowed on non-protected branches |
| `fetch` | — | Read-only |
| `pull` | — | Fast-forward updates |
| `checkout` | — | A pathspec checkout (`checkout .`, `checkout -- <path>`) is a local rewrite |
| `switch` | — | Branch switching |
| `stash` | — | All stash subcommands allowed |
| `merge` | — | Branch updates |
| `cherry-pick` | — | Selective commit application |
| `rebase` | `-i`, `--interactive` without `--autosquash` | `rebase -i --autosquash <base>` is a local rewrite |
| `reset` | — | A local rewrite, every mode |
| `clean` | — | A local rewrite |

### Local Rewrites

`vrg-git` protects origin, not the local checkout. `reset`, `clean`, a
pathspec `checkout` and `rebase -i --autosquash` rewrite or discard local
work, so they run only on a branch that is neither protected (`develop`,
`main`, `release/*`) nor frozen (reported ready). A detached HEAD is
refused, except mid-rebase, where the branch being rebased is checked.
Unlike commit and push, which warn and proceed, they fail closed when
`.vergil/pr-workflow.json` exists but cannot be read. The no-op editors
vrg-git sets for every rebase (`GIT_SEQUENCE_EDITOR=true`,
`GIT_EDITOR=true`) keep `rebase -i --autosquash` non-interactive.

### Denied Subcommands

Anything not on the allowlist is rejected. Notable denials:

- **`commit`** — all commits flow through `vrg-commit`
- **`gc`**, **`prune`** — repository maintenance
- **`filter-branch`**, **`replace`** — history rewriting

### Escape Hatch

None in the wrapper. If the agent genuinely needs a denied
operation, it explains the situation to the human and hands over
the raw command as a separate copy-paste block, with no `!`
prefix. The human runs it in their own terminal, not through `!`
in the agent session.

## `vrg-gh` Wrapper

Same architecture as `vrg-git`. Validates the top-level and
second-level subcommands as a pair.

### Subcommand Allowlist

The allowlist depends on the identity mode (resolved by
`vrg-whoami`). The `human` and `user` identities share one
allowlist (`_ALLOWED`), from which the `user` agent loses the
agent-denied pairs below. The `audit` identity has its own, much
narrower allowlist (`_ALLOWED_AUDIT`). Any pair not listed is
rejected as not recognized.

| Subcommand | `human` | `user` | `audit` | Notes |
|---|---|---|---|---|
| `issue view` | yes | yes | — | Read-only |
| `issue list` | yes | yes | — | Read-only |
| `issue edit` | yes | yes | — | Updating metadata; the raw sub-issue flags (`--parent`, `--add-sub-issue`, `--remove-sub-issue`) are refused for every identity, since the epic chain owns sub-issue links: use `vrg-epic-link` or `vrg-epic-move` |
| `issue comment` | yes | yes | — | Adding comments |
| `issue close` | yes | yes | — | Closing is mechanical and reversible; an agent's completed close of an operational task waits for its result (see [Operational task close](#operational-task-close)) |
| `issue reopen` | yes | yes | — | Non-destructive; an errant reopen is re-closed |
| `issue transfer` | yes | yes | — | Intra-org issue move (finite-epic relocation) |
| `pr view` | yes | yes | yes | Read-only |
| `pr checks` | yes | yes | yes | CI status |
| `pr list` | yes | yes | yes | Read-only |
| `pr diff` | yes | yes | yes | Read-only |
| `pr comment` | yes | yes | yes | Review context |
| `pr review` | yes | yes | yes | `--approve` is refused for `user` |
| `pr update-branch` | yes | yes | — | Server-side update onto the base (`--rebase` allowed) |
| `pr edit` | yes | — | — | Agents do not edit PRs |
| `pr merge` | yes | — | — | Human only, and a PR number or URL is required |
| `run list` | yes | yes | — | CI status |
| `run view` | yes | yes | — | CI status |
| `run watch` | yes | yes | — | Blocking wait for CI |
| `run rerun` | yes | yes | — | Retry a failed CI job (`--failed` allowed) |
| `run job-log` | yes | yes | yes | One job's log, read-only; see [`run job-log`](#run-job-log) |
| `workflow list` | yes | yes | yes | Read-only CI configuration |
| `workflow view` | yes | yes | yes | Read-only CI configuration |
| `cache list` | yes | yes | yes | Read-only CI state |
| `repo view` | yes | yes | — | Read-only |
| `repo list` | yes | yes | — | Read-only |
| `release list` | yes | yes | — | Read-only release facts |
| `release view` | yes | yes | — | Read-only release facts |
| `release download` | yes | yes | — | Read-only on GitHub: fetches a release's assets to local files |
| `label list` | yes | yes | — | Read-only |
| `label create` | yes | yes | — | Used by `vrg-ensure-label` |
| `variable list` | yes | yes | — | Read-only Actions variables |
| `variable get` | yes | yes | — | Read-only Actions variables |
| `search code` / `commits` / `issues` / `prs` / `repos` | yes | yes | — | Read-only, cross-org search |
| `api` | full | — | GET only | See [`gh api`](#gh-api-by-identity) |

The `audit` identity is a read/review reviewer, so it gets the
PR read and review actions plus the read-only `workflow list`,
`workflow view`, `cache list` and `run job-log`, which only read CI
configuration and state a reviewer may need to judge a PR's
checks. Every write (`run rerun`, `pr update-branch`, the
`issue` actions) stays off its allowlist.

### Denied Subcommands

These pairs are refused with an explanatory message rather than
a bare "not recognized":

| Subcommand | Denied for | Reason |
|---|---|---|
| `issue create` | every identity | Use `vrg-issue-create`, `vrg-epic-create` or `vrg-intake-create`, which force the issue's linkage |
| `pr close` | every identity | Agents do not close PRs |
| `repo edit` | every identity | Admin operation |
| `repo create` | every identity | Admin operation |
| `repo delete` | every identity | Destructive |
| `workflow run` | every identity | Dispatching a workflow can start CD or a release, which is a human action |
| `auth` | every identity | Credential management |
| `pr create` | every identity | The user agent opens a PR only through the gated path, `vrg-pr-workflow report-ready` then `vrg-submit-pr`; a human is pointed to `vrg-submit-pr` |
| `pr edit` | `user`, `audit` | No agent edits a PR with `gh`; the user agent corrects the PR metadata with `report-ready` before `vrg-submit-pr` opens the PR |
| `pr merge` | `user`, `audit` | The user agent merges only through the gated path: `vrg-submit-pr` turns auto-merge on |
| `pr review --approve` | `user` | Approval is restricted to the `audit` identity or a human |
| `api` | `user` | Broad, write-capable escape hatch |
| `api` (non-GET) | `audit` | Restricted to read-only GET calls |

Other write actions of allowed top-level commands (`release
create`, `workflow enable`/`disable`, `cache delete`, `variable
set`/`delete` and so on)
are rejected by the allowlist's deny-by-default.

### Operational task close

An operational task (a `validation` or `deployment` issue) is accepted
by a recorded `Outcome: SUCCESS` comment, and closes only once that
result has posted. For an agent identity, `vrg-gh issue close` makes
that order mechanical: before it closes an issue as completed (gh's
default reason), it reads the issue's labels and, for an operational
task, its trusted comments through the trust layer. It refuses the
close while no trusted comment carries an `Outcome: SUCCESS` line
(`Outcome: PASS` is the legacy alias), so a close sent in one batch
with a result comment that failed cannot close the task with no result
above it. The close's own `--comment` does not count as the result.

A close as `not planned` or a duplicate claims no result and is not
gated, and a human's close is not gated either. A target the wrapper
cannot resolve (a positional that is not one issue number or URL, or a
`--repo` that is not `OWNER/REPO`) and a read that cannot complete
refuse the close rather than let it through unchecked. The success
marker is the one `vrg-epic-audit` checks on closed operational tasks.

### `gh api` by Identity

`gh api` can perform any GitHub API operation — create repos,
delete branches, modify settings, push code via the Contents API.
It has no fixed sub-actions, so the wrapper gates it by identity
instead of by pair:

- **`human`** — full access.
- **`audit`** — read-only `GET` calls only. A call is treated as a
  write when it passes `-X`/`--method` with anything but `GET`, or
  passes a field flag (`-f`, `-F`, `--field`, `--raw-field`,
  `--input`) without an explicit `GET`.
- **`user`** — denied entirely, which closes the escape hatch that
  individual hook-based blocks cannot fully cover.

The user identity's one read it needs from the raw API, a PR's inline
review comments, goes through the dedicated read-only
[`vrg-pr-review-comments`](../reference/cli-tools-overview.md#vrg-pr-review-comments)
rather than an exception in this refusal.

### `run job-log`

`vrg-gh run job-log --repo <owner>/<repo> --job <job-id>` prints one
GitHub Actions job's log. It is the wrapper's own command, not a `gh`
subcommand: `gh run view --job <id> --log` downloads the whole run's
log archive, which fails on a run with many jobs, and the per-job log
endpoint is otherwise reachable only through `gh api`.

It calls exactly one endpoint, a `GET` of
`repos/<owner>/<repo>/actions/jobs/<job-id>/logs`, which it builds
itself, so it reopens none of the `gh api` access above. It accepts
only `--repo` (or `-R`) and `--job`, each once, and forwards no other
argument. `--repo` must be a plain `<owner>/<repo>` made of the
characters GitHub allows in those names, and `--job` a positive
number. Anything else is refused before `gh` runs. The App
installation is chosen by the `--repo` owner. Because it only reads,
it is open to every identity, `audit` included.

Job logs usually carry ANSI color codes, which `gh` refuses to print
unless told to. The command lets `gh` return them and then strips every
terminal escape sequence and control character except tab and newline
before printing, so none reaches the terminal.

### Credential Selection

`vrg-gh` is responsible for choosing which account's token to use
per-command. See [Credential Management](credential-management.md)
for the selection logic.

### VRG Tools That Bypass the Wrapper

Mechanized VRG tools call `gh` and `git` directly in their Python
code, mostly through the shared `lib/github.py` and `lib/git.py`
helpers, not through `vrg-gh` and `vrg-git`. They bypass the wrappers
because they are already validated: the wrappers only constrain
agent-initiated `gh` and `git` calls via the Bash tool.

These tools call both `gh` and `git`. Most of them call `git` only to
read the `origin` remote, which resolves the org whose GitHub App
token `gh` runs with:

`vrg-activity-log`, `vrg-adhoc-epic`, `vrg-audit-approve`,
`vrg-branch-sweep`, `vrg-ci-evidence`,
`vrg-container-docs`, `vrg-dependabot-audit`, `vrg-docs-stage`,
`vrg-ensure-label`, `vrg-epic-audit`, `vrg-epic-create`,
`vrg-epic-docs`, `vrg-epic-link`, `vrg-epic-move`, `vrg-epic-parent`, `vrg-epic-rollup`,
`vrg-finalize-pr`, `vrg-github-repo-config`,
`vrg-github-repo-init`, `vrg-intake-create`, `vrg-intake-route`,
`vrg-issue-create`, `vrg-pr-await`, `vrg-pr-workflow`, `vrg-promote`,
`vrg-queue`, `vrg-release`, `vrg-repo-status`,
`vrg-resolve-tracking-issue`, `vrg-roadmap`, `vrg-ruling`,
`vrg-ruling-reply`, `vrg-scope-change`, `vrg-security-record`,
`vrg-submit-pr` and `vrg-update-deps`.

`vrg-upstream-refresh` also clones, commits and pushes, in temporary
clones. Under GitHub Actions its `publish` mode takes every `gh` and
`git` token from an in-process automation App token source and runs as
the `automation` identity; agents may run only its `--dry-run` (see
[Upstream Refresh](../reference/upstream-refresh-config.md#identity-and-credentials)).

`vrg-package` calls `gh` only: it lists and downloads releases, and
verifies their attestations, when it builds the package index.
`vrg-pr-review-comments` calls `gh` only too: it mints its token for the
`--repo` owner, so it never reads the `origin` remote.
`vrg-checkpoint` calls `gh` only as well: it mints its token for the
owner of the issue it names.

These tools call `git` only: `vrg-commit`, `vrg-container-cache`,
`vrg-container-run`, `vrg-reword`, `vrg-scorecard`, `vrg-validate`,
`vrg-version` and `vrg-vm`. `vrg-vm` also runs `git config` inside
the VM, to set its git identity and HTTPS GitHub access.

The other `vrg-*` tools call neither.

## Permission Configuration

### Project Settings (`.claude/settings.json`)

```json
{
  "permissions": {
    "allow": [
      "Bash(vrg-*)"
    ]
  },
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/guard.sh"
          }
        ]
      }
    ]
  }
}
```

The project layer allowlists all VRG tools via a single wildcard
and wires the Claude Code hook guard (`guard.sh`) as a `PreToolUse`
hook on every `Bash` tool invocation. The hook guard delegates to
`vrg-hook-guard`, which uses regex matching to block raw `git` and
`gh` commands while allowing `vrg-git`/`vrg-gh` wrappers through. For
an agent identity it also refuses raw `vrg-gh` reads of issue and PR
content and names `vrg-issue-read` in their place (see
[Raw content reads](../reference/hooks/pre-commit.md#raw-content-reads)).

This configuration applies to everyone who clones the repo — no
per-developer setup needed.

### Global User Settings (`~/.claude/settings.json`)

```json
{
  "permissions": {
    "defaultMode": "bypassPermissions"
  }
}
```

This is the operator's per-host setting. `vrg-vm` copies it into every
identity VM — Lima and off-platform alike — via `copy_claude_config`,
so the agent boots straight into bypass inside the sandbox. The mode is
independent of the deny rules: raw `git`/`gh` are blocked regardless,
because the hook guard hard-blocks them even under bypass.

### Local Settings (`.claude/settings.local.json`)

Gitignored, and holds nothing. In a managed repo, every copy of this
file that Claude Code may load must be absent or exactly `{}`. The file
outranks the project settings, so any rule in it would silently
override the managed template for that checkout. A rule you need
belongs in the repo template or in your user settings instead.

The agent chokepoints `vrg-commit`, `vrg-pr-workflow report-ready` and
`vrg-container-run` check this. An agent identity is refused; a human
gets the same message as a warning, and the tool proceeds. See
[Local settings](claude-config-ownership.md#local-settings) in the
Claude Config Ownership Model for which copies are checked, why the
check is not a Claude Code hook, and its known limit.

### What This Means in Practice

| Command | Result |
|---|---|
| `vrg-commit` | No prompt (project allow) |
| `vrg-git status` | No prompt (project allow) |
| `grep -r "foo" src/` | Runs (bypass) |
| `git push` | Blocked (hook guard) |
| `curl https://...` | Runs (bypass; contained to the sandbox VM) |
| `rm -rf .` | Runs (bypass; the disposable VM is the containment) |
| `vrg-gh pr merge` | No Claude Code prompt, but wrapper rejects it |

## Defense-in-Depth

### The Vergil Assumption

Every client-side enforcement layer in this model assumes the agent
is cooperative — that it is operating as Vergil, not Mimir. An
agent with file write access (which `bypassPermissions` grants) can
dismantle the entire client-side stack:

1. Delete or edit `.claude/settings.json` — Layer 1 gone
2. Run raw `git` and `gh` — Layer 2 bypassed (the wrappers are
   never invoked)
3. Remove plugin hook configuration — Layer 3 weakened
4. Delete `.claude/hooks/guard.sh` or remove the hooks
   wiring from `.claude/settings.json` — Layer 4 gone

After those four steps, the agent has unrestricted local access.
This is not a theoretical attack — it is trivial for any model
that decides to do it.

**Client-side layers are behavioral constraints, not security
boundaries.** They keep a well-intentioned agent on the rails.
They prevent mistakes, enforce consistency, and provide an audit
trail. They do not stop an adversary.

The two boundaries an agent **cannot** edit its way out of are not
client-side: the **VM sandbox** that contains it locally, and the
**server-side** GitHub App permission shape that bounds what reaches
GitHub.

### The VM Sandbox Boundary

Agents run inside a per-identity Vergil VM — never on the host. This is
what makes `bypassPermissions` acceptable: the agent has full rein
*inside* the VM, but the VM is the wall.

- **Containment.** The agent reaches only what the VM can reach. The
  host filesystem, other identities' VMs, and the operator's wider
  environment are outside the sandbox.
- **Disposability.** The VM is ephemeral and reproducible from its
  declared profile, so a bad outcome inside it (an `rm -rf`, a wedged
  toolchain) is recovered by a rebuild, not a forensic cleanup. On the
  off-platform backend, irreplaceable state lives on a separate
  persistent volume; injected credentials stay on the ephemeral boot
  disk and die with the VM.
- **Credential bounding.** Even with full local control, the agent can
  only reach GitHub through its injected App token — which the
  server-side boundary below constrains regardless of what happens in
  the sandbox.

The sandbox is why VERGIL runs `bypassPermissions` rather than chasing
a per-command allowlist: move the boundary to the edge of a disposable
VM, and the prompts inside it stop being the thing that protects you.

### The Actual Security Boundary

At the GitHub layer, the enforcement an agent cannot edit its way out
of is **server-side**: the GitHub App's installation permission shape
and branch protection rulesets.

- Each agent is a GitHub App whose installation token is bounded by
  its declared permission shape. The user App holds
  `pull_requests: write` and `workflows: write`
  ([epic vergil-project/.github#1384](https://github.com/vergil-project/.github/issues/1384)),
  so it can open its own PRs, turn their auto-merge on and push
  changes under `.github/workflows/`; it takes the PR path only through
  `vrg-submit-pr`, whose gate requires a merge-review record or a
  merge-review exemption the human approved. The audit App holds
  `contents: read`, so it cannot write code or merge (merging through
  the API requires `contents: write`). Every secret that can write or
  act lives in each repo's `automation` environment, limited to
  `develop` and `main`, so a workflow pushed on a feature branch runs
  without them.
- Branch protection rulesets require a pull request to change
  `develop` or `main`, with its required CI checks passing; only
  `main`'s ruleset also requires the branch to be up to date. They
  require no approving review. A development PR merges into `develop`
  through the auto-merge `vrg-submit-pr` turns on, whether the user
  agent or the human submitted it, and the org automation App merges the
  mechanized `automation/` PRs once CI passes (the repo-config sync and
  the upstream refresh). The "Main updates" ruleset lets only the human
  admin update `main`, so no agent can merge a release, and the daily
  agent audit checks the merges into `develop` and `main` (see
  [the rulesets](../reference/config-audit.md#github-checks-rulesets)).
  No client-side
  manipulation changes this.
- An App can only act on accounts it is installed on and repos that
  installation covers.

This and the VM sandbox are the real security model. Everything else
is convenience.

Where these boundaries meet other systems, their guarantees do not
always line up. Known gaps of that kind are recorded in
[Security Boundaries](security-boundaries.md).

### Why Client-Side Layers Still Matter

If server-side enforcement is the only real boundary, why bother
with four client-side layers?

- **Mistake prevention.** Most failures are not adversarial. An
  agent that accidentally runs `git reset --hard` on `develop` or
  on a reported-ready branch is not trying to subvert the tooling —
  it made a mistake. The wrappers catch this.
- **Consistency.** Routing all operations through `vrg-*` tools
  ensures commits have the right format, PRs have the right
  metadata, and branches follow the naming convention.
- **Audit trail.** Wrapper logging records what the agent did
  and when. Even if a layer is bypassed, the gap in the log is
  itself a signal.
- **Friction.** An agent that has to actively dismantle four
  layers is doing something visibly wrong. That visibility is
  the value — not the strength of any individual gate.

The [Mimir project](identity-architecture.md#the-mimir-convention)
exists specifically to test this boundary — to verify that
server-side controls hold when every client-side layer is
deliberately bypassed.

### Client-Side Layers

Four layers, each catching mistakes the layer above misses.
Redundancy is intentional — no single layer is trusted, and none
are sufficient against adversarial behavior.

**Layer 1 — Claude Code Permissions (outermost).** The
allowlist/deny configuration. Denied commands are blocked without
prompt. Unknown commands prompt the human.

**Layer 2 — VRG Wrappers.** `vrg-git` and `vrg-gh` validate
subcommands and flags before executing the underlying tool. Even
if `vrg-git push --force` passes the permission layer (it is
allowlisted as `vrg-*`), the wrapper rejects it.

**Layer 3 — Vergil Plugin Hooks.** The existing hook system
remains as a backstop. Hooks exit code 2 (hard block) overrides
even bypass mode. Hooks that become redundant with layers 1-2 are
retained — they catch regressions if a higher layer is
misconfigured.

**Layer 4 — Claude Code Hook Guard (`.claude/hooks/guard.sh`).**
The innermost layer. A `PreToolUse` hook wired in
`.claude/settings.json` that delegates to `vrg-hook-guard`,
blocking all raw `git` and `gh` commands via regex matching.
With layers 1-3 in place, agents cannot reach this layer
through normal operation for raw `git` and `gh`. It is also the
layer that refuses an agent's raw `vrg-gh` read of issue or PR
content (a body, comments or reviews), which layers 1 and 2 allow,
and names `vrg-issue-read`, the trusted read command, in its place.
That blocklist guards against accidents; the trust layer is what
keeps untrusted content from driving behaviour.

### Layer Interaction Matrix

| Operation | L1 Permissions | L2 Wrapper | L3 Plugin Hook | L4 Hook Guard | Server-Side |
|---|---|---|---|---|---|
| `vrg-commit` | allowed | n/a | n/a | allows | push accepted |
| `vrg-git push` | allowed | validates: no `--force` | n/a | n/a | push accepted |
| `git push` | denied | n/a | blocked | n/a | push accepted (if layers bypassed) |
| `vrg-gh pr merge` | allowed | rejected | blocked | n/a | into `develop`, merged once its required checks pass (if layers bypassed); into `main`, **refused** by the "Main updates" ruleset |
| `gh pr create` | denied | n/a | blocked | n/a | PR created (if layers bypassed) |
| `gh pr merge` (raw) | denied | n/a | blocked | n/a | into `develop`, merged once its required checks pass (if layers bypassed); into `main`, **refused** by the "Main updates" ruleset |
| `vrg-gh issue view <N> --comments` (agent) | allowed | allowed | n/a | blocked, naming `vrg-issue-read` | read allowed (if layers bypassed) |
| `rm -rf .` | prompts human | n/a | n/a | n/a | n/a |
| `vrg-git reset --hard` | allowed | rejected on a protected or frozen branch | n/a | n/a | n/a |

The rightmost column is the only one that holds against Mimir.

## History: the `acceptEdits` direction, and why it was dropped

The original plan phased *toward* a tight `acceptEdits` + allowlist
model — `acceptEdits` as the default mode, then progressively shrinking
the bash allowlist until `Bash(vrg *)` was the only allowed pattern and
everything else required human approval.

**That direction was not adopted.** In practice, prompting on every
non-allowlisted shell command made the agent unusable for the
infrastructure work VERGIL actually does, where running many varied
commands *is* the job. The allowlist could never keep up, and the
operator spent sessions approving commands instead of getting work
done.

VERGIL resolved this by changing *where* the boundary sits rather than
*how tight* the allowlist is: the agent runs in `bypassPermissions`
inside a disposable, sandboxed VM (see
[Base Permission Mode](#base-permission-mode) and
[The VM Sandbox Boundary](#the-vm-sandbox-boundary)). The `vrg-*`
wrappers, deny rules, and hooks remain — as consistency and
mistake-prevention guardrails, and as hard blocks on raw `git`/`gh`
that hold even under bypass — but they are no longer what makes
autonomy *safe*. The VM sandbox and the server-side App permissions
are.

The earlier `acceptEdits` design spec
(`docs/specs/2026-05-14-permission-model-design.md`) is retained as
historical context; it does not describe the model in use.

## Related

- [Identity Architecture](identity-architecture.md) — the
  accounts that permissions protect
- [Credential Management](credential-management.md) — how
  `vrg-gh` selects credentials per-operation
- [Account Setup](account-setup.md) — creating and configuring
  accounts
- [Security Boundaries](security-boundaries.md) — register of
  known cross-system seams and their mitigations
- [Claude Config Ownership](claude-config-ownership.md) — which
  settings or instruction file owns each Claude Code setting
- [Permission model design spec][perm-spec] — full decision
  rationale and alternatives considered

[perm-spec]: https://github.com/vergil-project/vergil-tooling/blob/develop/docs/specs/2026-05-14-permission-model-design.md
