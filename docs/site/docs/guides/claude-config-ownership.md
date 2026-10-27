# Claude Config Ownership Model

This page says which file owns each Claude Code setting and instruction in
a Vergil-managed repository. It is **the ownership model**: the reference
that the config audit, the `steering-audit` skill and the per-repo steering
sweeps check against.

It is the target state of epic
[vergil-project/.github#558](https://github.com/vergil-project/.github/issues/558).
Until that epic's cleanup and audit changes land, some managed repos still
differ from it. For example, the repo settings files still declare
`extraKnownMarketplaces`.

**Platform facts verified:** 2026-10-27, against these Claude Code pages:

- [Settings files and precedence](https://code.claude.com/docs/en/settings.md)
- [Settings reference](https://code.claude.com/docs/en/settings-reference.md)
- [Permissions](https://code.claude.com/docs/en/permissions.md)
- [Plugin loading](https://code.claude.com/docs/en/plugins/loading.md)
- [Hooks reference](https://code.claude.com/docs/en/hooks.md) and
  [hooks guide](https://code.claude.com/docs/en/hooks-guide.md)
- [Memory and CLAUDE.md](https://code.claude.com/docs/en/memory.md)
- [Skills](https://code.claude.com/docs/en/skills.md) and
  [commands](https://code.claude.com/docs/en/commands.md)
- the Claude Code
  [`CHANGELOG.md`](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md)
  (latest entry at verification: 2.1.311)

Re-verify the facts, and update the date, whenever this page changes and
on every [recurring review](#recurring-review).

## Placement rule

A setting lives in the change-controlled repo file unless the platform
forces it into user scope. The repo file is reviewed, versioned and audited
nightly. The user file is none of those, so it stays minimal.

- **Forced into user scope:** plugin *installation* and the marketplace
  definitions. A plugin enabled only by the project file is never fetched
  ([fact 5](#platform-facts)), and a second marketplace definition replaces
  the first whole ([fact 6](#platform-facts)).
- **Fleet policy, so it stays in the repo:**
  - the guard `PreToolUse` hook;
  - `attribution` off;
  - `permissions.allow: Bash(vrg-*)`;
  - the enable for the vergil plugin. That plugin's own hooks enforce
    workflow policy, and a user-level `false` cannot turn off a project
    `true` ([fact 5](#platform-facts)).

  In the repo file these apply to every checkout on any machine, including
  host sessions and fresh clones without the VM's copy of the user file.
  Fleet policy must not depend on the personal file.
- **No repo-local overrides.** `.claude/settings.local.json` outranks the
  repo file. It stays gitignored, and in a managed repo the copy Claude Code
  loads must be absent or exactly `{}` (see
  [Local settings](#local-settings)).

## Ownership table

| File | Owns | Must not hold |
|---|---|---|
| `~/.claude/settings.json` (host, copied verbatim to VMs) | plugin enablement (which installs them) and marketplaces for **all** plugins; the agent-VM permission mode; personal model and UI settings | anything that works on only one OS: the same file runs on the macOS host and on Linux VMs |
| repo `.claude/settings.json` (checked in, generated from the template, audited) | the guard `PreToolUse` hook; `permissions.allow: Bash(vrg-*)`; `attribution` off; `enabledPlugins: {"vergil@vergil-marketplace": true}` | `extraKnownMarketplaces` (a second definition replaces the user's whole entry); any repo-specific key |
| `.claude/settings.local.json` (gitignored; checked by the agent chokepoints) | nothing: absent or exactly `{}` | any key |
| managed template `data/claude_settings.json` | the only source for the repo file's content | — |
| managed settings | unused, deliberately (see [Unused scopes](#unused-scopes)) | — |
| `~/.claude/CLAUDE.md` (host, human-owned, minimal) | only preferences that are genuinely personal and have no change-controlled home | engineering or workflow policy; anything a repo template, the plugin or repo docs can hold |
| template region of repo `CLAUDE.md` (`data/claude_md_consumer.md`) | fleet-wide agent policy: worktrees, `vrg-git`/`vrg-gh`, the validation command, the PR handoff and freeze, the memory rule (condensed), the engineering principles, the "`settings.local.json` stays absent or `{}`" rule, "report-ready text is public: no secrets", the `paad-policy.md` pointer | reference material; anything a hook enforces mechanically, beyond a one-line pointer |
| rest of repo `CLAUDE.md` | what an agent needs to work in that repo: overview, commands, constraints | catalogues and architecture detail (these go in site docs, linked); restated fleet policy |
| `.claude/rules/*.md` (path-scoped) | instructions that matter only for part of a repo | — |
| `AGENTS.md` | nothing: removed | — |

The user file carries the agent-VM permission mode
(`bypassPermissions`; see [Permission Model](permission-model.md)).
Because the host and the VMs share that file, every Claude Code session
started on the host uses that mode too. By design, the only such sessions
are VM sessions the human moves onto the host for supervised admin or
debugging work. That is a deliberate, accepted exception, and the recurring
review treats it as correct.

## Platform facts

Each fact below is what the cited page said on 2026-10-27.

1. **Precedence.** Highest first: managed, command line, project local,
   shared project, user. A key set at a higher level overrides the same key
   set lower down. List keys such as `permissions.allow` combine instead of
   overriding.
   ([settings.md, "Settings precedence" and "Lists merge instead of overriding"](https://code.claude.com/docs/en/settings.md#settings-precedence))
2. **Denies win.** "If a tool is denied at any level, no other level can
   allow it."
   ([permissions.md](https://code.claude.com/docs/en/permissions.md#settings-precedence))
3. **Hooks merge** across user, project, local and plugin sources. In an
   interactive session Claude Code holds back hooks from every settings
   file, including `~/.claude/settings.json`, until the workspace trust
   dialog is accepted. A `-p` or SDK session treats the folder as trusted.
   ([hooks.md, "Workspace trust"](https://code.claude.com/docs/en/hooks.md#workspace-trust))
4. **Folder trust.** Project `permissions.allow` rules,
   `extraKnownMarketplaces` and most `env` values "apply only after each
   teammate trusts the folder". `deny` and `ask` rules apply right away.
   ([settings.md, troubleshooting](https://code.claude.com/docs/en/settings.md#common-cases))
5. **`enabledPlugins`** merges per plugin id: the value from the
   highest-precedence source that mentions the id applies. The project file
   beats the user file, so a user-level `false` cannot disable a plugin the
   project enables, but a `false` in `.claude/settings.local.json` can.
   ([plugins/loading.md, "Find where a plugin is enabled"](https://code.claude.com/docs/en/plugins/loading.md#find-where-a-plugin-is-enabled))
   - A plugin with an external source whose only `true` is in the project
     file is not fetched. A fetch needs a `true` from user settings, an
     untracked `.claude/settings.local.json`, `--settings`, or managed
     settings.
   - That unfetched plugin is reported only in the `/plugin` **Errors**
     tab, which an agent session never opens.
   - Claude Code writes `.claude/settings.local.json` itself, on a standing
     approval ("Yes, and don't ask again") and for some `/config` options
     such as **Show tips**, and adds it to the global git excludes. In a
     worktree it reads and writes the copy at the **main checkout's root**,
     not the worktree's.
     ([settings.md](https://code.claude.com/docs/en/settings.md))
   - After session start Claude Code may still clone a declared
     marketplace and download enabled plugins, so what is loaded at session
     start can differ from what is on disk a minute later.
     ([plugins/loading.md, "Check which stage a plugin reached"](https://code.claude.com/docs/en/plugins/loading.md#check-which-stage-a-plugin-reached))
6. **`extraKnownMarketplaces`.** When several files define a marketplace
   under the same name, Claude Code "uses the entry from the
   highest-precedence file whole" and it "inherits none of its fields"
   (since v2.1.228). If the project and user files both define
   `vergil-marketplace`, the project entry wins as a whole, so a project
   entry without `ref` silently drops the user's `ref`.
   ([settings-reference.md, `extraKnownMarketplaces`](https://code.claude.com/docs/en/settings-reference.md#extraknownmarketplaces))
7. **`attribution`.** Empty strings for `commit` and `pr` hide attribution
   ("Set it to an empty string to hide commit attribution"). The
   `attribution: false` form needs v2.1.281 or later, and older versions
   skip the whole settings file that holds it, so shared files keep the
   object form. `sessionUrl` affects only cloud and Remote Control commits
   and pull requests.
   ([settings-reference.md, `attribution`](https://code.claude.com/docs/en/settings-reference.md#attribution))
8. **Changelog 2.1.195** reads: "Fixed external plugins enabled only by
   project `.claude/settings.json` not requiring explicit install consent
   on every loader path". It does not say that user-enabled plugins stopped
   installing on their own. Whether a fresh agent VM installs them without
   `vm_guest`'s reconcile step is still to be verified
   ([vergil-project/.github#578](https://github.com/vergil-project/.github/issues/578)),
   so that step stays.
   ([CHANGELOG.md](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md))
9. **`CLAUDE.md`.**
   ([memory.md](https://code.claude.com/docs/en/memory.md))
   - Target under 200 lines per file.
   - `@path` imports (at most four hops) help organize a file but don't
     reduce its context cost.
   - Path-scoped `.claude/rules/` files load only when Claude works with
     matching files.
   - By default `AGENTS.md` is read only when no `CLAUDE.md`,
     `.claude/CLAUDE.md` or `CLAUDE.local.md` exists in the working
     directory or above it (v2.1.277+), or when a `CLAUDE.md` imports it.
10. **`/doctor` and the instruction-file audit.**
    - `/doctor` (alias `/checkup`) is a bundled skill that a user types.
      ([commands.md](https://code.claude.com/docs/en/commands.md),
      [skills.md, "Bundled skills"](https://code.claude.com/docs/en/skills.md#bundled-skills))
    - The instruction-file audit runs through the bundled `claude-api`
      skill's `prompt-audit` subcommand (v2.1.221+); `/doctor prompt-audit`
      needs v2.1.283+. It checks instruction files for stale paths and
      commands, contradictions, and patterns written for older models.
      ([memory.md, "Audit your instruction files"](https://code.claude.com/docs/en/memory.md#audit-your-instruction-files))
11. **Settings schema errors.** A user, project or local file with invalid
    JSON or a value the schema rejects triggers a "Settings Error" dialog
    at the start of every interactive session. Committed settings files must
    therefore hold only valid keys.
    ([settings.md, "Fix a broken settings file"](https://code.claude.com/docs/en/settings.md#fix-a-broken-settings-file))

## Unused scopes

**Managed settings are unused, deliberately.** They are the only scope a
repo-local file cannot override, but nothing needs them yet, and they raise
the bar rather than close it: whoever controls the machine can still change
them. Revisit if the [local-settings guard](#local-settings) proves
inadequate.

## Local settings

**Rule:** in a managed repo, `.claude/settings.local.json` stays gitignored
(in the managed `.gitignore` block), and every copy Claude Code may load is
absent or parses to exactly `{}`. Any rule it would hold belongs in the repo
template or in the user file. Committing the file was rejected: Claude Code
writes the file itself, and in a worktree the tracked copy is not the one
that takes effect ([fact 5](#platform-facts)).

**Where it is enforced.** The agent chokepoints `vrg-commit`,
`vrg-pr-workflow report-ready` and `vrg-container-run` (host-side) check
every copy Claude Code may load:

- the main checkout's root, found through `git rev-parse --git-common-dir`;
- the current worktree's own copy;
- the invoking directory's copy and those of its ancestors up to the
  worktree root.

An agent identity is refused if any copy holds anything other than `{}`. A
session counts as an agent when any identity signal says so. A human
identity gets the same message as a warning and the tool proceeds. An absent
file passes. The guard acts only in repos whose `origin` belongs to an
in-scope org (`vergil-project`, `logical-minds-foundry`,
`mnemosys-project`).

These chokepoints don't depend on Claude Code hooks, because a local file
that disables hooks would disable a hook-based check too. `vrg-validate` is
not a chokepoint: it runs inside a dev container, where it sees neither the
main checkout's working tree nor the identity. The nightly config audit does
not check this file either, because a CI checkout has no local file.

The guard is being added under epic
[vergil-project/.github#558](https://github.com/vergil-project/.github/issues/558);
until it ships, the rule is stated in the `CLAUDE.md` template region only.

**Known limit.** Claude Code keeps the local file with `.claude/settings.json`
instead of at the main checkout's root when the repository root, or its
`.git` or `.claude` entry, isn't owned by the user (and in a few other
cases: outside a git repository, when the repository root is the home
directory, and on Windows;
[settings.md](https://code.claude.com/docs/en/settings.md)). The guard does
not check that relocated copy.

## Recurring review

Two layers keep the files matching this model: what a machine can check,
and what needs judgment.

### Mechanical: the nightly config audit

`vrg-github-repo-config audit` runs in each managed repo's scheduled
`ops.yml` job (see [GitHub Config Audit](../reference/config-audit.md)).
Under this model it checks the following. Epic
[vergil-project/.github#558](https://github.com/vergil-project/.github/issues/558)
adds the line budget, the `AGENTS.md` rule and the exact template
comparison to today's audit.

- the repo `CLAUDE.md` is at most 200 lines (a hard failure once the
  cleanup has landed fleet-wide);
- the template markers are present;
- no `AGENTS.md` exists;
- `.claude/settings.json` equals the template after JSON parsing;
- the `paad-policy.md` pointer is present (it is in the template region, so
  the template check covers it).

### Judgment: `steering-audit`

The `steering-audit` skill in vergil-claude-plugin (being added under the
same epic) reviews the steering and settings files against this page. For
each finding it verifies, suggests a disposition, and waits for the human's
decision. It re-verifies the
[platform facts](#platform-facts) against the docs at the time of the run.
It has two scopes:

- **repo** (the default): that repo's `CLAUDE.md`, `.claude/rules/` and
  `.claude/settings.json`. Each finding is fixed, filed to intake, or
  dropped by the human with a reason.
- **user** (`steering-audit user`): the global `~/.claude/CLAUDE.md` and
  `~/.claude/settings.json`, reviewed once per cycle, not once per repo.
  It is read-only and interactive only: the skill writes no file, every
  change is the human's, made on the host, and its findings never go to
  intake or any other public record.

**When to run it:** at least quarterly, and whenever a Claude Code release
changes settings or instruction-file behavior.

### Optional extra: `/doctor prompt-audit`

The human can run `/doctor prompt-audit` by hand in a session as an extra
check on instruction files ([fact 10](#platform-facts)); the review does
not depend on it. Whether `steering-audit` should run `claude-api
prompt-audit` itself is tracked in intake item
[vergil-project/.github#604](https://github.com/vergil-project/.github/issues/604).

## Related

- [Permission Model](permission-model.md): the permission layers these
  settings files configure
- [GitHub Config Audit](../reference/config-audit.md): the nightly audit
  that enforces the mechanical rules
- [Consuming Repo Setup](consuming-repo-setup.md): how a repo gets its
  settings and `CLAUDE.md` from the templates
- [Agent VM `.claude` Share Set](agent-vm-claude-share-set.md): how the
  user file and plugins reach agent VMs
