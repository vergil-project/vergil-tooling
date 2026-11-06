# vrg-ruling-reply

**Installed as:** `vrg-ruling-reply` (Python console script)

**Source:** `src/vergil_tooling/bin/vrg_ruling_reply.py` (library:
`src/vergil_tooling/lib/rulings/`)

The human's side of an authenticated ruling on a task issue: it posts the
human's **reply** to the agent's request, **as the human**. For the protocol,
the checks and recovery, see [Human Replies](../../guides/human-replies.md);
the agent's side is [`vrg-ruling`](ruling.md).

It is a human-only command. The human runs it in their own shell on the
host, logged in to `gh` as themselves, the way `vrg-submit-pr` is run; never
through `!` in an agent session, whose shell carries the agent's identity.

## Usage

```text
vrg-ruling-reply --issue 'OWNER/REPO#N' --request ID [--response-text TEXT]
```

| Flag | Meaning |
|---|---|
| `--issue` | The task issue, `owner/repo#N` |
| `--request` | The request's comment ID, a positive integer with no leading zero |
| `--response-text` | Post this text instead of the agent's draft (printable ASCII and newline only) |

Normally the human runs the command `vrg-ruling draft` prints, which posts
the agent's draft. `--response-text` posts the human's own text instead.

## What it does

In order, it:

1. refuses in an agent identity mode, before parsing arguments (with `-h` or
   `--help` it prints a short note saying the command is human-only, and
   exits 0);
2. refuses unless stdin and stdout are both interactive terminals;
3. refuses unless the trusted-authors configuration lists exactly one human
   login and the login its credentials post as (`gh api user`) is that login;
4. refuses unless the derived agent login, `<the human login>-vergil-user`,
   is on the `agents` list;
5. reads the issue and refuses unless the request is the issue's open request
   and has no reply yet (it is already answered, or the turn has moved on); a
   broken exchange is a fatal check;
6. reads the reply **once**: `--response-text`, else the agent's draft from
   the request's draft ref, `refs/vergil/ruling-draft/<issue>/<request ID>`
   (no draft is a refusal);
7. refuses a reply that is not plain text (printable ASCII, U+0020 to
   U+007E, plus newline, with no `<!-- vrg-ruling` marker prefix) or that is
   blank;
8. prints the reply exactly as it will be posted, between delimiter lines,
   with its byte length and SHA-256, and asks `Post this reply as <login>?`;
9. on yes, reads the issue again and refuses, posting nothing, if its trusted
   comments changed while the prompt waited or the checks no longer hold;
10. posts exactly the bytes it showed, once, prints the reply's comment ID,
    and deletes the request's draft ref.

On no, it exits 1, posts nothing and keeps the draft, so the command can be
run again.

A draft pushed again while the prompt waits is never what is posted: the
reply is read once, in step 6, and only those bytes are posted.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | The reply is posted |
| 1 | A refusal, a declined prompt or an error; nothing was posted unless the message says so |
| 2 | A usage error |
| 4 | A fatal turn check, named with its comment |

## Failures after the post

The post is never retried. If it fails, the issue is re-read: a reply that
landed is reported by its ID; otherwise the error says it did not land
(re-running is safe) or that the outcome cannot be told. A re-run settles the
last case, because it is refused once the request is answered.

If the reply is posted but deleting the draft ref fails, it exits 1 with a
message saying the reply is already posted. A re-run is refused now that the
request is answered; `vrg-ruling respond` deletes the leftover ref.
