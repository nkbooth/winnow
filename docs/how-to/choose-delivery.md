---
title: "Choose where the digest goes"
description: "Send the daily digest to your terminal, to your phone through ntfy, or to a Matrix room, and understand which can deliver urgent messages."
docType: "howto"
lastVerified: "2026-09-20"
weight: 20
---

# Choose where the digest goes

Configure delivery so the digest reaches you where you will read it. Do this
after your first successful run, once you know you want it daily.

## Prerequisites

- An initialised winnow install with `config.toml`

## Which backend

Two kinds of message leave winnow, and they have different deadlines.

| Backend | Setup | Delivers the digest | Delivers urgent messages |
|---|---|---|---|
| `terminal` | None | Yes, to stdout or a file | **No** |
| `ntfy` | A topic URL | Yes | Yes |
| `matrix` | A homeserver and bot account | Yes | Yes |

An urgent message is an interview request the mailbox listener spotted, or a
failure report from a scheduled run. Those are poorly served by a file you read
tomorrow, which is why `terminal` declares that it cannot push rather than
leaving you to discover it.

## Steps

### Option A: Terminal

The default. Edit `config.toml`:

```toml
[notify]
backend = "terminal"

[notify.terminal]
path = ""          # empty writes to stdout; a path appends to that file
```

### Option B: ntfy

[ntfy](https://ntfy.sh) is a push service that needs no account. Install the
app, subscribe to a topic you invent, and point winnow at it.

Anyone who knows a topic name can read it, so choose something unguessable —
`winnow-7f3a91c4` rather than `winnow-alex`.

```toml
[notify]
backend = "ntfy"

[notify.ntfy]
topic_url = "https://ntfy.sh/winnow-7f3a91c4"
# token_ref = "env:WINNOW_NTFY_TOKEN"    # only for a private ntfy server
```

### Option C: Matrix

Use this if you already run a homeserver. If you do not, ntfy is less work for
the same result.

1. Create a bot account on your homeserver.
2. Create a room and invite the bot.
3. Get an access token for the bot.
4. Set the references:

```toml
[notify]
backend = "matrix"

[notify.matrix]
homeserver_ref = "env:WINNOW_MATRIX_HOMESERVER"
room_id_ref = "env:WINNOW_MATRIX_ROOM_ID"
token_ref = "env:WINNOW_MATRIX_TOKEN"
```

```bash
export WINNOW_MATRIX_HOMESERVER="https://matrix.example.com"
export WINNOW_MATRIX_ROOM_ID='!abcdef:example.com'
export WINNOW_MATRIX_TOKEN="syt_..."
```

Winnow ships `deploy/provision-matrix-bot.py`, which logs the bot in, creates
the room and writes the token, if you would rather not click through a client.

## Verify

```bash
uv run winnow notify "delivery works"
```

The message arrives at the configured destination. A misconfigured backend
fails here, when you are watching, rather than at 07:00 tomorrow inside a timer.

## Troubleshooting

**`'carrier-pigeon' is not a delivery backend`** — a typo in `backend`. Winnow
refuses unknown names instead of falling back to the terminal, because a silent
fallback hides the mistake until you wonder where your notifications went.

**`ntfy needs a topic URL`** — `backend = "ntfy"` with no `topic_url`. Refused
at startup for the same reason.

## Related guides

- [Run it on a server](deploy.md)
- [Settings reference](../reference/configuration.md)
