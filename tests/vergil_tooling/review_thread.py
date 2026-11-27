"""The comment thread of the fake GitHub issues the merge-review gate reads.

Shared by the gate tests (``test_agent_gates``) and the batch tests
(``test_batch_mechanics``). A :class:`Thread` posts merge-review records,
exemption requests and approvals onto the issues a
:class:`~tests.vergil_tooling.trust_stubs.FakeItems` answers for, each stamped
one second after the one before it.
"""

from __future__ import annotations

from typing import Any

from tests.vergil_tooling import trust_stubs
from vergil_tooling.lib.merge_review.record import render_record
from vergil_tooling.lib.rulings.approvals import render_exemption_request
from vergil_tooling.lib.trusted_authors import TrustedAuthors

AUTHORS = TrustedAuthors(human=("human",), agents=("human-vergil-user",))
HUMAN: dict[str, str] = {"__typename": "User", "login": "human"}
AGENT: dict[str, str] = {"__typename": "Bot", "login": "human-vergil-user"}
APPROVE = "Looked at it.\nDecision: approve"


def _stamp(tick: int) -> str:
    """A UTC time in 2026-01-01 at minute and second *tick* of the day's first hour."""
    return f"2026-01-01T00:{tick // 60:02d}:{tick % 60:02d}Z"


class Thread:
    """The comments of the issues under test, numbered and timed across all of them.

    Every comment is stamped one second after the one before it, so the order
    comments are added in is the order they were posted in.
    """

    def __init__(self, items: trust_stubs.FakeItems) -> None:
        self.items = items
        self.nodes: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.next_id = 100
        self.clock = 0

    def add(
        self,
        ref: str,
        body: str,
        author: dict[str, str],
        *,
        editor: Any = None,
        created: str | None = None,
    ) -> None:
        repo, _, number = ref.partition("#")
        key = (repo, int(number))
        self.next_id += 1
        self.clock += 1
        posted = created if created is not None else _stamp(self.clock)
        self.nodes.setdefault(key, []).append(
            trust_stubs.comment(self.next_id, body, author, editor=editor, created=posted)
        )
        self.items.items[key] = trust_stubs.item(nodes=self.nodes[key])

    def empty(self, *refs: str) -> None:
        for ref in refs:
            repo, _, number = ref.partition("#")
            key = (repo, int(number))
            self.items.items.setdefault(key, trust_stubs.item(nodes=[]))

    def record(
        self,
        ref: str,
        head: str,
        fingerprint: str,
        outcome: str = "eligible",
        *,
        created: str | None = None,
    ) -> None:
        body = (
            f"Merge review: done: {outcome}; accepted-risk 0\n"
            f"{render_record(head, fingerprint, outcome)}\n\n- R1: fixed\n"
        )
        self.add(ref, body, AGENT, created=created)

    def request(self, ref: str, fingerprint: str) -> None:
        self.add(ref, render_exemption_request("a typo fix", fingerprint), AGENT)

    def approve(self, ref: str) -> None:
        self.add(ref, APPROVE, HUMAN)

    def exempt(self, ref: str, fingerprint: str) -> None:
        self.request(ref, fingerprint)
        self.approve(ref)
