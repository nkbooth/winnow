#!/usr/bin/env bash
# Run a winnow command against a deployed store on a remote host.
#
# The store is SQLite on that box, and SQLite over a network filesystem is a
# corruption hazard, so the process runs where the database is. This is launched
# from the laptop; the container runs on the server.
#
#   deploy/winnow-remote.sh company add "Proton" --board https://...
#   deploy/winnow-remote.sh digest --dry-run
#   deploy/winnow-remote.sh review          # interactive TUI
#
# Two things this handles that are easy to get wrong by hand:
#
#   * A terminal. `tailscale ssh` cannot pass -t, and without a pty Textual gets
#     TERM=dumb and paints nothing usable. Plain ssh reaches the same host over
#     Tailscale SSH and can allocate one.
#   * The drafting assets and the rubric. Both live in the notes repo and are
#     deployed beside the tokens. profile.yaml exists in the notes repo (the editable original,
#     under git) and on the server (what the timer reads). The tunables panel
#     writes to the server's copy because that is the one it can reach, so this
#     script syncs it out before and back after, and the two cannot drift.
set -euo pipefail

# No default: this points at somebody's server and guessing whose is worse
# than asking. Export it once in your shell profile.
HOST="${WINNOW_HOST:?set WINNOW_HOST, e.g. core@your-server}"
IMAGE="${WINNOW_IMAGE:?set WINNOW_IMAGE, e.g. ghcr.io/you/winnow:latest}"
PROFILE="${WINNOW_PROFILE_SRC:-$HOME/Documents/notes/projects/job-search/profile.yaml}"
REMOTE_PROFILE='~/.config/winnow/profile.yaml'

COMMAND="${1:-}"

# `review` is the only command that edits the rubric, and the only one that
# needs a terminal. A timer has no business rewriting the rules it is judged by.
if [ "$COMMAND" = "review" ]; then
  CONFIG_MODE="rw"
  SSH_FLAGS=(-t)
  PODMAN_FLAGS=(-it)
  # The mail token, because `s` sends: it reads the winnow vault and the
  # winnow-mail vault. This is the only place that credential is handed to an
  # interactive process, and the only process with a code path to SMTP. The
  # digest timer's token cannot read the mailbox at all.
  TOKEN="op-token-mail"
else
  CONFIG_MODE="ro"
  SSH_FLAGS=()
  PODMAN_FLAGS=()
  [ -t 0 ] && PODMAN_FLAGS=(-it)
  TOKEN="op-token-digest"
fi

remote() { ssh "${SSH_FLAGS[@]}" "$HOST" "$@"; }

if [ ! -f "$PROFILE" ]; then
  echo "rubric not found at $PROFILE (set WINNOW_PROFILE_SRC)" >&2
  exit 1
fi

# Out: the notes copy is the original, so it wins on the way in.
ssh "$HOST" "cat > $REMOTE_PROFILE" < "$PROFILE"

QUOTED=""
for arg in "$@"; do QUOTED+=" $(printf '%q' "$arg")"; done

set +e
remote "podman run --rm ${PODMAN_FLAGS[*]} \
  --userns=keep-id \
  --network=host \
  --authfile ~/.config/winnow/registry-auth.json \
  -v /var/winnow:/var/lib/winnow:z \
  -v ~/.config/winnow:/etc/winnow:$CONFIG_MODE,z \
  -e OP_SERVICE_ACCOUNT_TOKEN_FILE=/etc/winnow/$TOKEN \
  -e WINNOW_ASSETS=/etc/winnow/assets \
  -e WINNOW_DB=/var/lib/winnow/winnow.db \
  -e WINNOW_PROFILE=/etc/winnow/profile.yaml \
  -e WINNOW_CONFIG=/etc/winnow/config.toml \
  -e TERM=\"${TERM:-xterm-256color}\" \
  $IMAGE$QUOTED"
STATUS=$?
set -e

# Back: whatever the tunables panel changed comes home, every time, with no
# step for anyone to remember.
if [ "$COMMAND" = "review" ]; then
  UPDATED="$(mktemp)"
  trap 'rm -f "$UPDATED"' EXIT
  ssh "$HOST" "cat $REMOTE_PROFILE" > "$UPDATED"
  if ! cmp -s "$PROFILE" "$UPDATED"; then
    cp "$UPDATED" "$PROFILE"
    echo
    echo "rubric changed in the TUI and has been written back to:"
    echo "  $PROFILE"
    git -C "$(dirname "$PROFILE")" --no-pager diff --stat -- "$PROFILE" 2>/dev/null || true
  fi
fi

exit $STATUS
