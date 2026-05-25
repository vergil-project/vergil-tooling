#!/usr/bin/env bash
# Claude Code PreToolUse hook shim.
# Delegates to vrg-hook-guard if available; falls back to a
# jq-based git/gh check that hard-denies when vergil-tooling
# is not installed.
set -euo pipefail

if command -v vrg-hook-guard &>/dev/null; then
  exec vrg-hook-guard
fi

input=$(cat)
command=$(echo "$input" | jq -r '.tool_input.command // empty')

if echo "$command" | grep -qE '(^|[^-])\bgit\b'; then
  jq -n '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: "vergil-tooling is not available. This repository requires a correctly configured environment — all git/gh operations are blocked until resolved."
    }
  }'
  exit 0
fi

if echo "$command" | grep -qE '(^|[^-])\bgh\b'; then
  jq -n '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: "vergil-tooling is not available. This repository requires a correctly configured environment — all git/gh operations are blocked until resolved."
    }
  }'
  exit 0
fi

exit 0
