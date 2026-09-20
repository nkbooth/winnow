"""Where messages go, and whether they can arrive in time.

Two kinds of message leave winnow. The daily digest is long and not urgent: it
can sit in a file until somebody reads it. An escalation is short and
time-sensitive — an interview request arriving mid-afternoon is badly served by
a file read tomorrow.

Backends differ in exactly that respect, so every one declares whether it can
push. That lets the caller say so out loud rather than leaving the operator to
discover it by missing something, which is the same rule the rest of this
project follows about silence.

The default is the terminal, because it needs no account, no server and no
token, and a tool that cannot show its output until a stranger has registered
somewhere is a tool that does not get evaluated.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx

from winnow.matrix import MatrixConfig, MatrixSender
from winnow.secrets import resolve as resolve_secret
from winnow.settings import NotifySettings


class Notifier(Protocol):
    """Somewhere a message can be sent."""

    #: Whether a message reaches a person who is not already looking.
    can_push: bool

    def send(self, text: str, formatted: str) -> str:
        """Deliver one message and return whatever identifies it."""
        ...


class TerminalNotifier:
    """Writes to standard output, or appends to a file.

    Cannot push, and says so. Appends rather than replaces: a digest written
    over yesterday's is a digest that destroys the thing it is delivering.
    """

    can_push = False

    def __init__(self, path: str = "") -> None:
        self._path = Path(path) if path else None

    def send(self, text: str, formatted: str) -> str:
        """Write the message, dated so a file of them stays readable."""
        stamped = f"\n{'=' * 72}\n{datetime.now(UTC).isoformat(timespec='seconds')}\n{text}\n"
        if self._path is None:
            print(text)
            return "stdout"

        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a") as handle:
            handle.write(stamped)
        return str(self._path)


class NtfyNotifier:
    """Posts to an ntfy topic: a push notification with nothing to run.

    The realistic answer for someone who wants their phone to buzz and does not
    want to operate a homeserver to make it happen. A topic URL is the whole
    configuration, and anyone who knows the URL can read the topic — which is
    why the generated settings file says to choose an unguessable one rather
    than naming it after yourself.
    """

    can_push = True

    def __init__(self, topic_url: str, token_ref: str = "", client: httpx.Client | None = None):
        if not topic_url:
            raise ValueError("ntfy needs a topic URL — set [notify.ntfy] topic_url")
        self._topic_url = topic_url
        self._token_ref = token_ref
        self._client = client or httpx.Client(timeout=15.0)

    def send(self, text: str, formatted: str) -> str:
        """Post the plain text.

        Args:
            text: The body. ntfy shows plain text, so the HTML form is unused.
            formatted: Ignored here, kept for one interface across backends.

        Returns:
            The topic it went to.

        Raises:
            httpx.HTTPStatusError: If the server refuses it. A digest that
                silently failed to arrive is the failure worth preventing.
        """
        headers = {"Title": "winnow", "Markdown": "yes"}
        if self._token_ref:
            headers["Authorization"] = f"Bearer {resolve_secret(self._token_ref)}"

        response = self._client.post(self._topic_url, content=text.encode(), headers=headers)
        response.raise_for_status()
        return self._topic_url


class MatrixNotifier:
    """Posts to a Matrix room, for operators who already have a homeserver."""

    can_push = True

    def __init__(self, config: MatrixConfig, client: httpx.Client | None = None) -> None:
        self._config = config
        self._sender = MatrixSender(client=client)

    def send(self, text: str, formatted: str) -> str:
        """Post one message and return the event id."""
        return self._sender.send(text, formatted, config=self._config)


def build(config: NotifySettings, *, client: httpx.Client | None = None) -> Notifier:
    """Construct the configured backend.

    Anything unusable is refused here rather than at send time. A misconfigured
    notifier that only fails once a day, inside a timer, is indistinguishable
    from a quiet week.

    Args:
        config: The ``[notify]`` section.
        client: HTTP client, chiefly for tests.

    Returns:
        The backend.

    Raises:
        ValueError: If the backend cannot be built from what it was given.
    """
    if config.backend == "terminal":
        return TerminalNotifier(config.terminal_path)
    if config.backend == "ntfy":
        return NtfyNotifier(config.ntfy_topic_url, config.ntfy_token_ref, client=client)
    if config.backend == "matrix":
        return MatrixNotifier(
            MatrixConfig(
                homeserver_ref=config.matrix_homeserver_ref,
                room_id_ref=config.matrix_room_id_ref,
                token_ref=config.matrix_token_ref,
            ),
            client=client,
        )
    raise ValueError(f"{config.backend!r} is not a delivery backend")
