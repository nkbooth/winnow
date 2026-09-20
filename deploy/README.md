# Deployment files

Systemd quadlets and helper scripts for running winnow unattended with rootless
Podman.

See [docs/how-to/deploy.md](../docs/how-to/deploy.md) for the guide. These files
are the raw material it refers to.

| File | Role |
|---|---|
| `winnow-digest.container` | The daily run |
| `winnow-digest.timer` | What fires it |
| `winnow-listener.container` | The mailbox watcher |
| `winnow-failure@.service` | `OnFailure` target, reports a failed run |
| `install.sh` | Copies units and credentials to a host |
| `winnow-remote.sh` | Runs one winnow command against a remote store |
| `provision-matrix-bot.py` | Creates a Matrix bot account and room |

Every `Image=` line reads `ghcr.io/CHANGEME/winnow:latest`. Change it to your
own registry path before installing. `install.sh` and `winnow-remote.sh` take
the host and image from the environment and refuse to run without them, rather
than defaulting to somebody else's server.
