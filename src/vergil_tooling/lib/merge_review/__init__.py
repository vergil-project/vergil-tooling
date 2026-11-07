"""merge-review, the pre-PR reviewer's mechanical half (epic vergil-project/.github#881).

The protocol is ``docs/specs/merge-review-protocol.md``; it is authoritative
for every schema and rule here. The modules:

- :mod:`.schema` holds the constants the request builder and the result check
  share: the schema names, the closed value sets, the step rule and each
  step's finding ID prefix;
- :mod:`.result` parses a reviewer's reply and runs the result check on it,
  against the request it answers (``vrg-merge-review-check``).
"""

from __future__ import annotations
