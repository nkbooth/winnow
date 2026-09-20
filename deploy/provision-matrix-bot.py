#!/usr/bin/env python3
"""Provision winnow's Matrix identity. Run once, from the laptop.

Prerequisites, in order:

  1. The account exists. Registration is disabled on the homeserver, so it is
     created from Conduit's admin room — send `help` there for the exact
     command your Conduit version wants.
  2. Its password is in 1Password at op://winnow/matrix-bot/password, and an
     item named `matrix-bot` exists in the `winnow` vault to hold the rest.

Then, from the laptop — `op` lives here, not in the dev container, and a scratch
environment keeps the container's venv untouched:

    UV_PROJECT_ENVIRONMENT=/tmp/winnow-provision \
      uv run --project . deploy/provision-matrix-bot.py \
      --invite @you:matrix.example.com

The access token goes straight from the homeserver into 1Password. It is never
printed, so it never needs rotating because of where it was seen.
"""

import argparse
import os
import sys

import httpx

from winnow.provision import OnePasswordVault, ProvisioningError, provision

DEFAULT_HOMESERVER = os.environ.get("WINNOW_MATRIX_HOMESERVER", "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--homeserver", default=DEFAULT_HOMESERVER)
    parser.add_argument("--username", default="winnow")
    parser.add_argument("--invite", required=True, help="user id to invite to the room")
    args = parser.parse_args()

    with httpx.Client(base_url=args.homeserver, timeout=30) as client:
        try:
            provision(
                client,
                vault=OnePasswordVault(),
                homeserver=args.homeserver,
                username=args.username,
                invite=args.invite,
            )
        except ProvisioningError as error:
            print(f"provisioning failed, nothing stored: {error}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
