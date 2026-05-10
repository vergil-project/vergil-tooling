# Markdown Authoring Standards

## Purpose

Define consistent Markdown conventions for clarity, durability, and easy
navigation across all documentation.

## Scope

These standards apply to all documentation Markdown files. AI tooling
instruction files (such as `AGENTS.md`) are exempt unless they explicitly
state otherwise.

## File naming and placement

- Place documentation files under `docs/` unless a top-level file is required.
- Use descriptive, stable filenames in `kebab-case.md`.
- Avoid renaming files without a compelling reason.

## Structure and headings

- Use a single H1 title (`#`) at the top of the file.
- Use ATX headings (`##`, `###`) only; do not use Setext headings.
- Keep heading titles short, specific, and in sentence case.
- Avoid skipping heading levels (no `####` under `##`).

## Table of Contents rules

- Include a `## Table of Contents` section near the top of every document.
- Place it after the title and any brief preface block.
- List all `##` and `###` headings in order, excluding the Table of Contents.
- Use a bullet list with two-space indentation for `###` entries.
- Use GitHub-style anchor links.
- **Exception**: Pages built by documentation site generators (MkDocs, Sphinx)
  are exempt from the Table of Contents requirement because those tools
  provide integrated navigation. The `markdown-standards` validator detects
  MkDocs doc trees automatically when `mkdocs.yml` exists at the repository
  root.

## Formatting conventions

- Use blank lines around headings and lists.
- Prefer short paragraphs and bullets over dense blocks of text.
- Use bold only for short emphasis; avoid bolding entire sentences.
- Use italics sparingly for terms or titles, not emphasis.
- Avoid emojis unless explicitly required.

## Links and references

- Prefer relative links for repository content.
- Keep link text descriptive; avoid "click here."
- When referencing file paths in prose, use backticks.

## Code blocks

- Use fenced code blocks with a language tag when possible.
- Keep code examples minimal and focused on the rule being explained.
- Do not include output that is environment-specific unless required.

## Line length

Lines must not exceed 100 characters, matching the `ruff` `line-length`
setting. Tables and code blocks are exempt. See
[Markdown Validation](../reference/lint/markdown-standards.md) for the
full markdownlint configuration.

## Validation

- All repositories must run markdownlint for documentation validation.
- Markdownlint is the minimum baseline. Repositories may add additional checks.
- The canonical markdownlint config is bundled in standard-tooling and applied
  automatically by `st-validate`. Consuming repos do not need a local config.
- Markdownlint must be installed locally by humans; agents must not download or
  install it on demand. CI workflows may provision it explicitly.

## Maintenance

- Update the Table of Contents when headings change (non-docsite files only).
- Keep documents current; stale guidance should be revised or removed.
