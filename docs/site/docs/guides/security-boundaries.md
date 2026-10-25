# Security Boundaries

VERGIL lets powerful AI agents do real work by managing risk, not by
pretending it away. The riskiest places are usually not inside one
component. They are at the **seams** between systems (Vergil, GitHub,
the agent VM and the agent harness), where one system's guarantees do
not quite line up with the next system's.

This page is the register of those seams. Each entry records what the
seam is, what can go wrong, what limits the risk today, and the fix we
would want. The entries also serve as comparison criteria when looking
at alternatives to GitHub or to the current harness: does the
alternative have the same gap, or does it close it?

For the enforcement model these seams sit within, see
[Permission Model](permission-model.md). For the identities involved,
see [Identity Architecture](identity-architecture.md).

## Adding an entry

Add an entry whenever a new cross-system seam is found. Give each entry
the same fields:

- **Seam:** the two systems whose guarantees do not line up.
- **Risk:** what an agent, or code acting through it, could do as a
  result.
- **Current mitigation:** what limits the risk today. Record only
  mitigations that are actually in place.
- **Desired fix:** the change that would close the seam.
- **Status:** `open` or `resolved`. Keep resolved entries, and note
  what resolved them.
- **Tracking:** the issue or PR where the fix is followed.

This page is ordinary documentation. Update it through a normal PR
when a seam opens, changes or closes. It is not tracked as an open
issue.

## Register

### 1. GitHub permissions are coarser than the agent model

GitHub's permission model cannot express exactly what an agent
identity may and may not do. Granting one permission can bring a
companion capability we did not want to grant, and there is no
finer-grained control over agent capabilities at the level we would
design for.

- **Seam:** Vergil's least-privilege agent model and the permission
  shapes GitHub Apps can declare.
- **Risk:** an identity can do more than its role should allow.
- **Current mitigation:** each role is its own GitHub App
  (`<username>-vergil-user`, `<username>-vergil-audit`) with
  [inverted permission shapes](identity-architecture.md#inverted-permission-shapes),
  and App installation scope is kept minimal. The `vrg-git` and
  `vrg-gh` wrappers narrow agent behaviour further, but they are
  client-side guardrails, not a boundary.
- **Desired fix:** finer-grained GitHub App permissions. This needs an
  upstream change from GitHub that may never come.
- **Comparison criterion:** does an alternative offer the granularity
  GitHub lacks?
- **Status:** open.
- **Tracking:** no open issue; this entry is the record. It was first
  logged in
  [vergil-tooling#1369](https://github.com/vergil-project/vergil-tooling/issues/1369).

### 2. Repo-supplied provisioning ran as root in a credentialed VM

Per-repo VM profiles
([vergil-vm#99](https://github.com/vergil-project/vergil-vm/issues/99))
added a `provision` hook: a consuming repo could ship a shell script
that the VM ran as root at provision time. The same VM later holds the
identity's GitHub App private key. A hostile or compromised script, or
a dependency it pulled, could read and exfiltrate that key. Host
isolation protects the laptop; it does not protect a credential inside
the VM.

- **Seam:** the VM's host-isolation guarantee and the trust level of
  repo-supplied code running inside it, next to a live credential.
- **Risk:** credential exfiltration, or a persistent backdoor planted
  at provision time.
- **Mitigation before the fix:** a stated trust boundary (build a
  dedicated VM only for a repo you trust to run code as root), and the
  hook's content was part of the VM spec fingerprint, so any change to
  it forced a rebuild and a review checkpoint.
- **Fix:** the `provision` hook was removed and replaced with
  declarative `apt_repos`, `packages` and `vagrant_plugins` keys in the
  repo's `vergil.toml` `[vm]` tables. The reviewed, versioned
  `vergil-vm` template owns how they are installed, so no
  repo-supplied code runs as root. These fields feed the VM spec
  fingerprint directly.
- **Status:** resolved.
- **Tracking:**
  [vergil-vm#105](https://github.com/vergil-project/vergil-vm/issues/105)
  (fixed by
  [vergil-vm#106](https://github.com/vergil-project/vergil-vm/pull/106)).

### 3. A misfiring commit guard pushes agents to disable the sandbox

The vergil plugin's protected-branch hook
(`block-protected-branch-work.sh`) decides which repo a command
targets from the Bash tool's starting directory. The harness resets
that directory to the session root before every call. The hook
re-targets only a leading `cd <dir>` or `git -C`; any other command
shape is judged against the session-root repo. A commit in a feature
worktree is then denied as a commit to `develop`. Agents have worked
around the false deny by running the command with the sandbox
disabled, which removes the whole sandbox boundary for that call.

- **Seam:** the harness's pre-execution hook, which sees the reset
  starting directory, and the command, which runs after its own `cd`.
- **Risk:** agents learn to treat a safety guard as noise and switch
  off the sandbox to get past it. The same move (disable the sandbox,
  `cd` into a branch) would let an agent really commit to a protected
  branch.
- **Current mitigation:** `vrg-commit` checks the real current branch
  when it runs and refuses commits to protected branches, so the
  bypass does not let a `vrg-commit` land on `develop` or `main`.
  `vrg-git` blocks branch switches in the main worktree. Neither stops
  the sandbox bypass itself.
- **Desired fix (decided):** have the plugin hook skip `vrg-commit`,
  whose run-time branch check is the authoritative one, rather than
  try to parse every shell command shape. Other git write paths stay
  guarded as they are. Related latent assumption: `vrg-hook-guard`
  also trusts `tool_input.cwd`. That is harmless today, since it only
  blocks raw `git`/`gh`, but matters if a fix comes to rely on it.
- **Status:** open.
- **Tracking:**
  [vergil-claude-plugin#694](https://github.com/vergil-project/vergil-claude-plugin/issues/694).

## Related

- [Permission Model](permission-model.md): the enforcement layers and
  the boundaries agents cannot edit their way out of
- [Identity Architecture](identity-architecture.md): the agent
  identities and their permission shapes
- [Credential Management](credential-management.md): how credentials
  are minted and selected
