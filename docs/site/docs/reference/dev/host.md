# vrg-host

**Installed as:** `vrg-host` (Python console script)

**Source:** `src/vergil_tooling/bin/vrg_host.py` (libraries:
`src/vergil_tooling/lib/host_deps.py`, `host_methods.py`, `host_pins.py`,
`host_status.py`, `host_versions.py`)

Observes and manages the tools Vergil development needs **on the macOS host
itself** — the ones that cannot live in a dev container or an agent VM, such as
`gh`, `git`, `uv`, `gcloud`, Lima, Docker Desktop and the Xcode Command Line
Tools. It reports how far each one has drifted
from its latest release, upgrades the ones it is allowed to upgrade, and records
deliberate **pins** as tracked technical debt rather than silent freezes.

`vrg-host` is a host tool: run it on the Mac, not inside `vrg-container-run`.
It runs as the ordinary user and never prompts for a password (see
[No privilege escalation](#no-privilege-escalation)).

Design: epic
[vergil-project/.github#167](https://github.com/vergil-project/.github/issues/167).
The pinning doctrine is inherited from epic
[vergil-project/.github#155](https://github.com/vergil-project/.github/issues/155).

## Subcommands

| Command | What it does |
| --- | --- |
| `vrg-host` / `vrg-host status` | Read-only report of every host dependency (the default) |
| `vrg-host status --fix` | Run the remediation of each unsatisfied auxiliary check first, then report |
| `vrg-host upgrade <dep>…` | Upgrade the named dependencies |
| `vrg-host upgrade --all [--force]` | Upgrade every `auto` dependency, holding pinned ones unless `--force` |
| `vrg-host pin <dep> --constraint … --inducing-release … --reason …` | Hold a dependency back |
| `vrg-host unpin <dep>` | Free a pin (kept as history) |

### status

For each dependency in the [host-dependency manifest](#the-host-dependency-manifest),
`status` prints its install method, installed version, latest version, version
state and severity, followed by any pin and auxiliary-check detail lines, and an
`overall:` line. It changes nothing on the host (unless `--fix` is given).

Each dependency is classified on two axes, its **version state** and its
**auxiliary checks**, and gets the worse of the two severities:

| State | Meaning | Severity |
| --- | --- | --- |
| `current` | Installed is at or newer than latest | OK |
| `latest-unmanaged` | The manifest declares no latest feed; the installed version is shown as info | OK |
| `pinned` | An active pin holds it, and the installed version satisfies the constraint (tracked debt) | OK |
| `pinned` | An active pin holds it, but the installed version has drifted off the constraint, or the pin is [due for re-evaluation](#re-evaluation) | WARN |
| `behind` | Installed is older than latest, unpinned (shown as "drift") | WARN |
| `latest-unknown` | A latest-version probe that should work did not answer | WARN |
| `missing` | Not installed (or not installed by the manifest's method) | ERROR |
| `probe-error` | A probe answered but its output could not be read, two versions could not be ordered, or the entry could not be probed | ERROR |
| any state + an unsatisfied auxiliary check | The tool is present but a health check fails | ERROR |

#### Update levels (macOS)

Software Update can offer more than one macOS update at once, for example an
update within the current major **and** the next major. For `macos`, `status`
reports every level that is behind instead of only the newest:

```text
[WARN ] macos            custom              26.5.2 -> 27.0.1       behind (drift): minor 26.7.1, major 27.0.1
```

- Each offered version is classified against the installed one by the first
  component that differs: the first is a **major** update, the second a
  **minor**, and any later one a **patch** (`26.5.2` with only `26.5.3` offered
  reads `behind (drift): patch 26.5.3`).
- An offer that is not newer than the installed version is ignored. When
  several offers fall in the same level, the highest is shown.
- The `latest` column shows the **leading edge**: the highest offer, or the
  installed version when nothing newer is offered (`current`).
- Any level behind is WARN (drift); severity and exit codes are as for every
  other dependency. Only what Apple actually lists is reported. If a patch
  release has been superseded, Software Update stops listing it, and `status`
  does not invent it.
- A macOS offer the probe cannot read (a title with no `Version`, or a version
  that is not dotted numeric, such as `26.7.1 (a)`) is a `probe-error`, never
  dropped.

The Xcode Command Line Tools (`xcode-clt`) also come from Software Update, so
they report update levels the same way, from the "Command Line Tools" items
that `softwareupdate --list` lists:

```text
[WARN ] xcode-clt        custom              26.6.0 -> 26.7         behind (drift): minor 26.7
```

Only `macos` and `xcode-clt` report update levels. Every other dependency
compares against a single latest version.

A broken probe for one dependency becomes a `probe-error` row for that
dependency only; the rest of the report still runs. Nothing is ever guessed at:
an unreadable version is an error, never a false `current` or `missing`.

**Exit code** — the worst severity across all dependencies:

| Exit | Meaning |
| --- | --- |
| `0` | Everything OK (including tracked-debt pins and info-only rows) |
| `1` | At least one warning (drift, latest unknown, a pin needing attention) |
| `2` | At least one error (missing, probe error, unsatisfied check), a failed `--fix` remediation, or an unreadable manifest or pins file |

**`--fix`** runs, before the report, the remediation of each unsatisfied
auxiliary check on every *installed* dependency (a missing dependency is skipped
and reported as `missing`). Each remediation is verified by re-running its check.
A remediation that fails, or leaves its check unsatisfied, is printed to stderr
and forces exit `2`. Remediations run unprivileged like every other command.

### upgrade

```bash
vrg-host upgrade gh lima       # named dependencies
vrg-host upgrade --all         # every auto dependency
vrg-host upgrade --all --force # also upgrade past active pins
```

Name one or more dependencies **or** pass `--all`, not both.

- Only `upgrade = "auto"` dependencies are upgraded, through their install
  method (`brew upgrade`, `uv tool upgrade`, or the manifest's own upgrade
  command). A `manual` dependency (`docker-desktop`, `macos`, `xcode-clt`) is refused when
  named, and listed as skipped under `--all`; upgrade it by hand.
- A Homebrew cask that updates itself (its `brew info` JSON says
  `auto_updates: true`) is never upgraded through `brew`. Without `--greedy`,
  `brew upgrade` skips such a cask without saying so, so `vrg-host` reports the
  upgrade as failed instead.
- An **actively pinned** dependency is **held**: the package managers cannot
  upgrade *to* a constraint, so the pin is a real hold. Run `vrg-host unpin
  <dep>` first, or pass `--force` to upgrade past it. An active pin whose
  constraint is unsupported is an error even under `--force`.
- An upgrade that fails, or that would need elevated privileges, is reported for
  that dependency and never retried elevated. Upgrade it manually.

Before upgrading anything, `upgrade` probes each `auto` dependency's installed
and latest versions and classifies them exactly as `status` does. It then
prints one line per dependency saying what it actually did:

```text
skipped  uv: current (0.13.1)
upgraded git: 2.54.0 -> 2.56.0_1
unchanged bash: still 5.3.9, latest 5.3.20 (the upgrade command succeeded but did not change the installed version)
ran      vergil-tooling: 2.1.244 -> 2.1.244 (unchanged; no latest feed)
held     gh: pinned to 2.40.x (2.41.0 breaks vrg-gh auth) at 2.40.1, holding back from 2.42.0; run 'vrg-host unpin gh' or pass --force to upgrade past the pin
ERROR    lima: missing (not installed by brew-formula); vrg-host upgrades, it does not install
skipped (upgrade = manual, upgrade by hand): docker-desktop, macos
```

| Outcome | When | Severity |
| --- | --- | --- |
| `skipped … current (<v>)` | Already current; the upgrade command is **not** run | OK |
| `upgraded … <before> -> <after>` | Was behind; upgraded and reached the latest | OK |
| `upgraded … (still behind latest <v>)` | Was behind; the version moved but is still older than the latest (or cannot be ordered against it) | WARN |
| `unchanged …` | Was behind; the upgrade command succeeded but the installed version did not change | WARN |
| `ran … <before> -> <after> (no latest feed)` | `latest = "unmanaged"`: nothing to compare with, so the upgrade runs and the change is reported | OK |
| `ran … (latest unknown: …)` | The latest probe did not answer (as `status`'s `latest-unknown`) | WARN |
| `held …` | Actively pinned and no `--force`; shows the installed version and the latest it holds back from | WARN (not counted under `--all`) |
| `ERROR … missing` / `ERROR … probe-error` | Not installed, or a probe failed, as `status` reports it; the upgrade is not attempted | ERROR |
| `ERROR … upgrade ran but …` | The installed version could not be re-probed after the upgrade | ERROR |

The `after` version is always **re-probed** after the upgrade runs, never
assumed to be the latest. `--force` only upgrades past a pin. It never forces
the upgrade of a dependency that is already current.

**Exit code:** `0` when everything requested was upgraded or already current
(or, under `--all`, deliberately skipped or held); `1` when an explicitly named
dependency was refused (`manual` or pinned), or an upgrade left a dependency
behind (or ran with its latest unknown); `2` on any failure: an unknown
dependency name, a missing dependency, a probe error, a failed or
privilege-refused upgrade, an unsupported pin constraint, or an unreadable
manifest or pins file.

### pin and unpin

```bash
vrg-host pin gh \
  --constraint 2.40.x \
  --inducing-release 2.41.0 \
  --reason "2.41.0 breaks vrg-gh auth" \
  --tracking-issue https://github.com/vergil-project/vergil-tooling/issues/NNNN \
  --deterministic

vrg-host unpin gh
```

| Flag | Required | Meaning |
| --- | --- | --- |
| `<dep>` | yes | A dependency declared in the manifest; anything else is refused |
| `--constraint` | yes | The version range to hold to (see [Constraint grammar](#constraint-grammar)) |
| `--inducing-release` | yes | The release that broke us — the pin's anchor |
| `--reason` | yes | Why the pin exists. A blank reason is refused (the justification gate) |
| `--tracking-issue` | no | URL of the issue tracking the breakage |
| `--deterministic` | no | The breakage reproduces deterministically (selects the re-evaluation path) |

`pin` records an `active` pin stamped with today's date, replacing any earlier
pin for the same dependency. `unpin` sets the pin's state to `freed` and **keeps
it in the pins file as history**, so the file stays the audit trail of what was
held back and why. Unpinning a dependency with no pin, or one already freed, is
refused. Both commands exit `0` on success and `1` when refused.

## The host-dependency manifest

The list of host dependencies is a manifest **packaged with vergil-tooling**:
`src/vergil_tooling/data/host_dependencies.toml`. It is tooling-owned and is
**never** overridable by a repository, `vergil.toml`, or the current directory.
This is a security invariant: the manifest carries shell commands for probes,
upgrades and checks, and letting a repo supply them would be an arbitrary-code
execution surface on the host. Changing the list means a vergil-tooling PR.

Each entry declares the dependency's `method` (`brew-formula`, `brew-cask`,
`uv-tool` or `custom`), whether `vrg-host` may upgrade it (`upgrade = "auto"` or
`"manual"`), whether it has a latest-version feed (`latest = "probe"` or
`"unmanaged"`), and any auxiliary checks. A `custom` entry supplies its own
installed, latest and upgrade commands. Its latest command is either
`latest_version`, which prints one version, or `latest_versions`, which prints
every offered version, one bare version per line, for
[update-level reporting](#update-levels-macos). An entry may declare only one of
the two, and `latest_versions` requires `latest = "probe"`; the manifest is
refused at load otherwise. An entry with any other method can
override only how its installed version is read. This is for a self-updating
app, whose package-manager record goes stale.

A tool earns an entry only if Vergil development needs it on the macOS host
itself. Current entries:

| Dependency | Method | Upgrade | Latest | Why it is on the host |
| --- | --- | --- | --- | --- |
| `uv` | brew-formula | auto | probe | Installs and upgrades the `vrg-*` host tools |
| `gh` | brew-formula | auto | probe | GitHub CLI under `vrg-gh` and the PR/issue workflows |
| `git` | brew-formula | auto | probe | `vrg-git`, `vrg-commit`, the PR workflow, `vrg-release` and worktrees run it on the host |
| `bash` | brew-formula | auto | probe | The host-side `vrg-*` tools and the hook-guard shim run it |
| `gcloud` | custom (`gcloud components update`) | auto | probe | IAP tunnel for `vrg-vm` cloud sessions |
| `lima` | brew-formula | auto | probe | Runs the local agent VMs `vrg-vm` creates |
| `docker-desktop` | brew-cask (installed version from the app) | manual | probe | The container engine and `docker` CLI that `vrg-container-run` execs |
| `opentofu` | brew-formula | auto | probe | Provisions cloud agent VMs for `vrg-vm` (preflight requires >= 1.8.0) |
| `git-cliff` | brew-formula | auto | probe | Changelog generator `vrg-release` requires |
| `vergil-tooling` | uv-tool | auto | unmanaged | The `vrg-*` host tools themselves |
| `claude-code` | brew-cask | auto | probe | The host-side agent for out-of-sandbox work, such as reattaching an agent session with human access |
| `macos` | custom | manual | probe | The host OS; a pending update is surfaced, never applied |
| `xcode-clt` | custom | manual | probe | Xcode Command Line Tools: Homebrew needs them, and `/usr/bin/git` is a CLT shim; a pending update is surfaced, never applied |

Notes on the entries:

- **`uv` is a Homebrew formula.** On the macOS host uv is installed with
  Homebrew (`brew install uv`), and Homebrew builds have no `uv self update`, so
  `vrg-host` reads and upgrades it through `brew`. Its latest is Homebrew's
  stable version, which can trail Astral's newest release by a few days.
- **`gcloud` updates itself.** It is installed from the Homebrew `gcloud-cli`
  cask, but the SDK's component manager owns its updates: `vrg-host` reads its
  `core` component version and upgrades it with `gcloud components update`. The
  SDK lives in a user-owned directory, so this needs no `sudo`. Homebrew's
  recorded cask version goes stale as the SDK updates itself. Never upgrade it
  with `brew upgrade --greedy`.
- **`docker-desktop` and `macos` are report-only.** `vrg-host` tracks the
  Docker Desktop app, not the `docker` CLI (their version numbers differ). The
  installed version comes from the app's `Info.plist`, because Desktop updates
  itself and Homebrew's record goes stale. The latest comes from the
  `docker-desktop` cask (its `4.94.0,241994` reads as `4.94.0`). So `status`
  shows when Desktop is behind, but `upgrade` leaves it to Desktop's own updater,
  which may install privileged helpers. Moving to a headless engine (Colima,
  podman, docker-engine over Lima) would make Docker an `auto` entry; that is
  follow-on work
  ([vergil-project/.github#169](https://github.com/vergil-project/.github/issues/169)).
  A pending macOS update is reported so it is applied deliberately, never by
  `vrg-host`. The `macos` probe reads `softwareupdate --list` and reports
  every pending [update level](#update-levels-macos).
- **`git` and `bash` are Homebrew's.** On a Homebrew-first Mac, `git` resolves
  to `/opt/homebrew/bin/git` ahead of the Command Line Tools shim
  `/usr/bin/git`, and `bash` to Homebrew's bash 5 ahead of Apple's `/bin/bash`
  3.2. The copies that actually run are therefore not updated with macOS, so
  `vrg-host` tracks and upgrades them through `brew`.
- **`xcode-clt` is report-only.** Homebrew cannot work without the Xcode
  Command Line Tools, so a missing CLT is an ERROR. Apple ships CLT updates
  through Software Update, separately from macOS. The installed version comes
  from `pkgutil --pkg-info=com.apple.pkg.CLTools_Executables`, which prints a
  version with a trailing build stamp (`26.6.0.0.1781586589`). `vrg-host` keeps
  only the first three components (`26.6.0`), the precision Software Update
  uses, so its `Version: 26.6` reads as current and `26.7` as a minor update.
  The latest comes from the "Command Line Tools" items in `softwareupdate
  --list`. When Software Update lists none, the CLT is current. An item the
  probe cannot read is a `probe-error`, never dropped. A listed item that is
  not newer than the installed version is ignored, like any offer that is not
  an update. Install CLT updates with Software Update; `vrg-host` never
  applies them.
- **`claude-code` is upgraded through Homebrew.** It is installed from the
  `claude-code` cask, which does not declare `auto_updates` (Homebrew API,
  checked 2026-10-16), so `vrg-host upgrade claude-code` runs `brew upgrade
  --cask claude-code`. If the cask ever becomes self-updating, the upgrade is
  refused (see [upgrade](#upgrade)) and the entry must become `manual`. It is
  on the host as the agent for out-of-sandbox work, such as reattaching an
  agent session on the Mac with human access. The original design excluded it
  as a personal tool; that was reversed in
  [#3300](https://github.com/vergil-project/vergil-tooling/issues/3300).
- **`vergil-tooling` has no latest feed.** It is installed from git, so `uv`'s
  outdated feed cannot see new releases; `status` reports it as
  `latest-unmanaged` rather than a false `current`. `vrg-host upgrade
  vergil-tooling` runs `uv tool upgrade vergil-tooling`, which moves within the
  release line you installed.
- **Install method matters.** Each entry assumes the install path verified on a
  real macOS host. A tool installed some other way (for example, uv from
  Astral's standalone installer) reads as `missing` for the brew and `uv-tool`
  methods; it is never reported as a false `current`.

Deliberately **not** in the manifest:

- **Linters** (shellcheck, hadolint, yamllint, actionlint, markdownlint) — they
  run only inside the dev container via `vrg-validate`. Do not install them on
  the host.
- **ssh** and other OS-provided tools — updated with macOS (the `macos`
  entry). **jq** ships with macOS. (git and bash are tracked, because the
  copies that run are Homebrew's.)
- **Full Xcode** — nothing on the host needs more than the Command Line Tools.
- **az** (Azure CLI) — only `vrg-vm --provider azure` needs it, and GCP is the
  provider in use. It gets an entry if Azure becomes a working provider.
- **nfpm, npm, language toolchains** — container and CI only.
- **Homebrew** itself, editors and other personal tools.
- **Everything else installed with Homebrew** — only the curated entries above
  are tracked or upgraded, never a blanket `brew upgrade`.

### Auxiliary checks

A dependency can carry health checks beyond its version, each with an optional
remediation that `status --fix` runs. An unsatisfied check is an ERROR.

| Dependency | Check | What it verifies | Remediation |
| --- | --- | --- | --- |
| `gcloud` | `numpy-in-interpreter` | The Python interpreter gcloud runs on can import NumPy. Without it every IAP tunnel session prints an "install NumPy" warning and the tunnel is slower. | `pip install numpy` into that interpreter, unprivileged |

If the interpreter is externally managed (PEP 668), `pip` refuses the install.
`vrg-host` reports that as a failed remediation rather than working around it;
install NumPy into that interpreter yourself.

## Pinning host dependencies

The default for every host dependency is **unpinned**: float on the leading
edge. `vrg-host` adopts the org-wide pinning doctrine of epic
[#155](https://github.com/vergil-project/.github/issues/155), which treats a pin
as technical debt to be paid down, not a setting:

1. **Default is unpinned.**
2. **A pin is a reaction, never a default** — a reaction to a release breaking
   us.
3. **Every pin carries a written justification** — `vrg-host pin` refuses a pin
   without `--reason`.
4. **Pin the least-specific version that solves the problem** — `1.x`, not the
   exact patch.
5. **When in doubt, set it free** — unpin, see what breaks, and pin only the
   culprit.
6. **A pin is anchored to its inducing release and is re-evaluated when the
   leading edge moves past it** — never permanent.

### Where pins live

Pins are **your** configuration, not package data. They live in
`~/.config/vergil/host-pins.toml`, next to `identities.toml`, and are overlaid
onto the packaged manifest when `status` and `upgrade` run. (Pins are never
written into the packaged manifest: that would modify the installed package and
be lost on every reinstall.)

`vrg-host pin` and `unpin` manage the file; it looks like this:

```toml
[pin.gh]
constraint = "2.40.x"
inducing_release = "2.41.0"
deterministic = true
reason = "2.41.0 breaks vrg-gh auth"
state = "active"
tracking_issue = "https://github.com/vergil-project/vergil-tooling/issues/NNNN"
pinned_date = "2026-10-15"
```

The fields are #155's pin schema plus one host extension, `pinned_date`, so
`status` can show how long a pin has stood. A pin's `state` is `active` (holds
the dependency), `under-evaluation` (shown in `status` but not holding), or
`freed` (history). `vrg-host pin` only creates `active` pins; moving a pin to
`under-evaluation` is a hand edit of this file.

The file is validated strictly. A missing file means "no pins". It is an error
(exit `2` from `status` and `upgrade`) if the file is not valid TOML, has keys
other than `[pin.<dep>]` tables, pins a dependency the manifest does not declare,
or has a pin without a `reason`. The pins file can only hold back declared
dependencies; it cannot add dependencies or change their methods, probes or
checks.

### Constraint grammar

A constraint is a dotted version prefix, optionally ending in `x` wildcards:

| Constraint | Matches |
| --- | --- |
| `1.x` | any `1.*` release |
| `2.40.x` | any `2.40.*` release |
| `2.40.0` | exactly `2.40.0` (a Homebrew rebuild such as `2.40.0_1` also matches) |

Any other syntax — `>=`, `<=`, `~=`, an `x` that is not trailing, empty
components — is refused when you run `vrg-host pin`, never guessed at.

### How a pin is surfaced

`status` renders each active pin as tracked debt under its dependency's row:
the constraint, the latest version it is holding back from (the size of the
debt), the inducing release, the reason, the tracking issue, and the pin's age.
For `macos`, the row still lists every pending update level, so an update the
pin allows (such as `26.7.1` under a `26.x` pin) stays visible.

### Re-evaluation

A pin is valid only while its inducing release is the leading edge. When the
latest available version moves past `inducing_release`, `status` marks the pin
`RE-EVALUATION DUE` and the row becomes WARN. For `macos`, the latest is the
leading edge: the highest version Software Update offers. If the latest version is unknown,
or the two versions cannot be compared, it shows `RE-EVALUATION UNKNOWN` (also
WARN). For a dependency with no latest feed, it reminds you to check the leading
edge by hand.

When a pin is due, follow the #155 process:

- **Deterministic breakage** (`deterministic = true`) — re-test against the new
  leading edge. If it works, `vrg-host unpin` the dependency; if it still
  breaks, re-pin with the new release as `--inducing-release`.
- **Non-deterministic breakage** — move the pin to `under-evaluation` under a
  tracking issue and observe.

## No privilege escalation

`vrg-host` runs as the ordinary user. It refuses to run any command that starts
with `sudo`, `doas`, `su` or `pkexec`, and gives every child process a closed
stdin, so a command that unexpectedly asks for a password fails instead of
prompting. An upgrade or remediation that needs elevated privileges is reported
as needing manual action and is never retried elevated.

## Known limitations

- The version parsers are tested against output captured on a real Mac. A few
  cases could not be captured there and are still hand-written: `softwareupdate`
  with nothing pending, only non-OS updates pending, or only a patch update
  pending, and `uv tool list --outdated` listing an outdated tool. The
  Command Line Tools update offer (`softwareupdate --list` listing a CLT item
  newer than the installed one) and the full `pkgutil --pkg-info` output are
  hand-written as well: only the CLT `version:` line was recorded. See
  `tests/vergil_tooling/fixtures/host/README.md`. If one of those cases shows a
  `probe-error` row, it may be a parser mismatch rather than a broken tool.
- `vrg-host` reports and acts only when you run it. Scheduled or automatic runs
  are follow-on work
  ([vergil-project/.github#169](https://github.com/vergil-project/.github/issues/169)).
