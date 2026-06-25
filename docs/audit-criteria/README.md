# Audit criteria (reference)

These six markdown files are the judgment criteria a future API-driven agentic
review would apply to a PR:

- `commit-message-fidelity.md`
- `pr-description-fidelity.md`
- `docstring-accuracy.md`
- `site-docs-reflection.md`
- `scope-coherence.md`
- `test-adequacy.md`

They were authored for the interactive dual-agent audit loop, which was removed
in #1872 (see `docs/specs/2026-06-25-remove-audit-loop-design.md`). They are
**reference material only** — no running code reads them today. When the
API-driven review is built, it can reuse them as its prompt set.
