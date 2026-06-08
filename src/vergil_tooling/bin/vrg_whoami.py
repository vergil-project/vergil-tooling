"""Print the resolved Vergil identity role.

``vrg-whoami`` answers "who am I?" definitively by wrapping the
authoritative resolver in :mod:`vergil_tooling.lib.identity_mode`. Every
consumer — provisioning scripts, agents, humans at a prompt, and the
``vrg-*`` wrappers — should ask this tool rather than hand-rolling a
partial check against a single signal (most commonly ``$VRG_IDENTITY_MODE``,
which is only the first of five fallback steps; an unset value means "fall
through," not "default to HUMAN").

Modes:

- ``vrg-whoami`` / ``vrg-whoami --mode`` — print the resolved role as a
  single token (``human`` | ``user`` | ``audit``), suitable for
  ``export VRG_IDENTITY_MODE="$(vrg-whoami --mode)"``.
- ``vrg-whoami --explain`` — print the resolved role, the signal it
  resolved from, and every signal's state; warn on stderr when present
  signals disagree (the condition that precedes a misread).
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib.identity_mode import Resolution, Signal, resolve

_SIGNAL_LABELS = {
    Signal.ENV_VAR: "environment variable",
    Signal.MODE_FILE: "mode file",
    Signal.APP_KEY: "app credential",
    Signal.APP_ID: "app id",
    Signal.DEFAULT: "default (no signal present)",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print the resolved Vergil identity role.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--mode",
        action="store_true",
        help="Emit only the role token (machine-readable; the default already does this).",
    )
    group.add_argument(
        "--explain",
        action="store_true",
        help="Show the resolving signal and every signal's state; warn on disagreement.",
    )
    return parser.parse_args(argv)


def _reading_state(present: bool, implied_value: str | None) -> str:
    if not present:
        return "absent"
    if implied_value is None:
        return "present (unrecognized value)"
    return implied_value


def _explain(resolution: Resolution) -> int:
    label = _SIGNAL_LABELS[resolution.resolved_by]
    print(f"role:          {resolution.mode.value}")
    print(f"resolved from: {label}")
    print("signals (in fallback order):")
    for reading in resolution.readings:
        marker = " <-- resolved" if reading.signal is resolution.resolved_by else ""
        implied_value = reading.implied.value if reading.implied is not None else None
        state = _reading_state(reading.present, implied_value)
        print(f"  {_SIGNAL_LABELS[reading.signal]} ({reading.detail}): {state}{marker}")

    if resolution.disagreement:
        conflicts = ", ".join(
            f"{_SIGNAL_LABELS[r.signal]}={r.implied.value}"
            for r in resolution.readings
            if r.present and r.implied is not None
        )
        print(
            f"WARNING: identity signals disagree ({conflicts}); resolved to "
            f"'{resolution.mode.value}' via {label}. Reconcile the signals — "
            "disagreement is the condition that precedes a misread.",
            file=sys.stderr,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    resolution = resolve()

    if args.explain:
        return _explain(resolution)

    print(resolution.mode.value)
    return 0


if __name__ == "__main__":
    sys.exit(main())
