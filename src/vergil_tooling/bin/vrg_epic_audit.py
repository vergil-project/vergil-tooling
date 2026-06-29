"""Print the epic/task drift audit (read-only safety net).

See :mod:`vergil_tooling.lib.epic_audit`. Surfaces work that slipped through
auto-close so a human can close it (agents cannot close issues).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

from vergil_tooling.lib import epic_audit

_WINDOW_DAYS = 30


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001
    since = (datetime.now(UTC) - timedelta(days=_WINDOW_DAYS)).date().isoformat()
    print(epic_audit.render(epic_audit.task_drift(since), epic_audit.epic_drift()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
