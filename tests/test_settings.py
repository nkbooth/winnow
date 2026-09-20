"""Infrastructure settings, separate from the career rubric.

Two files, because they answer different questions and change on different
schedules. ``profile.yaml`` is who you are and what work you want — the output
of the intake interview, edited often. ``config.toml`` is where things live and
how to reach them: mail hosts, where the digest goes, which credential
reference resolves to which secret. Written once at setup and rarely touched.

Everything here has a default that does something sensible, because a tool that
refuses to start until a stranger has filled in nine fields is a tool nobody
evaluates.
"""

from pathlib import Path

import pytest

from winnow import settings


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body)
    return path


def test_a_missing_config_file_still_gives_working_settings(tmp_path):
    """First run, nothing configured: the terminal digest must still work."""
    loaded = settings.load(tmp_path / "absent.toml")

    assert loaded.notify.backend == "terminal"
    assert loaded.llm.api_key_ref == "env:ANTHROPIC_API_KEY"


def test_the_notifier_backend_is_chosen_by_name(tmp_path):
    path = _write(
        tmp_path, '[notify]\nbackend = "ntfy"\n\n[notify.ntfy]\ntopic_url = "https://ntfy.sh/abc"\n'
    )

    loaded = settings.load(path)

    assert loaded.notify.backend == "ntfy"
    assert loaded.notify.ntfy_topic_url == "https://ntfy.sh/abc"


def test_an_unknown_backend_is_refused_by_name(tmp_path):
    """Silently falling back to the terminal would hide a typo forever."""
    path = _write(tmp_path, '[notify]\nbackend = "carrier-pigeon"\n')

    with pytest.raises(ValueError, match="carrier-pigeon"):
        settings.load(path)


def test_mail_settings_are_read(tmp_path):
    path = _write(
        tmp_path,
        "[mail]\n"
        'imap_host = "imap.example.com"\n'
        'smtp_host = "smtp.example.com"\n'
        "smtp_port = 465\n"
        'message_id_domain = "example.com"\n'
        'password_ref = "file:/run/secrets/mail"\n',
    )

    loaded = settings.load(path)

    assert loaded.mail.imap_host == "imap.example.com"
    assert loaded.mail.smtp_port == 465
    assert loaded.mail.message_id_domain == "example.com"
    assert loaded.mail.password_ref == "file:/run/secrets/mail"
    assert loaded.mail.imap_port == 993, "untouched keys keep their defaults"


def test_the_user_agent_carries_the_operator_contact(tmp_path):
    """Job boards are being polled by a stranger's tool; they can see whose."""
    path = _write(tmp_path, '[identity]\ncontact = "alex@example.invalid"\n')

    loaded = settings.load(path)

    assert "alex@example.invalid" in loaded.user_agent()
    assert loaded.user_agent().startswith("winnow/")


def test_the_user_agent_says_unconfigured_rather_than_naming_nobody(tmp_path):
    loaded = settings.load(tmp_path / "absent.toml")

    assert "unconfigured" in loaded.user_agent()


def test_mail_is_reported_unconfigured_until_a_host_is_set(tmp_path):
    """The listener and the mailer are optional; nothing should assume a mailbox."""
    assert settings.load(tmp_path / "absent.toml").mail.configured is False

    path = _write(tmp_path, '[mail]\nimap_host = "imap.example.com"\n')
    assert settings.load(path).mail.configured is True


def test_a_malformed_config_names_the_file(tmp_path):
    path = _write(tmp_path, "[mail\nbroken")

    with pytest.raises(ValueError) as error:
        settings.load(path)
    assert "config.toml" in str(error.value)
