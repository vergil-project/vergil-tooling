# Upstream Refresh Reference (`[upstream-refresh]`)

`vrg-upstream-refresh` keeps a repo's upstream inputs current. The repo
supplies the logic in a hook, and the command supplies the mechanics: it
runs the hook, checks the change against the repo's path fence, and
opens a PR that merges itself once CI passes. No human merges a refresh
PR.

The command was introduced by epic
[vergil-project/.github#472](https://github.com/vergil-project/.github/issues/472)
(spec `epics/472-multi-series-runtime/spec.md`, section 5.6) and rebuilt
on the mechanized-PR building block by epic
[vergil-project/.github#1168](https://github.com/vergil-project/.github/issues/1168)
(spec `epics/1168-upstream-refresh-merge/spec.md`, section 6). The
unattended merge carries out the policy decided in
[vergil-project/.github#470](https://github.com/vergil-project/.github/issues/470).

## How it differs from a dependency update

An upstream refresh and a [dependency update](dependency-update-config.md)
are separate pipelines with separate hooks:

| | Upstream refresh | Dependency update |
|---|---|---|
| Command | `vrg-upstream-refresh` | `vrg-update-deps`, and the `update-deps` stage of `vrg-release` |
| Config | `[upstream-refresh].hook` and `.paths` | `[dependency-update].hook` |
| What changes | Inputs the repo pins from upstream, such as a runtime build or a tool version | Dependencies within their declared bounds |
| Branch | `automation/upstream-refresh/<run-id>`, a new one per run | `deps/update-<version>` |
| Validation | The path fence before the push; the PR's own CI decides the merge | One `vrg-validate` run before the PR |
| Merge | Merged by the job, as the org automation App, once CI passes | Merged when green |
| Who runs it | A scheduled ops workflow, or a human | A human |

Neither command runs the other's hook. `vrg-upstream-refresh` never runs
dependency updaters, validation or a release.

## Which mechanism do I use?

Ask one question of the input: does the repo's code depend on it, or
does the repo deliver it?

- **The code depends on it: a dependency update.** Libraries, lockfiles
  and the vergil line are what the repo is built on. They move after
  each release, at the start of the next development cycle, so a whole
  cycle runs against them before the next release ships them.
- **The repo delivers it: an upstream refresh.** Upstream content that
  the repo packages and publishes is refreshed before a release, and the
  release publishes it.

The current users of an upstream refresh, each from a scheduled
`upstream-refresh` job in its `ops.yml`:

- **vergil-python** refreshes each series listed in `runtime.toml` to
  its newest CPython patch and python-build-standalone build
  (`scripts/dev/upstream-refresh.sh`).
- **vergil-containers** moves its binary-download tools to their newest
  releases and its language toolchain pins (`docker/pins/toolchains.yml`:
  Python builds from python-build-standalone, Go, Node, Rust and
  rustup-init) to the newest patch of each series
  (`docker/pins/upstream-refresh.sh`).

The current users of a dependency update are listed on the
[dependency update page](dependency-update-config.md#which-mechanism-do-i-use).

An upstream refresh PR merges unattended, as the org automation App,
from the scheduled job. A dependency update merges its own PR once it is
green, but only from a `vrg-release` or `vrg-update-deps` that a human
runs.

## `[upstream-refresh]`

```toml
[upstream-refresh]
hook = "scripts/dev/upstream-refresh.sh"
paths = [
  "docker/pins/toolchains.yml",
  { path = "docker/common/*", lines = '^ARG [A-Z0-9_]+_(VERSION=(?P<version>\d+(\.\d+)*)|SHA256_(AMD64|ARM64)=[0-9a-f]{64})$' },
]
```

| Key | Type | Default | Description |
|---|---|---|---|
| `hook` | string | *(required)* | Command that refreshes the repo's upstream inputs. Must be non-empty |
| `paths` | array | `[]` | The path fence's allowlist: the files the hook may change. Each entry is a glob string, or a `{ path, lines }` table: a glob and a line regex. An empty or absent list refuses every patch |

The table is optional, but `vrg-upstream-refresh` refuses to run without
it (`vergil.toml on <repo>'s develop has no [upstream-refresh] section`).
When the table is present, `hook` is required, and any other key than
`hook` and `paths` is an error. A `paths` entry's table takes only
`path` (required) and `lines`; `lines` must compile as a Python regex.

A glob is repo-relative (no leading `/`, and no empty, `.` or `..`
segment). `*` matches within one path segment, and `**` matches any
number of segments. A path's first matching entry decides which line
rule applies to it.

## The path fence

No human reviews a refresh PR before it merges, so the patch the hook
produced is treated as untrusted data. Publish checks every path it
touches before anything is pushed, and a refused patch fails the run,
naming each path or table and the rule it broke.

**Inputs come from publish's own clone.** The touched set is computed
from publish's clone after the patch is applied: added, modified,
deleted, mode-changed and untracked files. It must equal the list of
changed paths prepare recorded, exactly. That list is otherwise used
only for the PR body. The rules are read from the `develop` tip of
publish's clone, never from prepare's output. A rules change therefore
takes effect only once it has merged; `--dry-run --rules-from` tests one
before then (see [CLI](#cli)).

**The rules:**

- **The allowlist.** A path outside every `paths` entry is refused. With
  no `paths`, every path is refused.
- **`.github/**` is always refused**, whatever `paths` allows, even
  `**`. PR CI is no gate against a change to the workflows that CI
  itself runs.
- **Symlinks and submodules** are always refused.
- **A line rule.** Under an entry with `lines`, every removed and added
  line must match the regex as a whole line (`re.fullmatch`). An added,
  deleted, binary or mode-changed file is refused under a line rule.
- **`vergil.toml`**, where `paths` allows it, has its own rules (below).

An allowlist names the data files the hook writes, as exact paths where
the set is fixed. It never covers the hook, its resolvers or
generators, or files the hook does not change. Where a file's content
follows a format, a line rule pins it.

### A refresh never lowers a pinned version

A line rule marks each version in its regex with a named group:

- `(?P<version>…)` for the first one;
- `(?P<version_<suffix>>…)` for each further one in the same regex,
  such as `(?P<version_pbs>…)`. Python's `re` refuses two groups of the
  same name, so a rule that pins two keys' versions, such as `cpython`
  and `pbs_tag`, names the second `version_<suffix>`.

A file under a versioned rule is compared as a whole, before and after
the patch, so publish takes its diff with the whole file as context. A
versioned line's **identity** is its block's anchor value (none under a
rule with no anchor group) and the text before its version group.
Versions are compared per identity, wherever the line stands in the
file, as tuples of integers split on `.` (trailing zeros dropped). A
version that goes down is refused, and so is one that is not numeric.

**Blocks and anchors.** A file that repeats a key in several blocks,
such as one `cpython` per `[[series]]` in vergil-python's
`runtime.toml`, marks the line that names each block with an optional
`(?P<anchor>…)` group, such as the block's `series` value. Each
versioned line belongs to the block named by the most recent anchor
line above it. Under a rule with no anchor group, the whole file is one
block. For example, a `runtime.toml` rule:

```toml
{ path = "runtime.toml", lines = '^(\[\[series\]\]|\[series\.sha256\]|series = "(?P<anchor>\d+\.\d+)"|cpython = "(?P<version>\d+\.\d+\.\d+)"|pbs_tag = "(?P<version_pbs>\d{8})"|x86_64 = "[0-9a-f]{64}"|aarch64 = "[0-9a-f]{64}"|)$' }
```

**What is refused, and what is allowed:**

- **Ambiguity is refused.** An identity with more than one versioned
  line on either side, where the versions change, cannot be paired: a
  key that repeats in a file whose rule has no anchor. Under a rule
  with an anchor, a versioned line that appears or disappears inside a
  block that stays, or above the first anchor line, is refused too.
- **New blocks and new pins are allowed.** A whole new anchored block
  (a new `[[series]]`) is allowed, as is a new or a dropped versioned
  line under a rule with no anchor group.
- **Removals are a human change.** An automated refresh never removes
  an existing anchored block. Dropping a series is made by a human, in
  an ordinary PR.

### A hash changes only with its block's version

A line rule may mark a hash or digest in its regex with a named group:

- `(?P<hash>…)` for the first one;
- `(?P<hash_<suffix>>…)` for each further one in the same regex, such
  as `(?P<hash_arm>…)`, for the same reason as `version_<suffix>`.

A **hash line** is a line one of these groups marks. Its identity is
its block's anchor value and the text before its hash group, as for a
versioned line, and its block is the one that anchor names (under a
rule with no anchor group, the whole file). The file is compared as a
whole, so a diff that does not show the whole file is refused.

- **A changed hash needs a version change.** A hash line whose hash
  changes is allowed only when a version of the same block also changes
  in the same patch. A hash that changes while every version of its
  block stays the same is refused, naming the path and the line. Under
  a rule with no version group, no existing hash may change.
- **A hash pinned for the first time is allowed:** a hash line in a new
  block, or a new key in an existing block.
- **A hash line that disappears from a block that stays is refused**
  under a rule with an anchor group, since moving it above its block's
  anchor line would otherwise give it a new identity. Under a rule with
  no anchor group, a dropped hash line is a dropped pin, and is allowed.

A rule with no hash group checks no hashes. For example, the
`runtime.toml` rule above, with its hashes marked:

```toml
{ path = "runtime.toml", lines = '^(\[\[series\]\]|\[series\.sha256\]|series = "(?P<anchor>\d+\.\d+)"|cpython = "(?P<version>\d+\.\d+\.\d+)"|pbs_tag = "(?P<version_pbs>\d{8})"|(x86_64|aarch64) = "(?P<hash>[0-9a-f]{64})"|)$' }
```

### `vergil.toml`

Where `paths` allows `vergil.toml`, publish parses it before (at the
recorded commit) and after the patch:

- outside `[[package]]`, nothing may change. Above all, that covers
  `[dependencies]` (the tooling pin) and `[upstream-refresh]` (the hook
  and this allowlist);
- in an existing `[[package]]` entry, matched by `name`, only
  `upstream-version` and `summary` may change. `upstream-version` must
  keep its `<cpython>+<pbs_tag>` format and must not go down, compared
  on its `<cpython>` part, then its `<pbs_tag>`;
- an existing `[[package]]` entry is never removed: dropping a series is
  a human change, as for an anchored block;
- a new entry is allowed only as a series copy of an existing one: the
  names differ by one `\d+\.\d+` series token, and every other value,
  including `smoke` and `staged`, is the existing entry's with that
  token replaced.

The file may be edited but never added or deleted by a refresh.

### What the fence relies on

The fence limits which files change. For an allowlisted data file that
workflows read, what goes into it is limited by the repo's PR CI checks,
and each repo names them. In vergil-containers, `generate.py --check`
requires `docker/versions/matrix.json` to equal what is generated from
`manifest.yml` (which the fence never allows to change) and
`docker/pins/toolchains.yml`, and `check_pins.py` checks the pins. Those
checks must be required status checks on `develop`. Where a data file's
format allows it, a value-format line rule bounds its content as well.

## The hook contract

Prepare runs the hook as `bash -c "<hook>"` from the root of a fresh
clone of `develop`, taken in a temporary directory. The clone holds
`develop`'s tracked files only, as a fresh CI checkout does: files that
are ignored or untracked in your checkout are not there, so the hook
must not rely on them. The hook runs with no GitHub token and no App
credential: `GH_TOKEN`, `GITHUB_TOKEN`, every `VRG_AUTOMATION_*`
variable, `VRG_APP_ID`, `VRG_PRIVATE_KEY_PATH`, Actions' own tokens and
git's environment config are removed from its environment. The hook:

- **Edits tracked files only.** It must not leave new untracked files.
  If it does, the command fails and names them
  (`hook left untracked files: <paths>`), and nothing is published.
  Files the repo's `.gitignore` ignores are allowed and never published.
- **May write notes** to the file named in `$VRG_UPSTREAM_REPORT`, one
  note per line. Each non-empty line becomes a bullet under `## Notes`
  in the PR body. The format of each line is the repo's choice.
- **Fails with a non-zero exit.** The command then stops with
  `[upstream-refresh].hook failed (exit <n>): <hook>`, and nothing is
  published.

## What the command does

The command runs in two halves, split by the **key boundary**: the
automation App's private key never shares a runner with the hook, or
with anything the hook installs or loads.

**prepare** (`vrg-upstream-refresh prepare --out DIR`) clones `develop`,
runs the hook there, and writes to `DIR`, which must not already hold
prepare's output:

- `changes.patch`: the hook's tracked changes as a binary patch against
  the clone's `develop` commit (absent when nothing changed);
- `meta.json`: the repo, that commit's SHA, the changed paths and the
  hook's notes.

It holds no App credentials and needs no write token. It refuses to run
when the App's credentials are in its environment.

**publish** (`vrg-upstream-refresh publish --from DIR`) reads that
output as data. It never runs the hook, and never runs git in the tree
prepare worked in. In order, it:

1. takes the App's credentials, when given, from its environment and
   removes them, so no child process starts with them;
2. installs SIGINT and SIGTERM handlers, then mints its first
   repo-scoped token and checks the identity (see
   [Identity and credentials](#identity-and-credentials));
3. clones `develop`, and reads the fence rules from that tip;
4. checks that the recorded commit is still on `develop` (GitHub's
   compare API says `identical` or `ahead`), fetches it, checks it out
   and applies the patch with `git apply --index`. A recorded commit
   that is no longer on `develop`, or a patch that does not apply
   cleanly, is refused;
5. computes the touched set from the clone and runs the
   [path fence](#the-path-fence);
6. commits as the App's bot user, `<app-slug>[bot]` with its
   `<id>+<app-slug>[bot]@users.noreply.github.com` address. A human's
   run commits as the human;
7. hands the clone to the mechanized-PR building block, which closes an
   earlier run's open PR and deletes its branch, pushes the new branch,
   opens the PR with squash auto-merge, and waits until it merges. When
   GitHub refuses auto-merge because the PR can already merge, it
   merges the PR directly, with the same method and pinned to the
   pushed commit, and does not wait.

The PR's commit is parented on the recorded commit, so the patch lands
on exactly the tree the hook ran against. `develop` does not require a
branch to be up to date, so the PR merges from that base once its own
checks pass, even if `develop` has moved since. The wait merges the PR
itself once its checks are green, if auto-merge has not merged it
first; a failed check, a conflict, a closed PR, the branch-update cap
(see the permissions below) or the deadline ends it unmerged. Under a token source the deadline is
7200 seconds, counted from publish's own start: the clone and the patch
come out of it, and the wait gets what is left. Closing an earlier
run's PR, the push and opening the PR come after that is worked out,
so they are not counted; they take seconds, well inside the publish
job's 140-minute timeout. A human's run keeps the building block's
own deadline, `$VRG_MECHANIZED_PR_DEADLINE` seconds (2400 by default).

The commit and the PR title are `chore(upstream): refresh upstream
inputs`. The PR body says that the job merges it once CI passes, and
lists the changed files and the hook's notes.

Every run ends in one of these outcomes:

| Outcome | When | Exit |
|---|---|---|
| In sync | No change. An earlier run's open refresh PR is still closed and its branch deleted | 0 |
| Merged | A change: the PR was opened and merged within the run | 0 |
| Not merged | The PR was opened but did not merge; the PR and the reason are named | 2 |
| Refused or failed | The hook, the clone, the path fence, the base check, a refused identity, a failed mint or revocation, or a usage error; nothing is pushed unless the failure came after the push | 1 |

**The combined mode** (no mode named) runs prepare and publish in one
process, for a human's local run under the human's own credentials. The
key boundary does not apply, since no App key is involved, and no token
source is registered. It is refused under GitHub Actions.

**`--dry-run`** runs prepare, applies the patch in a second clone
exactly as publish does, and runs the path fence. It prints the PR body
and every violation, pushes nothing and opens nothing. It exits 1 if
any violation is found.

The command never reads or touches the caller's checkout. Every clone
lives in a temporary directory, removed when the run ends.

## CLI

```text
vrg-upstream-refresh prepare --out DIR [--repo OWNER/NAME]
vrg-upstream-refresh publish --from DIR [--repo OWNER/NAME]
vrg-upstream-refresh [--repo OWNER/NAME] [--dry-run [--rules-from PATH]]
```

| Option | Default | Description |
|---|---|---|
| `prepare --out DIR` | — | Run the hook with no credentials; write the patch and `meta.json` to `DIR` |
| `publish --from DIR` | — | Publish prepare's output from `DIR`: apply, fence, open the PR and wait until it merges |
| `--repo` | `$GITHUB_REPOSITORY`, then the `origin` remote | Repository to refresh. Publish refuses output prepared for another repo |
| `--dry-run` | off | Run prepare and the path fence; print the PR body and every violation; push nothing |
| `--rules-from PATH` | the `develop` tip | With `--dry-run` only: read the fence rules from this `vergil.toml`, so a branch can test its new `paths` before they merge |

Exit codes, also given by `--help`:

- **0:** merged, or nothing to change (a dry run: no fence violation);
- **1:** any other failure or refusal, with the reason, including a
  usage error such as the removed `--base` (argparse's own exit 2 is
  mapped to 1);
- **2:** the PR was opened but did not merge, naming the PR and the
  reason (a terminal outcome or the deadline);
- **130 or 143:** publish was interrupted by SIGINT or SIGTERM, after
  revoking its token.

The fixed `upstream-refresh` branch, the temporary linked worktree, the
clean-checkout check and `--base` are gone: the base is always
`develop`, as with every mechanized PR.

## Identity and credentials

The command accepts the `human` and `automation` identity modes. The
agent modes (`user` and `audit`) are refused with
`vrg-upstream-refresh opens PRs and is not run by agents`, except for
`--dry-run`, which mints no token, pushes nothing and opens nothing, so
an agent can check a repo's allowlist before it merges.

Under GitHub Actions (`GITHUB_ACTIONS` set):

- **publish** refuses to run, before touching GitHub, unless it was given
  the App's credentials and resolves to `automation`;
- **prepare** runs without credentials, and refuses if they are present;
- **the combined mode** is refused.

Publish's credentials are three environment variables, set only on the
step that runs it:

| Variable | Holds |
|---|---|
| `VRG_AUTOMATION_APP` | The automation App's slug (from the org-level Actions variable of the same name) |
| `VRG_AUTOMATION_CLIENT_ID` | The App's client ID |
| `VRG_AUTOMATION_PRIVATE_KEY` | The App's PEM private key |

The key and every token are never in argv, a log line, an exception
message or a file. Publish refuses to run when the agent App mode is
configured (`VRG_APP_ID` or `VRG_PRIVATE_KEY_PATH` set), so the two
minting paths never mix.

With the credentials, publish mints installation tokens limited to the
one target repo, with `contents: write`, `pull_requests: write`,
`workflows: write` and `metadata: read`. Workflows write is needed only
if the wait has to update the PR's branch, which merges `develop` into
it: GitHub refuses that merge from an App token without it whenever
`develop` has changed a workflow file. The `CI gates (develop)` ruleset
does not require a branch to be up to date, so the wait updates a branch
only on a repo whose `develop` still requires it, such as one whose
classic protection has not yet been brought in line by
`vrg-github-repo-config update`. The path fence keeps every
patch out of `.github/**`. Each token is checked against
`<VRG_AUTOMATION_APP>[bot]` before use. The token is renewed once it is
older than 1800 seconds, and the replaced token is revoked at once.
Every `gh` and `git` call takes the current token, so a wait longer
than one token's hour keeps working. When publish exits, on success,
on failure or on SIGINT or SIGTERM, it revokes the current token. A
revocation that fails fails the run.

## Logging

Every run prints enough to tell what happened without opening GitHub:

- the earlier run's PR it closed, if any;
- the PR it opened, or that it is in sync;
- each token mint, renewal and revocation, with the time (never the
  token);
- how the wait ended, with the PR URL and the time waited.

## The scheduled workflow

The reusable vergil-actions workflow `ops-upstream-refresh.yml` runs the
command as two jobs, each on a fresh runner:

- **prepare** holds no secrets and a read-only `GITHUB_TOKEN`, runs
  `vrg-upstream-refresh prepare`, and uploads its output as an artifact;
- **publish** downloads that artifact and runs
  `vrg-upstream-refresh publish` with `VRG_AUTOMATION_APP` and the App's
  client ID and key set on that step only.

Each consuming repo calls it from its own `ops.yml` on its own schedule,
and declares its allowlist in `[upstream-refresh].paths`. A failed or
unmerged run shows as a red scheduled run.
