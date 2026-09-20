"""Delivery, and the difference between a digest and an alert.

Two kinds of message leave this tool. The daily digest is long and not urgent —
it can wait in a file until someone reads it. An escalation is short and
time-sensitive: an interview request arriving mid-afternoon is badly served by
a file read tomorrow.

Backends differ in exactly that respect, so the interface keeps them separate
and each backend says honestly whether it can push. A terminal cannot, and
saying so is what lets the digest report it rather than the operator finding
out by missing something.
"""

import httpx
import pytest

from winnow import notify, settings


def test_the_terminal_backend_writes_the_digest_where_it_was_asked(tmp_path):
    path = tmp_path / "digest.txt"
    backend = notify.build(settings.NotifySettings(backend="terminal", terminal_path=str(path)))

    backend.send("the digest body", "<p>the digest body</p>")

    assert path.read_text().endswith("the digest body\n")


def test_the_terminal_backend_appends_rather_than_replacing(tmp_path):
    path = tmp_path / "digest.txt"
    backend = notify.build(settings.NotifySettings(backend="terminal", terminal_path=str(path)))

    backend.send("monday", "")
    backend.send("tuesday", "")

    assert "monday" in path.read_text()
    assert "tuesday" in path.read_text()


def test_the_terminal_backend_goes_to_stdout_by_default(capsys):
    backend = notify.build(settings.NotifySettings(backend="terminal"))

    backend.send("printed", "")

    assert "printed" in capsys.readouterr().out


def test_the_terminal_backend_admits_it_cannot_push():
    """The digest can say so, instead of the operator finding out by missing one."""
    assert notify.build(settings.NotifySettings(backend="terminal")).can_push is False


def test_ntfy_posts_the_body_to_the_topic():
    seen: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    backend = notify.build(
        settings.NotifySettings(backend="ntfy", ntfy_topic_url="https://ntfy.sh/secret-topic"),
        client=httpx.Client(transport=httpx.MockTransport(capture)),
    )
    backend.send("two roles today", "<p>two roles today</p>")

    assert str(seen[0].url) == "https://ntfy.sh/secret-topic"
    assert seen[0].content == b"two roles today"
    assert backend.can_push is True


def test_ntfy_without_a_topic_is_refused_at_construction():
    """Failing later would mean a digest silently going nowhere every morning."""
    with pytest.raises(ValueError, match="topic"):
        notify.build(settings.NotifySettings(backend="ntfy"))


def test_a_rejected_ntfy_post_is_raised_not_swallowed():
    backend = notify.build(
        settings.NotifySettings(backend="ntfy", ntfy_topic_url="https://ntfy.sh/t"),
        client=httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(403, text="forbidden"))
        ),
    )

    with pytest.raises(httpx.HTTPStatusError):
        backend.send("body", "")


def test_the_matrix_backend_is_built_from_its_references(monkeypatch):
    monkeypatch.setenv("WINNOW_MATRIX_HOMESERVER", "https://matrix.example.test")
    monkeypatch.setenv("WINNOW_MATRIX_ROOM_ID", "!room:example.test")
    monkeypatch.setenv("WINNOW_MATRIX_TOKEN", "syt_token")

    backend = notify.build(
        settings.NotifySettings(
            backend="matrix",
            matrix_homeserver_ref="env:WINNOW_MATRIX_HOMESERVER",
            matrix_room_id_ref="env:WINNOW_MATRIX_ROOM_ID",
            matrix_token_ref="env:WINNOW_MATRIX_TOKEN",
        )
    )

    assert backend.can_push is True
