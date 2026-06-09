# Vergil GUI: Vision & Feasibility Study

**Issue:** #1532
**Date:** 2026-06-08
**Status:** Draft — Vision / Feasibility (not an implementation plan)
**Relates to:** Forge Abstraction Strategy (#1521)

## What this is

A north-star vision and an honest feasibility assessment for a desktop
GUI that becomes the single interface to the Vergil workflow. It is a
strategy artifact, deliberately written to be picked up *later* — when
the core tooling plateaus and there is room to build on top of it. It is
not an implementation plan, and nothing here commits us to building.

The assessment is framed against one primary user — the maintainer, as a
power user on his own machine. A wider audience is a real long-term
aspiration but is treated as a secondary evolution (see
[Wider audience](#wider-audience-secondary)), because designing for users
on machines we do not control is a categorically larger problem and would
distort an honest feasibility read of the near-term tool.

## Motivation: the real pain is window management

It is tempting to frame this as "CLIs are hard, so add a GUI." That is
not the motivation. The `vrg-*` suite works well, and the maintainer is
fluent in it.

The actual pain is spatial. The daily setup is two macOS windows in
fullscreen split-screen: iTerm on the left half (one tab as the human
shell running `vrg-*` commands, plus roughly three tabs each running
Claude inside a VergilVM sandbox), and Safari on the right half for the
forge (issues, pull requests, CI). This split-screen arrangement is not a
preference — it is a workaround. macOS does not let you pick up a *set* of
related windows and move them around external monitors as a unit. Gluing
two windows into one fullscreen "space" is the only way to treat the
working context as a single movable object: send it to a second monitor,
or pull it back to the laptop, in one gesture.

A single application window solves this directly. The OS treats one window
as one object, so it moves and fullscreens as a unit for free. And because
the panes then live *inside* the window — under the application's control
rather than the window manager's — it opens up spatial arrangements the OS
will never allow with raw iTerm and Safari: detachable panes, multiple
workspace windows, saved layouts. The window-management win is the
foundation; everything else is built on top of having one coherent surface
to work in.

A secondary benefit follows naturally: once the workflow lives in one
app, the CLI's power can be surfaced as wizards and buttons, and all the
machinery the project already invests in — CI gates, auditing, identity
modes, the release workflow — comes along "for free," because the GUI
drives the existing tools rather than reimplementing them.

## Foundational decisions

These were settled during the brainstorm and frame the rest of the
document.

1. **Thin orchestration.** The GUI is a launcher and dashboard. The
   `vrg-*` CLIs, `limactl`/Lima, and the forge abstraction remain the
   source of truth. The GUI spawns them, embeds their terminals, and
   surfaces their state. It never re-homes their logic. This is the single
   decision that turns a multi-year rewrite into a tractable tool: the
   hard logic already exists and is exercised daily, so the GUI inherits
   it instead of duplicating it.
2. **Tauri.** Lightweight native binaries with a system webview and a Rust
   backend for spawning subprocesses. Chosen over Electron (heavy; bundles
   Chromium), native macOS (best polish but macOS-only, which contradicts
   the independence goal), and a pure local web app (still needs a backend
   daemon for PTYs, so it is "half of Tauri" without desktop integration).
   The deciding factors were that it is small and lightweight *today*, and
   cross-platform as *option value* — it should be possible to make it
   work on Windows and native Linux later, which someone in the community
   could carry even if the maintainer never does. That cross-platform
   posture is the same value the forge abstraction exists to protect:
   independence from any one vendor's stack.
3. **Forge panel: webview now, native panels later.** Phase 1 embeds the
   forge's own web UI in a webview — it works with GitHub today, requires
   no forge-API work, and retires the Safari half immediately. As the
   #1521 forge-abstraction API matures, high-traffic views (PR list, CI
   status, review) get replaced with native panels that present a
   consistent experience across GitHub, Forgejo, and beyond.

## Architecture

Everything below the UI layer already exists. The GUI's job is to compose
it.

```text
┌────────────────────────────────────────────────────────────────────┐
│  Tauri shell (UI)                                                    │
│  workspace rail · embedded terminals (xterm.js) · forge webview ·    │
│  wizard forms · identity badge                                       │
└────────────────────────────────────────────────────────────────────┘
                  ▼ spawns subprocesses / reads structured output
┌────────────────────────────────────────────────────────────────────┐
│  Orchestration layer (Rust)                                          │
│  PTY management · process lifecycle · state cache ·                  │
│  the "never show green when it's red" discipline                     │
└────────────────────────────────────────────────────────────────────┘
                  ▼ drives existing tools — no logic reimplemented
┌──────────────────────┬──────────────────────┬──────────────────────┐
│  vrg-* CLIs          │  limactl / Lima       │  forge abstraction   │
│  commit · submit-pr  │  VM lifecycle ·       │  (#1521)             │
│  release · validate  │  shell sessions       │  GitHub → Forgejo →  │
│  whoami              │  (sandboxes)          │  N                   │
└──────────────────────┴──────────────────────┴──────────────────────┘
```

The key technical realization that grounds the whole assessment: a
VergilVM session is `limactl shell <instance>` — a subprocess attached to
a pseudo-terminal. "Embed a terminal running Claude in a sandbox" is
therefore "embed a PTY widget that runs `limactl shell`," which is a
solved problem with mature libraries (xterm.js on the frontend, a Rust PTY
crate such as `portable-pty` on the backend). It is not research.

## Feasibility by component

Ratings assume the foundational decisions above (you-first, thin, Tauri).

| Component | Rating | Why / main risk |
|---|---|---|
| Tauri shell + workspace model | **Easy** | Standard desktop scaffolding |
| Embedded terminals (PTY → `limactl shell`) | **Easy** | Mature libs (xterm.js + portable-pty) |
| Identity badge (human/user/audit) | **Easy** | `vrg-whoami` is already a resolver |
| Forge panel — webview (phase 1) | **Easy** | Embed the site; retires the Safari half |
| Wizards → `vrg-*` (repo/org/release/PR) | **Medium** | Forms are trivial; parsing CLI output/progress is the work |
| Sandbox / VM orchestration (lifecycle) | **Medium** | `limactl` is scriptable; staying correct across laptop sleep/restart is the risk |
| Live state (CI / PR / validate) without lying | **Medium** | Needs `--json` from CLIs; the progress-framework spec helps |
| Multi-agent USER/AUDIT + worktree view | **Hardest** | The most novel UX: representing parallel agents, pairing, pr-watch |
| Forge native panels (phase 2+) | **Later** | Gated on #1521 maturity |

**Verdict.** Feasible as a personal power tool. Nothing on the list
requires unsolved technology; the hard items are matters of robustness and
UX design, not of capability.

## The deepest risk: a thin GUI must not lie

The most important risk is not any single component — it is a discipline
that cuts across all of them. A thin GUI reflects state that actually
lives elsewhere (the CLIs, the VMs, the forge). If it ever shows "CI
green" when CI is red, or "validation passed" when it failed, it becomes a
hallucination layer — exactly the failure mode the project's
no-silent-failures principle exists to prevent. Stale or optimistic state
in a tool the maintainer trusts is worse than no tool.

Design implications:

- **Prefer reading truth over caching it.** Where the cost is acceptable,
  re-query rather than trust a cached value. Where caching is necessary,
  show staleness explicitly (timestamps, "checking…" states).
- **Surface failure loudly.** A subprocess that exits non-zero must
  produce a visible, honest error state — never a silently swallowed one.
- **This pulls `--json` forward.** The cleanest way for the GUI to know
  the truth is for the CLIs to emit structured, machine-readable status.
  This is the main piece of work that the thin approach pushes back into
  the CLI suite, and it is shared infrastructure that benefits scripting
  and CI as well, not just the GUI.

## Roadmap

The feasibility ratings imply a natural, value-first sequence. Each
version is a standalone productivity win; the later phases are optionality,
not obligation.

### v1 — the window-management win

- One movable, fullscreenable workspace window
- Workspace rail (switch between repos/orgs)
- Embedded terminals attached to `limactl shell` sessions and the host
  shell
- Forge webview (Safari retired)
- Identity badge driven by `vrg-whoami`

This alone replaces the split-screen workaround and is worth building on
its own merits.

### vNext — CLIs become buttons

- Wizards over `vrg-*`: new repo, new org, release, submit PR
- VM lifecycle controls (start/stop/status)
- Live CI / PR / validate status surfaced in the UI
- Depends on giving the relevant CLIs a `--json` output mode

### Later — the fuller vision

- Multi-agent USER/AUDIT pairing visualization
- Worktree / parallel-agent map (drawing the picture nothing draws today)
- Native forge panels on the #1521 abstraction
- Detachable panes and multiple workspace windows
- Cross-platform (Windows, native Linux) — plausibly community-carried

## Interaction with existing project boundaries

Two existing constraints matter for any future build and are recorded here
so they are not rediscovered the hard way:

- **PR submission stays human.** Agents do not run `vrg-submit-pr`, and
  merge/finalize are human actions. In the GUI this is natural, not
  awkward: the human is the one sitting at the GUI, so the
  "Submit PR" wizard is a human affordance by construction. The wizard
  should produce the same `.vergil/pr-template.yml` handoff that exists
  today, then invoke the human-run submit path.
- **Identity modes are first-class.** The GUI must never infer identity
  from `VRG_IDENTITY_MODE` alone; it surfaces whatever `vrg-whoami`
  resolves, including `--explain` warnings when signals disagree. The
  identity badge is a read-through of the canonical resolver, not a
  parallel implementation.

## Wider audience (secondary)

The stated long-term hope is to lower the barrier to creating and managing
open-source utilities with AI, and to bring the whole Vergil workflow —
CI gates, auditing, workflow management — to a wider audience "for free."
That ambition is real but deliberately out of scope for the near-term
feasibility read, because it changes the problem in kind, not degree:

- Onboarding and installers for non-experts
- Hiding (or safely exposing) the VM and identity machinery
- Supporting users on machines and forges we do not control
- Support, updates, and the long tail of "works on my machine"

The thin-orchestration + Tauri + forge-abstraction choices keep this door
open: a cross-platform, forge-neutral, lightweight app is the right
*starting shape* for an eventual product, even though productizing it is a
separate, larger effort. The honest position is that v1–vNext are for the
maintainer; broad distribution is a later decision made on its own merits.

## Open questions for whenever this is picked up

- What is the minimum `--json` surface across `vrg-*` that vNext needs,
  and is it worth standardizing a shared status schema first?
- How should VM lifecycle state survive laptop sleep/restart robustly —
  reconcile on launch, or maintain a supervising daemon?
- Does the multi-agent view belong in this app at all, or is it a separate
  observability surface that this app merely links to?
- Frontend framework within Tauri (the webview UI layer) — deferred until
  there is intent to build.

## Summary

A desktop GUI for Vergil is feasible as a personal power tool, with no
dependency on unsolved technology. The unlock is thin orchestration over
tooling that already exists; the stack is Tauri for lightweight,
cross-platform-capable independence; and the first and most valuable win
is collapsing the split-screen window-management workaround into one
movable, coherent workspace window. The roadmap then offers — but does not
require — progressively more power, with the single discipline that
matters most being that the GUI always tells the truth about the state it
reflects.
