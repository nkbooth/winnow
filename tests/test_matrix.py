"""Matrix delivery.

winnow needs a send path and nothing else — no sync loop, no room state, no
encryption handling for incoming events. Choosing a review TUI over Matrix
replies is what removed all of that, and it is why this process is a timer
oneshot rather than a daemon.
"""

import httpx
import pytest

from winnow.matrix import MatrixConfig, MatrixSender, MatrixSendError

CONFIG = MatrixConfig()

SECRETS = {
    "env:WINNOW_MATRIX_HOMESERVER": "https://matrix.example.test",
    "env:WINNOW_MATRIX_ROOM_ID": "!abcdef:example.test",
    "env:WINNOW_MATRIX_TOKEN": "syt_secret",
}


def _sender(handler):
    return MatrixSender(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolve=SECRETS.__getitem__,
    )


def test_the_message_is_sent_to_the_configured_room():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"event_id": "$1"})

    event_id = _sender(handler).send("plain", "<p>html</p>", config=CONFIG)

    assert event_id == "$1"
    assert seen["method"] == "PUT"
    assert seen["url"].startswith(
        "https://matrix.example.test/_matrix/client/v3/rooms/"
        "%21abcdef%3Aexample.test/send/m.room.message/"
    )
    assert seen["auth"] == "Bearer syt_secret"


def test_the_body_carries_both_renderings():
    import json

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.read()))
        return httpx.Response(200, json={"event_id": "$1"})

    _sender(handler).send("plain text", "<p>markup</p>", config=CONFIG)

    assert captured["msgtype"] == "m.text"
    assert captured["body"] == "plain text"
    assert captured["format"] == "org.matrix.custom.html"
    assert captured["formatted_body"] == "<p>markup</p>"


def test_each_send_uses_a_fresh_transaction_id():
    """Reusing one would make Matrix drop the second digest as a duplicate."""
    urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, json={"event_id": "$1"})

    sender = _sender(handler)
    sender.send("a", "<p>a</p>", config=CONFIG)
    sender.send("b", "<p>b</p>", config=CONFIG)

    assert urls[0] != urls[1]


def test_a_rejected_send_is_raised_not_swallowed():
    """A digest that silently failed to arrive is the failure mode to avoid."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"errcode": "M_FORBIDDEN"})

    with pytest.raises(MatrixSendError) as error:
        _sender(handler).send("plain", "<p>html</p>", config=CONFIG)
    assert "M_FORBIDDEN" in str(error.value)


def test_the_config_holds_references_not_secrets():
    """So an instance is safe to log, repr, or dump into a debug trace."""
    rendered = repr(CONFIG)
    assert "env:WINNOW_MATRIX_TOKEN" in rendered
    assert "syt_" not in rendered
