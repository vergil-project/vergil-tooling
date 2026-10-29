# Getting Started

A quickstart for putting a repository under vergil-tooling. You
install the host tools, let `vrg-github-repo-init` create (or adopt)
the repository, and verify the result. For the detailed walkthrough,
see [Consuming Repo Setup](guides/consuming-repo-setup.md).

## Prerequisites

Install these on your host:

- **Homebrew** (macOS) — with the Xcode Command Line Tools, which Homebrew
  needs (`xcode-select --install`). Install the host tools below with it.
- **git** — `brew install git` on macOS; `vrg-git`, `vrg-commit` and the PR
  workflow run it on the host
- **Docker** — the dev container engine
- **uv** — Python package manager
  ([install](https://docs.astral.sh/uv/getting-started/installation/))
- **`gh` CLI** — GitHub CLI, authenticated as you
  (`gh auth login`)
- **macOS or Linux** (Bash)

Everything else — language runtimes, linters, test frameworks — lives
inside the dev container. The `vrg-*` tools are installed on the host
with `uv tool install`.

This list is the minimum to get started. On macOS, once vergil-tooling is
installed (step 1), `vrg-host status` is the complete host-dependency check: it
reports every tool Vergil needs on the host, including the Command Line Tools,
git and the agent-VM tools, and flags anything missing or out of date.

## 1. Install vergil-tooling on the host

```bash
uv tool install --python 3.14 'vergil-tooling @ git+https://github.com/vergil-project/vergil-tooling@v2.1'
```

This installs all `vrg-*` console scripts into `~/.local/bin/`.
`uv`'s official installer already puts that directory on `PATH`; with
a Homebrew-installed `uv`, run `uv tool update-shell` once to add it.

```bash
which vrg-container-run    # should resolve to ~/.local/bin/vrg-container-run
vrg-container-run --help   # should print usage
```

On macOS, `vrg-host` then reports the health of every tool Vergil needs on
the host (installed vs. latest, missing tools, health checks) and upgrades
them:

```bash
vrg-host                   # read-only status report; exit 0 = all OK
vrg-host upgrade --all     # upgrade the tools vrg-host manages
```

See the [vrg-host reference](reference/dev/host.md) for the dependency list
and for pinning a tool that a new release has broken.

On Linux, vergil-tooling is also published as a signed `.deb`/`.rpm`
package (this is how Lima and cloud agent VMs get it). Follow the
[`vergil-project/packages` README](https://github.com/vergil-project/packages#readme)
to set up the apt or dnf repository. Check the downloaded signing key
against the pinned primary fingerprint
`B3A1D804DC03AB036566A4E367861822166ECABE`.

## 2. Create or adopt the repository

`vrg-github-repo-init` builds a managed repository that conforms from
its first commit. Run it in a human shell: it uses your `gh`
credentials for `gh repo create` and the GitHub settings, and it
refuses to run under an agent identity.

**A new repository.** Run it from inside the directory named after
the GitHub org (or any subdirectory of it), or name the clone's
parent directory with `--target-dir`:

```bash
cd ~/dev/projects/<org>
vrg-github-repo-init <org>/<name>
```

The wizard prompts for the project description, repository type,
primary language, branching model, versioning scheme, release model
and the other `vergil.toml` values. Every prompt has a matching flag;
`--non-interactive` runs with no prompts and fails loudly if a
required flag is missing (see `vrg-github-repo-init --help`).

**An existing repository.** From the root of its clone, on a feature
branch:

```bash
vrg-github-repo-init --adopt
```

Adopt overwrites every Vergil-managed file to its canonical state —
`vergil.toml`, `CLAUDE.md`, `.claude/settings.json`,
`.claude/hooks/guard.sh`, `LICENSE`, `README.md`, `.gitignore`,
`.github/dependabot.yml`, the CI/CD workflows and the docs site config — and applies the GitHub
settings, rulesets and labels. It asks for confirmation first. Values
already in `vergil.toml` become the prompt defaults, and hand-written
tables such as `[package]` or `[container]` are kept. Review the
resulting commits and land them through a PR.

Either way, the repository ends up with:

| Path | What it is |
|---|---|
| `vergil.toml` | The repository profile and tooling config |
| `CLAUDE.md` | Agent guidance: a short header, the canonical template region between its `vergil:template:claude-md` markers (fleet-wide agent policy such as worktrees, `vrg-git`/`vrg-gh`, validation and the PR handoff), then your repo's own notes; at most 200 lines in all (see [Consuming Repo Setup](guides/consuming-repo-setup.md#claudemd)) |
| `.claude/settings.json` | Wires the hook guard, enables the Vergil Claude Code plugin, and turns off Claude Code's AI co-author attribution |
| `.claude/hooks/guard.sh` | Hook-guard shim that blocks raw `git`/`gh` in agent sessions |
| `.gitignore` | The vergil-managed ignore block for your language, including `.worktrees/` |
| `.github/dependabot.yml` | The canonical report-only Dependabot config: every entry has `open-pull-requests-limit: 0`, so Dependabot never opens a PR |
| `.github/workflows/` | `ci.yml`, `ops.yml` (nightly config audit, branch sweep and Dependabot alert audit), `epic-rollup.yml`, and `cd.yml` when the repo publishes releases or docs |

`.claude/settings.json` is the packaged template
[`claude_settings.json`](https://github.com/vergil-project/vergil-tooling/blob/develop/src/vergil_tooling/data/claude_settings.json),
unchanged. It enables the plugin but does not define its marketplace,
which lives in your user settings (see
[Claude Config Ownership](guides/claude-config-ownership.md)). Its
`attribution` block is what keeps Claude Code from adding an AI
co-author trailer to commits and PRs; AI co-author attribution is not
used (#3200):

```json
"attribution": {
  "commit": "",
  "pr": ""
}
```

## 3. Verify

```bash
# Full validation, inside the dev container
vrg-container-run -- vrg-validate

# Config audit: vergil.toml, CLAUDE.md, .claude/, .gitignore, workflows,
# and the GitHub settings (exit 0 compliant, 1 drift, 2 could not complete)
vrg-github-repo-config audit
```

`vrg-validate` includes `vrg-repo-profile`, which checks `vergil.toml`.
The hook guard fires on Claude Code `Bash` tool calls; to test it end
to end, have Claude Code try a raw `git commit` in a session in the
repository.

## Next steps

- **[Consuming Repo Setup](guides/consuming-repo-setup.md)** —
  what each generated file does, the `vergil.toml` profile, CI, the
  config audit, keeping up to date, and troubleshooting.
- **[Identity Setup](guides/account-setup.md)** — the GitHub App
  identities that AI agents run as.
- **[Git Workflow](guides/git-workflow.md)** — branching, commit /
  PR / finalize cycle, two-layer enforcement, worktrees in practice.
- **[Worktree convention spec][worktree-spec]**
  — full rationale for the parallel-agent convention, failure
  modes, memory-path implications.

[worktree-spec]: https://github.com/vergil-project/vergil-tooling/blob/develop/docs/specs/worktree-convention.md
