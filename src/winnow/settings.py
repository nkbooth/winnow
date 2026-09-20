"""Where things live and how to reach them.

Kept apart from ``profile.yaml`` because the two answer different questions and
change on different schedules. The profile is who you are and what work you
want: the output of the intake interview, edited whenever the search moves.
This is infrastructure — mail hosts, where the digest goes, which credential
reference resolves to which secret — written once at setup and rarely touched.

Every value has a default that does something, and the defaults describe a
machine with nothing set up: digest to the terminal, credentials from the
environment, no mailbox. A tool that refuses to start until a stranger has
filled in nine fields is a tool nobody gets far enough to evaluate.

Credentials are never values here, only references. See :mod:`winnow.secrets`.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from winnow import __version__

#: Where the digest and alerts can be sent.
BACKENDS = ("terminal", "ntfy", "matrix")


@dataclass(frozen=True)
class MailSettings:
    """The mailbox the listener watches and the mailer sends from.

    Optional in full. Without it the pipeline still polls, scores and digests;
    what stops is reading employer replies and sending a draft, both of which
    are additions to a tool that works without them.
    """

    imap_host: str = ""
    imap_port: int = 993
    smtp_host: str = ""
    smtp_port: int = 587
    mailbox: str = "INBOX"
    sent_folder: str = "Sent"
    message_id_domain: str = "localhost"
    username_ref: str = "env:WINNOW_MAIL_USERNAME"
    password_ref: str = "env:WINNOW_MAIL_PASSWORD"

    @property
    def configured(self) -> bool:
        """Whether there is a mailbox to talk to at all."""
        return bool(self.imap_host or self.smtp_host)


@dataclass(frozen=True)
class NotifySettings:
    """Where the daily digest and anything urgent are delivered.

    ``terminal`` is the default because it needs nothing: no account, no
    server, no token. It is also the only backend that cannot push, which
    matters for the one message that is time-sensitive — an interview request
    arriving at two in the afternoon is not well served by a file you read
    tomorrow.
    """

    backend: str = "terminal"
    ntfy_topic_url: str = ""
    ntfy_token_ref: str = ""
    matrix_homeserver_ref: str = ""
    matrix_room_id_ref: str = ""
    matrix_token_ref: str = ""
    #: Where a terminal digest is written. Empty means standard output.
    terminal_path: str = ""


@dataclass(frozen=True)
class LlmSettings:
    """The model credential. The one thing with no free default."""

    api_key_ref: str = "env:ANTHROPIC_API_KEY"


@dataclass(frozen=True)
class AdzunaSettings:
    """Optional aggregator credentials, used only by ``winnow discover``."""

    app_id_ref: str = ""
    app_key_ref: str = ""

    @property
    def configured(self) -> bool:
        """Whether discovery can run."""
        return bool(self.app_id_ref and self.app_key_ref)


@dataclass(frozen=True)
class Settings:
    """Everything that is not the rubric."""

    contact: str = ""
    mail: MailSettings = field(default_factory=MailSettings)
    notify: NotifySettings = field(default_factory=NotifySettings)
    llm: LlmSettings = field(default_factory=LlmSettings)
    adzuna: AdzunaSettings = field(default_factory=AdzunaSettings)

    def user_agent(self) -> str:
        """Identify this installation to the job boards it polls.

        These are public endpoints used as intended, and part of using them as
        intended is being reachable. An operator who has not said who they are
        is described as unconfigured rather than left anonymous, because a
        blank contact in a real request is worse than an honest admission.
        """
        contact = self.contact or "unconfigured"
        return f"winnow/{__version__} (personal job search; contact {contact})"


def load(path: Path | str) -> Settings:
    """Read a settings file, falling back to defaults for anything absent.

    Args:
        path: Location of ``config.toml``. A missing file is not an error: it
            describes a machine where nothing has been set up yet, which is
            exactly the state the defaults are written for.

    Returns:
        The settings.

    Raises:
        ValueError: If the file does not parse, or names a delivery backend
            that does not exist. A misspelled backend silently falling back to
            the terminal would hide the mistake for as long as nobody wondered
            where their notifications went.
    """
    path = Path(path)
    if not path.exists():
        return Settings()

    try:
        document = tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, OSError) as error:
        raise ValueError(f"cannot read {path}: {error}") from error

    notify = document.get("notify") or {}
    backend = str(notify.get("backend", "terminal"))
    if backend not in BACKENDS:
        raise ValueError(
            f"{backend!r} is not a delivery backend — choose one of {', '.join(BACKENDS)}"
        )

    mail = document.get("mail") or {}
    ntfy = notify.get("ntfy") or {}
    matrix = notify.get("matrix") or {}
    llm = document.get("llm") or {}
    adzuna = document.get("adzuna") or {}

    return Settings(
        contact=str((document.get("identity") or {}).get("contact", "")),
        mail=MailSettings(
            imap_host=str(mail.get("imap_host", "")),
            imap_port=int(mail.get("imap_port", 993)),
            smtp_host=str(mail.get("smtp_host", "")),
            smtp_port=int(mail.get("smtp_port", 587)),
            mailbox=str(mail.get("mailbox", "INBOX")),
            sent_folder=str(mail.get("sent_folder", "Sent")),
            message_id_domain=str(mail.get("message_id_domain", "localhost")),
            username_ref=str(mail.get("username_ref", MailSettings.username_ref)),
            password_ref=str(mail.get("password_ref", MailSettings.password_ref)),
        ),
        notify=NotifySettings(
            backend=backend,
            ntfy_topic_url=str(ntfy.get("topic_url", "")),
            ntfy_token_ref=str(ntfy.get("token_ref", "")),
            matrix_homeserver_ref=str(matrix.get("homeserver_ref", "")),
            matrix_room_id_ref=str(matrix.get("room_id_ref", "")),
            matrix_token_ref=str(matrix.get("token_ref", "")),
            terminal_path=str((notify.get("terminal") or {}).get("path", "")),
        ),
        llm=LlmSettings(api_key_ref=str(llm.get("api_key_ref", LlmSettings.api_key_ref))),
        adzuna=AdzunaSettings(
            app_id_ref=str(adzuna.get("app_id_ref", "")),
            app_key_ref=str(adzuna.get("app_key_ref", "")),
        ),
    )
