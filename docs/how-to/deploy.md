---
title: "Run it on a server"
description: "Run winnow unattended with systemd and Podman, with credentials scoped so a scheduled run cannot send mail."
docType: "howto"
lastVerified: "2026-09-20"
weight: 30
---

# Run it on a server

Move winnow off your laptop so the digest arrives whether or not you are at a
machine. This guide uses rootless Podman and systemd user units, which is what
the reference deployment runs.

## Prerequisites

- A Linux host with Podman 4.4 or later and lingering enabled for your user
  (`loginctl enable-linger $USER`)
- A working winnow install with a rubric and settings
- A delivery backend that can push — see [Choose where the digest goes](choose-delivery.md)

## The credential boundary

Read this before copying unit files.

Two processes run unattended: the digest timer and the mailbox listener.
Neither may hold a credential capable of sending mail. That is not a policy in
code — the listener has no SMTP host and no sending path, and the timer is
given references only to the things it needs.

Only `winnow review`, which a human is sitting in front of, resolves the
credential that can send. If your secret backend supports scoping, scope it:
1Password service accounts scope to vaults, so a digest token can be made
physically unable to read the mailbox. With `env:` or `file:` references, the
boundary is structural rather than credential-scoped.

## Steps

### Step 1: Build or pull an image

```bash
podman build -t winnow:local .
```

### Step 2: Put the configuration where the units expect it

```bash
mkdir -p ~/.config/winnow
cp profile.yaml config.toml ~/.config/winnow/
cp -r assets ~/.config/winnow/
```

### Step 3: Install the units

Copy the quadlets from `deploy/` into `~/.config/containers/systemd/` and edit
the `Image=` line in each to match what you built:

```bash
cp deploy/winnow-*.container deploy/winnow-*.timer deploy/winnow-failure@.service \
   ~/.config/containers/systemd/
systemctl --user daemon-reload
```

Three units matter:

| Unit | Role |
|---|---|
| `winnow-digest.timer` | Fires the daily run |
| `winnow-listener.service` | Watches the mailbox continuously |
| `winnow-failure@.service` | `OnFailure` target that reports a failed run |

### Step 4: Start them

```bash
systemctl --user start winnow-digest.timer
systemctl --user start winnow-listener.service
```

## Verify

```bash
systemctl --user list-timers winnow-digest.timer
systemctl --user show winnow-listener.service -p Result -p ExecMainStatus
```

The timer lists a next firing. The listener reports `Result=success` and
`ExecMainStatus=0`.

Force one run rather than waiting:

```bash
systemctl --user start winnow-digest.service
journalctl --user -u winnow-digest.service -n 30
```

## Troubleshooting

**The listener is killed on restart, and `OnFailure` reports a failure.** The
container's entrypoint is the process, so Python runs as PID 1, and the kernel
applies no default action to a signal PID 1 has no handler for. Winnow installs
a SIGTERM handler for this. If you have wrapped the entrypoint in a shell,
signals stop at the shell.

**`op` silently falls back to the desktop app.** The 1Password CLI reads its
service-account token from the environment as a value and has no file
equivalent. Given an unreadable token file it does not fail — it tries the
desktop app, which is not running. Winnow reads the file itself for this reason.

**The container cannot read its own volume.** Rootless UID mapping. Use
`UserNS=keep-id`, and `:z` rather than `:Z` when two containers share a mount —
`:Z` assigns a private SELinux category that the second container cannot read.

## Related guides

- [Choose where the digest goes](choose-delivery.md)
- [Settings reference](../reference/configuration.md)
