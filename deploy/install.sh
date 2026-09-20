#!/usr/bin/env bash
# Install winnow's units on the target host.
#
# Rootless user units. Does not
# create the vault items or the service accounts: those are provisioned by
# hand, deliberately, because the credential split is the one property of this
# deployment worth getting wrong slowly rather than fast. See deploy/README.md.
set -euo pipefail

HOST="${1:?usage: install.sh user@host [registry-user]}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE="${WINNOW_PROFILE_SRC:-$HOME/Documents/notes/projects/job-search/profile.yaml}"

run() { tailscale ssh "$HOST" "$@"; }

echo "==> directories"
run '
  mkdir -p ~/.config/winnow ~/.config/containers/systemd ~/.config/systemd/user &&
  chmod 0700 ~/.config/winnow &&
  sudo mkdir -p /var/winnow && sudo chown core:core /var/winnow
'

echo "==> credentials"
for item in op-token-digest op-token-mail; do
  op read "op://winnow/${item}/credential" |
    run "cat > ~/.config/winnow/${item} && chmod 0600 ~/.config/winnow/${item}"
done

op read op://winnow/ghcr-pull/credential |
  run "podman login ghcr.io -u ${REGISTRY_USER:?set REGISTRY_USER} --password-stdin --authfile ~/.config/winnow/registry-auth.json"

echo "==> rubric"
# The rubric is also where the review TUI writes tunables back, so this copy is
# the deployed one and the notes repo keeps the editable original.
run 'cat > ~/.config/winnow/profile.yaml' < "$PROFILE"

echo "==> drafting assets"
# The only material a cover letter may make factual claims from. Deployed
# rather than fetched, so drafting cannot reach anything it was not given.
NOTES="$(dirname "$PROFILE")"
run 'mkdir -p ~/.config/winnow/assets'
for asset in cover-letter-assets.md work-inventory.md email-signature.md \
             resume-a-business-systems.md resume-b-consulting.md resume-c-technical.md; do
  run "cat > ~/.config/winnow/assets/$asset" < "$NOTES/$asset"
done

echo "==> units"
run 'cat > ~/.config/containers/systemd/winnow-digest.container' < "$REPO/deploy/winnow-digest.container"
run 'cat > ~/.config/containers/systemd/winnow-listener.container' < "$REPO/deploy/winnow-listener.container"
run 'cat > ~/.config/systemd/user/winnow-digest.timer' < "$REPO/deploy/winnow-digest.timer"
# Reports a unit dying before it could report anything itself.
run 'cat > ~/.config/systemd/user/winnow-failure@.service' < "$REPO/deploy/winnow-failure@.service"

echo "==> enable"
run '
  systemctl --user daemon-reload &&
  systemctl --user enable --now winnow-digest.timer &&
  systemctl --user start winnow-listener.service &&
  systemctl --user list-timers winnow-digest.timer --no-pager
'

cat <<'NEXT'

Installed. Boards still have to be added before a poll has anything to do:

  ssh "$HOST" \
    'podman exec winnow-digest winnow company add "Tailscale" \
       --board https://boards.greenhouse.io/tailscale'

...or run the same via `podman run --rm` with the same mounts. See the seed
list in companies.md; resolution is manual on purpose.
NEXT
