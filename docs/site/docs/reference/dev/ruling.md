# vrg-ruling

**Installed as:** `vrg-ruling` (Python console script)

**Source:** `src/vergil_tooling/bin/vrg_ruling.py` (library:
`src/vergil_tooling/lib/rulings/`)

The agent's side of an authenticated ruling on a task issue. It posts the
**request**, relays the reply drafted for the human, reads the human's
**reply** with every turn check, and posts the **response**. Each comment is
posted exactly once. For the protocol, the checks and recovery, see
[Human Replies](../../guides/human-replies.md); the human's side is
[`vrg-ruling-reply`](ruling-reply.md).

## Usage

```text
vrg-ruling request --issue OWNER/REPO#N --items FILE
vrg-ruling draft   --issue OWNER/REPO#N --request ID --items FILE
vrg-ruling read    --issue OWNER/REPO#N --request ID [--json]
vrg-ruling respond --issue OWNER/REPO#N --request ID --reply ID --items FILE
```

`--issue` is the task issue as `owner/repo#N`. `--request` and `--reply` are
comment IDs, positive integers with no leading zero.

## Preconditions

Every subcommand refuses (exit 1) unless:

- the session is in the `user` identity mode;
- the trusted-authors configuration lists exactly one human login;
- the derived agent login, `<the human login>-vergil-user`, is on its
  `agents` list.

Every GitHub call is scoped to the issue's owner, whatever the clone's
`origin` is. `draft` alone also needs a clone whose `origin` is the issue's
repo.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success, or `read` found a valid reply |
| 1 | Any other refusal or error |
| 2 | A usage error |
| 3 | `read` only: no reply yet |
| 4 | A fatal check, named with its comment |

## Items files

`request`, `draft` and `respond` each read an items file: a JSON array with
at least one object, one per item. Every item has:

| Key | Meaning |
|---|---|
| `id` | Dot-separated letters and digits, at most 16 characters; unique within the file |
| `security` | `true` or `false`; a security item is marked by this flag, never by its ID |

The other keys depend on the file and the flag. An item may carry only the
keys listed for it, and every one of them is required:

| File | Not flagged | Flagged |
|---|---|---|
| Request | `text` (the question), `recommendation` | nothing else |
| Reply (for `draft`) | `text` (the decision in prose) | `decision`: `fix`, `file`, `defer` or `wont-fix` |
| Response | `text` (how the agent read the reply and what it did) | `outcome`: `fixed`, `filed`, `deferred` or `wont-fix` |

Every `text` and `recommendation` is a non-empty single line of printable
ASCII (U+0020 to U+007E) that does not contain the marker prefix
`<!-- vrg-ruling`. Each item renders as exactly one line:

| Comment | Line per item |
|---|---|
| Request | `<id>: <text> Recommendation: <recommendation>`, or `<id>: security finding, details given privately` |
| Reply | `<id>: <text>` or `<id>: <decision>` |
| Response | `<id>: <text>` or `<id>: <outcome>` |

A request and a response end with a blank line and their hidden marker,
`<!-- vrg-ruling kind=request -->` or
`<!-- vrg-ruling kind=response request=<ID> reply=<ID> -->`. A reply carries
no marker.

A refusal names the item by its ID, or by its position when the ID itself is
bad. It never echoes a refused value: extra keys are counted, not named, and
a plain-text refusal gives the character's code point and offset, not the
text.

Example request items file:

```json
[
  {"id": "F1", "security": false,
   "text": "The retry loop has no upper bound.",
   "recommendation": "fix: cap it at three attempts."},
  {"id": "F2", "security": true}
]
```

## request

Posts a request from a request items file and prints its comment ID on
stdout.

It refuses while the issue has an **open request** (an earlier request that
no response names yet), naming that request's ID and the `read` command for
it, so a resumed run can pick it up instead.

The post is never retried. If it fails, the issue is re-read: a request that
landed is reported by its ID; otherwise the error says it did not land
(re-running is safe) or that the outcome cannot be told. A re-run settles the
last case, because it is refused and names the open request if the post
landed.

## draft

Builds the human's reply from a reply items file, pushes it to the request's
draft ref, and prints the human's command on stdout:

```text
vrg-ruling-reply --issue OWNER/REPO#N --request ID
```

In order, it checks the items and the rendered reply, refuses unless the
clone's `origin` pushes to the issue's repo, and runs `read`'s checks on the
request, refusing once the request has a reply. Only then does it write the
reply as a single-file commit (`reply.md`) with git plumbing and force-push it
to `refs/vergil/ruling-draft/<issue>/<request ID>` on `origin`. It never
touches a branch, the index or the working tree, and it posts no comment.
Running it again replaces the draft.

The ref is world-readable, so a draft holds only what the reply will make
public anyway, with security items by ID and decision only.

## read

Reads the human's reply to a request, running every turn check (see
[The checks](../../guides/human-replies.md#the-checks)).

| Outcome | Exit | stdout | stderr |
|---|---|---|---|
| A valid reply | 0 | The reply body (or the JSON object with `--json`) | `vrg-ruling: reply <reply ID> to request <request ID> on OWNER/REPO#N` |
| No reply yet | 3 | Empty | `vrg-ruling: no reply yet to request <ID> on OWNER/REPO#N` |
| A fatal check | 4 | Empty | The check and the comment |
| Any other refusal or error | 1 | Empty | The reason |

Without `--json`, stdout on a valid reply is the reply body alone, and the
reply's comment ID is only on stderr.

### --json

With `--json`, stdout on a valid reply (exit 0) is exactly one JSON object:

```json
{"request": 4123456789, "reply": 4123456790, "body": "F1: fix it\nF2: defer"}
```

| Key | Value |
|---|---|
| `request` | The request's comment ID, an integer |
| `reply` | The reply's comment ID, an integer |
| `body` | The reply body, a string |

On every other outcome (exit 1, 3 or 4), stdout is empty; the exit code is
the signal.

### Fatal checks

A fatal check exits 4 with `fatal check '<check>' on comment <ID>: <detail>`
on stderr. `draft` and `respond` run the same checks and fail the same way.

| Check | Fails when |
|---|---|
| `deleted` | The request is not on the issue; for `respond`, the reply is not the reply to the request |
| `request-author` | The request is not a request posted by the derived agent login |
| `marker` | The request, or the agent comment after the reply, has a malformed marker |
| `edited` | The request or the reply has been edited |
| `turn` | The first trusted comment after the request is not by the human; the comment after the reply is not the response to this request and reply; or a trusted comment follows the response |
| `reply-text` | The reply is not plain text |

## respond

Posts the response to a request and its reply from a response items file,
and prints the response's comment ID on stdout. The response's marker
records the request and reply comment IDs.

It re-runs `read`'s checks first and refuses unless the reply is still the
latest trusted comment. The one exception is a re-run that finds its own
well-formed response to that request and reply already landed after the
reply: it prints that response's ID, posts nothing and exits 0.

Either way, it then deletes the request's draft ref, if one is left; an
absent ref is not an error. If the deletion fails, it exits 1 with a message
saying the response is already posted. Re-running `respond` with the same
arguments posts nothing and retries the deletion.

A failed post is handled as for `request`. If the outcome cannot be told, a
re-run settles it: it finds the landed response and succeeds without
posting.
