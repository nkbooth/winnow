---
title: "Settings"
description: "Every key in config.toml: mail hosts, delivery backends, credential references, and the optional aggregator."
docType: "reference"
lastVerified: "2026-09-20"
weight: 10
---

# Settings

`config.toml` holds infrastructure: where things live and how to reach them.
Your career preferences live in [`profile.yaml`](rubric.md) instead, because the
two change on different schedules.

Default location: `~/.config/winnow/config.toml`. Override with `WINNOW_CONFIG`.

A missing file is not an error. Every key has a default describing a machine
where nothing is set up yet.

## Credential references

Winnow never stores a secret. Every `*_ref` key holds a reference that names its
own backend by scheme.

| Scheme | Resolves to | Use when |
|---|---|---|
| `env:NAME` | An environment variable | Default. Nothing to install. |
| `file:/path` | The file's contents, stripped | Podman or Kubernetes secrets, systemd credentials |
| `op://vault/item/field` | 1Password, through its CLI | You already run 1Password; scoping per service account is possible |

A bare value is refused. Accepting one would make the easiest thing to write in
this file also the thing that commits a password to a repository.

Absent and empty are both errors, raised at resolution. A silently empty
credential becomes an authentication error much later, far from its cause.

## `[identity]`

| Key | Type | Default | Description |
|---|---|---|---|
| `contact` | string | `""` | Sent in the `User-Agent` to every board polled. Unset renders as `unconfigured`. |

## `[llm]`

| Key | Type | Default | Description |
|---|---|---|---|
| `api_key_ref` | string | `env:ANTHROPIC_API_KEY` | Anthropic key. Required for scoring and drafting. |

## `[notify]`

| Key | Type | Default | Description |
|---|---|---|---|
| `backend` | enum | `terminal` | `terminal`, `ntfy`, or `matrix`. An unknown value is refused. |

### `[notify.terminal]`

| Key | Type | Default | Description |
|---|---|---|---|
| `path` | string | `""` | Empty writes to stdout. A path appends, dated. |

### `[notify.ntfy]`

| Key | Type | Default | Description |
|---|---|---|---|
| `topic_url` | string | `""` | Required when `backend = "ntfy"`. |
| `token_ref` | string | `""` | Only for private ntfy servers. |

### `[notify.matrix]`

| Key | Type | Default | Description |
|---|---|---|---|
| `homeserver_ref` | string | `env:WINNOW_MATRIX_HOMESERVER` | Base URL of the homeserver. |
| `room_id_ref` | string | `env:WINNOW_MATRIX_ROOM_ID` | Room the digest posts to. |
| `token_ref` | string | `env:WINNOW_MATRIX_TOKEN` | Bot access token. |

## `[mail]`

Optional in full. Without it winnow still polls, scores and digests. What stops
is reading employer replies and sending a draft.

| Key | Type | Default | Description |
|---|---|---|---|
| `imap_host` | string | `""` | Mailbox the listener watches. |
| `imap_port` | integer | `993` | IMAP over TLS. |
| `smtp_host` | string | `""` | Used only by the review screen, never by a scheduled run. |
| `smtp_port` | integer | `587` | STARTTLS. |
| `mailbox` | string | `INBOX` | Folder watched for replies. |
| `sent_folder` | string | `Sent` | Folder read to recover applications sent by hand. |
| `message_id_domain` | string | `localhost` | Domain part of generated `Message-ID` headers. Set it: replies are correlated by thread. |
| `username_ref` | string | `env:WINNOW_MAIL_USERNAME` | |
| `password_ref` | string | `env:WINNOW_MAIL_PASSWORD` | |

## `[adzuna]`

Optional. Used only by `winnow discover`, which names employers not yet tracked.

| Key | Type | Default | Description |
|---|---|---|---|
| `app_id_ref` | string | `""` | Adzuna application ID. |
| `app_key_ref` | string | `""` | Adzuna application key. |

## Constraints

- `backend` must be one of the three listed names.
- `backend = "ntfy"` requires `topic_url`; the notifier refuses to build without it.
- Ports must be integers.
- Settings are read once per process. A file edited mid-run takes effect next run.

## Examples

Minimal, terminal only:

```toml
[identity]
contact = "you@example.com"
```

Phone notifications and reply tracking:

```toml
[identity]
contact = "you@example.com"

[notify]
backend = "ntfy"

[notify.ntfy]
topic_url = "https://ntfy.sh/winnow-7f3a91c4"

[mail]
imap_host = "imap.example.com"
smtp_host = "smtp.example.com"
message_id_domain = "example.com"
```

## See also

- [Choose where the digest goes](../how-to/choose-delivery.md)
- [Rubric reference](rubric.md)
