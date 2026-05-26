# VM Voice-to-Text Design

**Date:** 2026-05-26
**Status:** Approved
**Issue:** #1175

## Problem

Claude Code's built-in `/voice` dictation does not work inside Lima
VMs. The feature uses a native audio module that requires direct
access to the local machine's microphone hardware. When Claude Code
runs inside an SSH session (via `vrg-vm session`), no microphone is
available and the feature silently fails.

Voice-to-text is a critical part of the development workflow — over
90% of input is dictated. Losing it inside VMs would be a significant
productivity regression.

## Rejected Approaches

### Lima VZ audio passthrough

Configure Lima to use `audio.device: "vz"` so Apple's Virtualization
Framework exposes the host mic to the VM as an emulated device.

**Rejected because:** Lima's audio support is experimental. Microphone
input is not wired up in the QEMU driver, and VZ driver audio status
is undocumented. USB passthrough is unsupported. This path depends
entirely on upstream Lima work outside our control and would break
unpredictably across Lima updates.

### PulseAudio/PipeWire network streaming

Run a PulseAudio server on the host and configure the VM to connect
back over the network for audio input.

**Rejected because:** Audio networking is fragile, latency-sensitive,
and requires significant guest-side configuration that conflicts with
the stateless VM model (VMs are rebuilt every three days). The
maintenance burden far exceeds the value.

## Solution: Host-Side Dictation with Superwhisper

The microphone is on the host. Voice-to-text belongs on the host.

**Superwhisper** is a macOS menu bar app that captures audio from the
host mic, provides real-time transcription in a floating overlay, and
injects the finished text into the focused application via clipboard
paste.

### How It Works

1. Developer runs `vrg-vm session` in iTerm2 — inside the VM.
2. Claude Code is running at a prompt inside the VM.
3. Developer presses `Option+Space` (Superwhisper hotkey), speaks,
   sees real-time transcription in the overlay, edits if needed,
   confirms.
4. Superwhisper pastes the text into iTerm2 at the cursor position.
5. The text lands in Claude Code's prompt as if typed. Developer
   presses Enter to send.

### Why Superwhisper

Evaluated six tools against three critical requirements:

| Requirement | Why It Matters |
|---|---|
| Real-time transcription feedback | Technical terms are frequently misrecognized; must see and correct during dictation, not after a batch |
| Edit-before-commit | Ability to fix errors before text is injected into the terminal |
| Works in iTerm2 + SSH sessions | Text must reach Claude Code inside the VM |

| Tool | Real-time | Edit before commit | iTerm2 + SSH |
|---|---|---|---|
| **Superwhisper** | Yes (cloud models) | Yes | Yes |
| open-wispr | No | No | Yes (verified) |
| Voibe | No | No | Unverified |
| OpenQuack | No | No | Yes |
| Wispr Flow | Partial | No | SSH unsupported |
| macOS Dictation | Yes | No | Broken in iTerm2 |

Superwhisper is the only tool that satisfies all three requirements.

### Configuration

**Text injection:** Clipboard paste (default). Superwhisper saves the
current clipboard, pastes the transcription via `Cmd+V`, then restores
the previous clipboard contents. If clipboard conflicts emerge, an
experimental keystroke simulation mode is available (US QWERTY only).

**Model choice:** Cloud models (Nova/Scribe) for real-time
transcription. Local Whisper models (`large-v3-turbo` recommended)
available as offline fallback — functional but batch-only (text
appears after speaking, not during).

**Custom vocabulary:** Populate with domain terms that voice
recognition commonly mangles: `vergil`, `vrg-`, `nerdctl`, `limactl`,
`worktree`, `pyproject`, `virtiofs`, `containerd`, etc.

**AI cleanup level:** Light or None. Higher levels rephrase sentences,
which conflicts with the voice-to-text pipeline where raw intent is
captured first and paraphrased into permanent records separately.

**Activation:** Push-to-talk via `Option+Space` (configurable).
Similar muscle memory to Claude Code's hold-spacebar.

### Vergil Impact

None. Superwhisper is a host-only developer tool. No changes to:

- VM provisioning or Lima configuration
- `vergil.toml` or container setup
- The vergil-tooling package
- CI or validation pipelines

The VM remains stateless and audio-ignorant. Claude Code's `/voice`
inside the VM stays non-functional — Superwhisper replaces it
entirely. On the host (non-VM sessions), either `/voice` or
Superwhisper can be used.

### Limitations

**Cloud dependency for real-time:** Real-time transcription requires
sending audio to third-party services (Deepgram or ElevenLabs). Loss
of internet degrades to local batch mode — still functional but
without live preview.

**Clipboard window:** Brief clipboard usage during paste. The
save/paste/restore cycle is fast but creates a small window for
conflict during active copy-paste workflows.

**Cost:** $8.49/month or $249 lifetime license.

### Fallback

If Superwhisper proves unsatisfactory, **open-wispr** is the zero-cost
alternative: `brew install open-wispr`. Uses keystroke simulation
(no clipboard), verified working with Claude Code in terminals,
100% local via whisper.cpp. Trade-off: no real-time preview and no
edit-before-commit.

### Scope

This is a personal workflow solution — install Superwhisper, configure
it, use it. If the approach proves solid and other developers adopt
it, a section in the identity architecture documentation would
describe the recommended host-side setup. No code changes, no
automation, no infrastructure modifications.
