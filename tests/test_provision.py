"""Provisioning the Matrix bot identity.

Run once, by hand, on the laptop — never by the timer or the listener, which is
why it lives outside the package's command surface. The unattended binaries
contain no code that writes credentials.

The property worth testing is discipline rather than protocol: the access token
this obtains must reach 1Password without passing through a terminal, a log
line, or a shell history.
"""

import json

import httpx
import pytest

from winnow import provision

HOMESERVER = "https://matrix.example.test"


class FakeVault:
    """Stands in for `op`, recording what was written."""

    def __init__(self, secrets):
        self.secrets = dict(secrets)
        self.written: dict[str, str] = {}

    def read(self, reference):
        return self.secrets[reference]

    def write(self, item, fields):
        self.written = dict(fields)


def _transport(responses):
    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.path}"
        body, status = responses[key]
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


@pytest.fixture
def client():
    return httpx.Client(
        base_url=HOMESERVER,
        transport=_transport(
            {
                "POST /_matrix/client/v3/login": (
                    {
                        "access_token": "syt_verysecret",
                        "device_id": "ABCDEFG",
                        "user_id": "@winnow:matrix.example.test",
                    },
                    200,
                ),
                "POST /_matrix/client/v3/createRoom": (
                    {"room_id": "!newroom:matrix.example.test"},
                    200,
                ),
            }
        ),
    )


def test_login_payload_names_the_device_it_creates():
    """A nameless device in a session list is one nobody can audit later."""
    payload = provision.login_payload("winnow", "hunter2")
    assert payload["type"] == "m.login.password"
    assert payload["identifier"] == {"type": "m.id.user", "user": "winnow"}
    assert payload["initial_device_display_name"]


def test_the_room_is_private_invite_only_and_unencrypted():
    """Unencrypted on purpose: the digest sender is one PUT with no crypto state."""
    payload = provision.room_payload("winnow", "@alex:matrix.example.test")
    assert payload["preset"] == "private_chat"
    assert payload["visibility"] == "private"
    assert payload["invite"] == ["@alex:matrix.example.test"]
    assert "m.room.encryption" not in json.dumps(payload)


def test_provisioning_writes_every_field_the_digest_needs(client):
    vault = FakeVault({"env:WINNOW_MATRIX_PASSWORD": "hunter2"})

    result = provision.provision(
        client,
        vault=vault,
        homeserver=HOMESERVER,
        username="winnow",
        invite="@alex:matrix.example.test",
    )

    assert vault.written["homeserver"] == HOMESERVER
    assert vault.written["room-id"] == "!newroom:matrix.example.test"
    assert vault.written["token"] == "syt_verysecret"
    assert vault.written["user-id"] == "@winnow:matrix.example.test"
    assert vault.written["device-id"] == "ABCDEFG"
    assert result.room_id == "!newroom:matrix.example.test"


def test_the_token_is_never_printed(client, capsys):
    vault = FakeVault({"env:WINNOW_MATRIX_PASSWORD": "hunter2"})

    provision.provision(
        client,
        vault=vault,
        homeserver=HOMESERVER,
        username="winnow",
        invite="@alex:matrix.example.test",
    )

    captured = capsys.readouterr()
    assert "syt_verysecret" not in captured.out
    assert "syt_verysecret" not in captured.err
    assert "hunter2" not in captured.out


def test_a_refused_login_stops_before_anything_is_written():
    vault = FakeVault({"env:WINNOW_MATRIX_PASSWORD": "wrong"})
    client = httpx.Client(
        base_url=HOMESERVER,
        transport=_transport(
            {
                "POST /_matrix/client/v3/login": (
                    {"errcode": "M_FORBIDDEN", "error": "Invalid password"},
                    403,
                )
            }
        ),
    )

    with pytest.raises(provision.ProvisioningError) as error:
        provision.provision(
            client,
            vault=vault,
            homeserver=HOMESERVER,
            username="winnow",
            invite="@alex:matrix.example.test",
        )

    assert "M_FORBIDDEN" in str(error.value)
    assert vault.written == {}
