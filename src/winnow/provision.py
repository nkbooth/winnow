"""One-time provisioning of the Matrix bot identity.

Deliberately outside the command surface. `winnow digest` and `winnow listen`
run unattended, and neither should contain code that can write a credential —
this is run by hand, once, from the laptop.

What it does not do is print anything secret. The access token goes from the
homeserver's response into 1Password without touching a terminal, a log line, or
a shell history, because the moment it appears in any of those it has to be
rotated to be trusted again.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Protocol

import httpx

DEVICE_DISPLAY_NAME = "winnow digest"
ROOM_TOPIC = "Daily job-board digest. Feedback goes through `winnow review`, not here."


class ProvisioningError(RuntimeError):
    """The homeserver refused something, and nothing was stored."""


@dataclass(frozen=True)
class Provisioned:
    """What provisioning established."""

    user_id: str
    device_id: str
    room_id: str


class Vault(Protocol):
    """The credential store, injected so tests never touch the real one."""

    def read(self, reference: str) -> str:
        """Resolve one reference."""
        ...

    def write(self, item: str, fields: dict[str, str]) -> None:
        """Set fields on one item."""
        ...


class OnePasswordVault:
    """The real vault, driven through the `op` CLI."""

    def __init__(self, vault: str = "winnow") -> None:
        self._vault = vault

    def read(self, reference: str) -> str:
        """Resolve one ``op://`` reference."""
        from winnow.secrets import resolve as resolve_secret

        return resolve_secret(reference)

    def write(self, item: str, fields: dict[str, str]) -> None:
        """Set fields on an item, creating it if it does not exist.

        Values are passed as arguments to ``op`` rather than echoed anywhere.

        Raises:
            RuntimeError: If ``op`` refuses the edit.
        """
        assignments = [f"{name}={value}" for name, value in fields.items()]
        result = subprocess.run(
            ["op", "item", "edit", item, "--vault", self._vault, *assignments],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"op item edit failed: {result.stderr.strip()}")


def login_payload(username: str, password: str) -> dict:
    """Build the login request.

    The device is named so that a later look at the account's sessions can tell
    which one is the digest timer and which one was a human.
    """
    return {
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": username},
        "password": password,
        "initial_device_display_name": DEVICE_DISPLAY_NAME,
    }


def room_payload(name: str, invite: str) -> dict:
    """Build the room-creation request.

    Private, invite-only, and **not** encrypted. That last part is deliberate:
    the digest sender is a single authenticated PUT with no crypto state to
    hold, which is what lets it be a timer oneshot rather than a daemon. The
    room lives on a homeserver in the house and carries job listings.
    """
    return {
        "name": name,
        "topic": ROOM_TOPIC,
        "preset": "private_chat",
        "visibility": "private",
        "invite": [invite],
    }


def provision(
    client: httpx.Client,
    *,
    vault: Vault,
    homeserver: str,
    username: str,
    invite: str,
    item: str = "matrix-bot",
    password_reference: str = "env:WINNOW_MATRIX_PASSWORD",
) -> Provisioned:
    """Log the bot in, create its room, and store what the digest will need.

    Args:
        client: HTTP client pointed at the homeserver.
        vault: Where the credentials are read from and written to.
        homeserver: Base URL, stored so nothing has to guess it later.
        username: Localpart of the bot account.
        invite: Full user id to invite to the room.
        item: The 1Password item to write to.
        password_reference: Where the bot's password already lives.

    Returns:
        The identifiers that were stored.

    Raises:
        ProvisioningError: If the homeserver refuses. Nothing is written in that
            case — a half-provisioned item is worse than none, because it looks
            finished.
    """
    password = vault.read(password_reference)

    login = _post(client, "/_matrix/client/v3/login", login_payload(username, password))
    token = login["access_token"]

    room = _post(
        client,
        "/_matrix/client/v3/createRoom",
        room_payload(username, invite),
        token=token,
    )

    vault.write(
        item,
        {
            "homeserver": homeserver,
            "room-id": room["room_id"],
            "token": token,
            "user-id": login["user_id"],
            "device-id": login["device_id"],
        },
    )

    print(f"bot {login['user_id']} provisioned, device {login['device_id']}")
    print(f"room {room['room_id']} created, {invite} invited")
    return Provisioned(
        user_id=login["user_id"], device_id=login["device_id"], room_id=room["room_id"]
    )


def _post(client: httpx.Client, path: str, payload: dict, *, token: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = client.post(path, json=payload, headers=headers)
    if response.status_code >= 400:
        # The body carries the errcode and no secret of ours, so it is safe to
        # surface; the request body is not.
        raise ProvisioningError(f"{path} returned {response.status_code}: {response.text}")
    try:
        return response.json()
    except json.JSONDecodeError as error:
        raise ProvisioningError(f"{path} returned a non-JSON body") from error
