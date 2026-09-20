"""Matrix delivery.

A send path and nothing more: one authenticated HTTP request with a bot token.
There is no sync loop, no room state and no encryption handling for incoming
events, because feedback arrives through the review TUI rather than through
Matrix replies. That decision is what lets the digest run as a timer oneshot
instead of a daemon that has to be kept alive and watched for silent death.

The bot is winnow's own identity, deliberately not Nyx's, so a job-search
failure cannot take Nyx down with it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from winnow.secrets import resolve as resolve_secret


class MatrixSendError(RuntimeError):
    """The homeserver refused the message."""


@dataclass(frozen=True)
class MatrixConfig:
    """Where the digest goes, and where the credentials live.

    Holds 1Password references, never values, so an instance is safe to log.
    """

    homeserver_ref: str = "env:WINNOW_MATRIX_HOMESERVER"
    room_id_ref: str = "env:WINNOW_MATRIX_ROOM_ID"
    token_ref: str = "env:WINNOW_MATRIX_TOKEN"
    timeout: float = 30.0


class MatrixSender:
    """Posts a message to one Matrix room as the winnow bot."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        resolve: Callable[[str], str] = resolve_secret,
    ) -> None:
        self._client = client or httpx.Client()
        self._resolve = resolve

    def send(self, text: str, formatted: str, *, config: MatrixConfig | None = None) -> str:
        """Post one message, resolving credentials at call time.

        Args:
            text: The plain-text body.
            formatted: The HTML body Matrix clients render in its place.
            config: Room and credential references.

        Returns:
            The event id the homeserver assigned.

        Raises:
            MatrixSendError: If the homeserver refuses it. A digest that
                silently failed to arrive is the failure this raise exists to
                prevent — the timer's exit status is the only thing watching.
            RuntimeError: If a credential cannot be resolved.
        """
        config = config or MatrixConfig()
        homeserver = self._resolve(config.homeserver_ref).rstrip("/")
        room_id = self._resolve(config.room_id_ref)
        token = self._resolve(config.token_ref)

        # Matrix deduplicates on the transaction id, so a reused one would make
        # the homeserver silently drop tomorrow's digest.
        transaction_id = uuid.uuid4().hex
        url = (
            f"{homeserver}/_matrix/client/v3/rooms/{quote(room_id, safe='')}"
            f"/send/m.room.message/{transaction_id}"
        )

        response = self._client.put(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={
                "msgtype": "m.text",
                "body": text,
                "format": "org.matrix.custom.html",
                "formatted_body": formatted,
            },
            timeout=config.timeout,
        )
        if response.status_code >= 400:
            raise MatrixSendError(f"HTTP {response.status_code}: {response.text}")
        return str(response.json().get("event_id", ""))
