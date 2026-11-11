# vrg-scope-change

**Installed as:** `vrg-scope-change` (Python console script)

**Source:** `src/vergil_tooling/bin/vrg_scope_change.py` (library:
`src/vergil_tooling/lib/rulings/`)

Posts a **scope change** on a task issue, **as the human**: a maintainer's
addition to the task's scope, a narrowing of it, or a ruling on an item
awaiting one. Merge review counts only scope the human posted as
themselves, because the implementing agent shares the agent identity and
could otherwise widen its own scope. For the comment's form and how it is
recognised, see
[Human Replies: Scope changes](../../guides/human-replies.md#scope-changes);
the agent drafts the text with
[`vrg-ruling draft-scope`](ruling.md#draft-scope).

It is a human-only command, like [`vrg-ruling-reply`](ruling-reply.md). The
human runs it in their own shell on the host, logged in to `gh` as
themselves; never through `!` in an agent session, whose shell carries the
agent's identity.

## Usage

```text
vrg-scope-change --issue 'OWNER/REPO#N' [--text TEXT]
```

| Flag | Meaning |
|---|---|
| `--issue` | The task issue, `owner/repo#N` |
| `--text` | Post this text instead of the agent's draft (printable ASCII and newline only; the header is added for you) |

Normally the human runs the command `vrg-ruling draft-scope` prints, which
posts the agent's draft. `--text` posts the human's own text instead.

## The comment

The posted comment is a fixed header line, a blank line and the text:

```text
Scope change

<the text>
```

Leading and trailing blank lines of the text are dropped and the comment
ends with one newline. The text may run over several lines; it must not
start with the header itself.

## What it does

In order, it:

1. refuses in an agent identity mode, after parsing arguments (so `-h` or
   `--help` prints the full usage to any identity, and exits 0);
2. refuses unless stdin and stdout are both interactive terminals;
3. refuses unless the trusted-authors configuration lists exactly one human
   login and the login its credentials post as (`gh api user`) is that login;
4. refuses unless the derived agent login, `<the human login>-vergil-user`,
   is on the `agents` list;
5. reads the issue and refuses while it has an **open ruling exchange** (a
   request that no response names yet): between a request and its response
   the only human comment allowed is the reply, so a scope change there
   would be read as the reply or break the turn order;
6. reads the text **once**: `--text`, else the agent's draft from the
   issue's scope draft ref, `refs/vergil/scope-draft/<issue>` (no draft is a
   refusal);
7. refuses text that is not plain text (printable ASCII, U+0020 to U+007E,
   plus newline, with no `<!-- vrg-ruling` marker prefix), that is blank, or
   that starts with the header, and renders the comment;
8. if the human has already posted exactly this comment on the issue, prints
   that comment's ID, deletes the draft ref when the text came from it,
   posts nothing and exits 0;
9. prints the comment exactly as it will be posted, between delimiter lines,
   with its byte length and SHA-256, and asks
   `Post this scope change as <login>?`;
10. on yes, reads the issue again and refuses, posting nothing, if its
    trusted comments changed while the prompt waited;
11. posts exactly the bytes it showed, once, prints the comment's ID on
    stdout, and deletes the scope draft ref when the text came from it.

On no, it exits 1, posts nothing and keeps the draft, so the command can be
run again. A draft pushed again while the prompt waits is never what is
posted: the text is read once, in step 6.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | The scope change is posted, or was already posted |
| 1 | A refusal, a declined prompt or an error; nothing was posted unless the message says so |
| 2 | A usage error |

## Failures after the post

The post is never retried. If it fails, the issue is re-read: a comment that
landed is reported by its ID; otherwise the error says it did not land
(re-running is safe) or that the outcome cannot be told. A re-run settles
the last case: if the comment landed, step 8 finds it and posts nothing.

If the comment is posted but deleting the draft ref fails, it exits 1 with a
message saying the scope change is already posted. Re-running the command
finds the posted comment, posts nothing and retries the deletion.
