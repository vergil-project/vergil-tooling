# Human Replies

When a review of a task's change needs the human's decision, the decision
travels as an authenticated comment on the task issue. The agent posts a
**request**. The human posts the **reply** from their own GitHub account, in
their own shell. The agent reads the reply from the issue, checks who wrote
it, and posts a **response**. A caller never relays the decision: under
`epic-implement` it says only that a human response is available, and the
run reads the issue itself.

Two commands carry the protocol:

- [`vrg-ruling`](../reference/dev/ruling.md), run by the agent: `request`,
  `draft`, `read` and `respond`.
- [`vrg-ruling-reply`](../reference/dev/ruling-reply.md), run by the human:
  it posts the reply as the human.

The task issue is the unit of record. A task is closed by one PR, and its
comments record how that PR's change was reviewed and what the human
decided.

## Identities

- **Trusted authors.** Both tools read identity only from the trusted-authors
  configuration ([Trusted authors](account-setup.md#trusted-authors),
  `vrg-trusted-authors`): its `human` login and its `agents` logins. A leading
  `app/` is stripped from every comment author before comparing. Comments by
  any other login are ignored by every check.
- **One human.** The `human` entry must hold exactly one login, the
  **human login**. Both tools refuse when it holds none or several.
- **The agent login** is derived, never asked of GitHub: `<the human
  login>-vergil-user`. `vrg-ruling` runs only in the `user` identity mode, and
  both tools refuse unless the derived login is on the `agents` list.
- **A comment's author is the only source of who wrote it.** The hidden
  marker on a request or response says what kind of comment it is, never who
  wrote it.
- **Agent sessions hold only App credentials.** Every agent session runs in a
  `vrg-vm` VM whose only GitHub credentials are the App's. `vrg-gh`'s fallback
  to ambient `gh` auth finds none there and runs unauthenticated, which cannot
  post, so an agent can never post as the human. This is a stated assumption,
  not something the tools check.

## One round

1. The review's findings, posted by an agent login.
2. **The request:** one comment listing every finding that needs the human,
   each with the agent's recommendation. Posted by `vrg-ruling request`.
3. **The reply:** one plain-text comment covering everything in the request.
   Posted by `vrg-ruling-reply`, as the human.
4. **The response:** what the agent did with each item, and how it read the
   reply. Posted by `vrg-ruling respond`.

One request per round, and one reply per request. Decisions that are not
about review (approving a follow-up issue, preflight stops, problems the
agent cannot resolve) stay outside this channel.

In practice the human and the agent talk the items through in the terminal
first; that conversation is not recorded. Once the human decides, the agent
drafts the reply with `vrg-ruling draft`, which pushes it to the
[draft relay](#the-draft-relay) and prints a `vrg-ruling-reply` command with
the issue and request already filled in. The human reads the draft that
command shows and confirms it, or passes their own text with
`--response-text`.

```text
agent:  vrg-ruling request --issue 'OWNER/REPO#N' --items request.json   -> request ID
agent:  vrg-ruling draft --issue 'OWNER/REPO#N' --request ID --items reply.json
human:  vrg-ruling-reply --issue 'OWNER/REPO#N' --request ID              -> reply posted
agent:  vrg-ruling read --issue 'OWNER/REPO#N' --request ID               -> reply body
agent:  vrg-ruling respond --issue 'OWNER/REPO#N' --request ID --reply ID --items response.json
```

### Where the response states the agent's reading

A reply has no grammar: the skill that reads it interprets it. The response
says, item by item, how the agent read the reply and what it did, so the
reading is on the record beside the reply it came from. A security item's
outcome is also its reading. An unclear reply is answered by a new request,
never by guessing.

## The commands and their exit codes

| Command | What it does | Exit codes |
|---|---|---|
| `vrg-ruling request` | Posts a request from an items file and prints its comment ID. Refuses while the issue has an open exchange, naming that request. | 0, 1, 2 |
| `vrg-ruling draft` | Pushes the reply drafted for the human to the request's draft ref and prints the human's command. Posts nothing. | 0, 1, 2, 4 |
| `vrg-ruling read` | Runs every check on the reply to a request and prints the reply. | 0, 1, 2, 3, 4 |
| `vrg-ruling respond` | Re-runs `read`'s checks and posts the response, recording the request and reply comment IDs. | 0, 1, 2, 4 |
| `vrg-ruling-reply` | Shows the reply, asks for confirmation, and posts it as the human. | 0, 1, 2, 4 |

The codes mean the same in both tools:

| Code | Meaning |
|---|---|
| 0 | Success: the comment is posted, the draft is pushed, or `read` found a valid reply |
| 1 | A refusal or an error, including a declined prompt in `vrg-ruling-reply` |
| 2 | A usage error (bad or missing arguments) |
| 3 | `read` only: no reply yet |
| 4 | A fatal check, named with its comment |

A skill acting on `read` branches on the exit code alone: 0 means act on the
reply, 3 means wait, and 1 or 4 means stop. With `--json`, a valid reply is
one JSON object on stdout and every other outcome leaves stdout empty; see
the [`read` reference](../reference/dev/ruling.md#read).

## The checks

Every check below that fails is a **fatal check**: the tool exits 4 with a
message naming the check and the comment ID, and the human triages it.

### Accepting a reply

`read` accepts a reply only when:

- the request it answers was posted by the derived agent login and carries a
  well-formed request marker;
- the reply's author is the human login;
- the reply is the first trusted comment after the request;
- neither the request nor the reply has been edited;
- the reply is plain text ([the plain-text rule](#the-plain-text-rule)),
  however it was posted, including from the GitHub web page.

### Turn order

The issue is single-threaded. After a request, the next trusted comment must
be the reply. After the reply, the next trusted comment must be the response
to that request and reply. Any other trusted comment in either gap is a turn
violation and fatal: an agent comment between the request and the reply, or
a second human comment before the response. To correct an answer, the human
waits for the response, and the agent asks again in a new request.

### Write-once

Agents never edit a comment; they post a new one. The human never edits one
either. The tools enforce this on the request and the reply: an edited
request or reply is fatal, whoever edited it.

### Deletions

The checks cover only the exchange in hand, never earlier rounds. `read`
fails when the request it was given is gone, and `respond` fails when that
request or its reply is gone. A reply deleted before `read` cannot be told
from no reply, and gives exit 3. The response records both comment IDs as the
record of what it acted on; nothing re-checks earlier responses. Deleting an
untrusted comment is allowed and ignored.

### The audit's ceiling

Other comments in the exchange (the findings, earlier responses) are not
chained or checked, and anyone with write access can delete evidence, up to
a whole issue. GitHub lets only a comment's author or someone with write
access edit or delete it, and shows an edit as edited, but there is no way
to make a comment uneditable. The record is as trustworthy as the people
with write access. The tools guarantee only that the decision the agent acts
on is the human's, unaltered.

## The plain-text rule

Every piece of text either tool handles (item IDs, item text and
recommendations, requests, responses, drafts, `--response-text` and the reply
as read from the issue) is printable ASCII, U+0020 to U+007E, plus newline.
Anything else, tab and carriage return included, is refused, naming the
character's code point and its offset, and is never converted. Plain ASCII
has no invisible or look-alike characters, so what is on screen is all that
is posted. Text that contains the marker prefix `<!-- vrg-ruling`, in any
letter case, is refused too, so text can never carry or imitate a marker.

Item IDs are dot-separated letters and digits, at most 16 characters. An
items file holds at least one item, every ID in it is unique, and every item
text and recommendation is a single line, so each item renders as exactly one
line of the comment.

## Security items

A security finding's details never appear in a public record. Its decision
still goes through the request and the reply, by ID:

- The details reach the human only in the terminal, never from the issue.
- **In a request,** a security item is marked by an explicit `security`
  flag, not recognised by the shape of its ID, and carries only its ID. The
  request shows the fixed text `security finding, details given privately`.
  `vrg-ruling request` refuses a flagged item that carries anything else.
- **In a drafted reply,** a flagged item carries only its ID and a decision:
  `fix`, `file`, `defer` or `wont-fix`. The agent drafts the human's reply
  with security items by ID only, and the human reads the draft before
  confirming it.
- **In a response,** a flagged item carries only its ID and an outcome:
  `fixed`, `filed`, `deferred` or `wont-fix`. `vrg-ruling respond` refuses a
  flagged item that carries anything else.
- **Refusals never echo a refused value.** A refused items file is named by
  item ID (or, when the ID itself is bad, by its position), and extra keys are
  counted, never named, so a detail placed by mistake in an items file never
  reaches the terminal or a log. A plain-text refusal gives the code point and
  offset, not the text.

## The draft relay

The draft reaches the human through GitHub, never a shared filesystem, so
the handoff works the same from a Lima VM and from a cloud VM. It reuses the
relay mechanism of `vrg-pr-workflow report-ready`:

- `vrg-ruling draft` writes the reply as a single-file commit (`reply.md`)
  built with git plumbing and force-pushes it to the reserved ref
  `refs/vergil/ruling-draft/<issue>/<request comment ID>` through the clone's
  `origin`. It never touches a branch, the index or a working tree. It runs
  only in a clone whose `origin` is the issue's repo, and refuses otherwise.
- `vrg-ruling-reply` reads that ref once through the GitHub API, shows its
  content, and posts exactly those bytes on confirmation. The agent pushing
  the ref again while the prompt waits cannot change what is posted.
- The ref is world-readable, like the report-ready relay. A draft holds only
  what the reply will make public anyway, with security items by ID only.
- After posting a reply, `vrg-ruling-reply` deletes the request's draft ref.
  A reply declined at the prompt keeps it, so the command can be run again.
  `vrg-ruling respond` also deletes any draft ref left for its request.

## Posting exactly once

`request`, `respond` and `vrg-ruling-reply` each post with no retry, because
a retried POST that had already landed would post a duplicate, and a
duplicate request or reply is a fatal turn violation. A POST is bounded by a
30-second timeout. After any failed post the tool re-reads the issue, up to
three times:

- if its comment landed, it reports the comment's ID;
- if not, it fails with an error saying the post did not land, and posts
  nothing more;
- if every re-read fails, it says it cannot tell whether the comment landed,
  rather than claiming either.

A re-run settles an uncertain outcome. `respond` finds its landed response,
reports its ID and succeeds without posting; `request` is refused and names
the open request if its post landed; `vrg-ruling-reply` is refused because
the request is already answered.

When the post landed but deleting the draft ref afterwards failed, `respond`
and `vrg-ruling-reply` exit 1 with a message saying the comment is already
posted. A re-run of `respond` posts nothing and retries the cleanup; for a
reply, `respond` deletes the leftover ref.

## Recovery

A fatal check stops the run with exit 4 and a message naming the check and
the comment. The human triages it. The recovery is to start over on a new
issue carrying the same context, patching the spec or plan if needed. The
broken issue is not repaired. Nothing is built for recovery beyond detecting
and stopping; it is expected to be rare.

## Related

- [`vrg-ruling` reference](../reference/dev/ruling.md)
- [`vrg-ruling-reply` reference](../reference/dev/ruling-reply.md)
- [Identity Setup: trusted authors](account-setup.md#trusted-authors)
- [Security Records](security-records.md)
