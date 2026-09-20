"""Mail construction and delivery for winnow.

Every outbound message goes through this module. Nothing hand-builds an
``EmailMessage`` elsewhere.

The reason is a real failure: a message assembled ad hoc reached the MTA
without a ``Date`` header and was relayed anyway with
``250 2.6.0 Bad message, but will be delivered anyway``. Delivery succeeded,
so no error surfaced. Threading would have broken silently for the same
reason, since ``Message-ID`` was missing too -- and winnow correlates
employer replies back to applications by thread.
"""

from __future__ import annotations

import mimetypes
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from winnow import config as app_config
from winnow.secrets import resolve as resolve_secret
from winnow.settings import MailSettings

#: Only used when no domain is configured. A Message-ID must be globally
#: unique and its domain part is what makes it so, which is why this is a
#: setting rather than a guess at the sender's address.
DEFAULT_MESSAGE_ID_DOMAIN = "localhost"


@dataclass(frozen=True)
class SmtpConfig:
    """Where to send from, and where the credentials live.

    Holds 1Password *references*, never secret values -- so an instance is
    safe to log, repr, or write into a debug dump.

    The mailbox lives in its own vault rather than beside the other winnow
    secrets. Service accounts scope to vaults, not items, so that separation is
    what makes "the digest timer holds no credential that could send mail" a
    property of the deployment rather than a promise about it.
    """

    host: str = ""
    port: int = 587
    starttls: bool = True
    username_ref: str = "env:WINNOW_MAIL_USERNAME"
    password_ref: str = "env:WINNOW_MAIL_PASSWORD"

    @classmethod
    def from_settings(cls, mail: MailSettings) -> SmtpConfig:
        """Build from the ``[mail]`` section of config.toml.

        The defaults describe a machine with no mailbox. Shipping that is
        right; connecting with it is not, and nothing carried the configured
        host into here until a deployment found out the hard way.
        """
        return cls(
            host=mail.smtp_host,
            port=mail.smtp_port,
            username_ref=mail.username_ref,
            password_ref=mail.password_ref,
        )


def build_message(
    sender: str,
    to: str | list[str],
    subject: str,
    body: str,
    *,
    message_id_domain: str = DEFAULT_MESSAGE_ID_DOMAIN,
) -> EmailMessage:
    """Build an RFC 5322-compliant message.

    Sets the headers that neither ``EmailMessage`` nor
    ``smtplib.send_message`` supplies on its own: ``Date`` (required by
    RFC 5322 s3.6) and ``Message-ID`` (required in practice for threading
    and spam scoring).

    Args:
        sender: From address, optionally with a display name.
        to: One recipient address, or a list of them.
        subject: Subject line.
        body: Plain-text body.
        message_id_domain: Right-hand side of the generated Message-ID.

    Returns:
        A message ready for attachments, threading, or sending.

    Raises:
        ValueError: If sender is empty or no recipients are given.
    """
    if not sender:
        raise ValueError("sender is required")

    recipients = [to] if isinstance(to, str) else list(to)
    if not recipients:
        raise ValueError("at least one recipient is required")

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=message_id_domain)
    msg.set_content(body)
    return msg


def attach_files(msg: EmailMessage, paths: list[Path]) -> EmailMessage:
    """Attach files to a message, inferring each one's media type.

    Args:
        msg: Message to attach to; modified in place.
        paths: Files to attach.

    Returns:
        The same message, for chaining.

    Raises:
        FileNotFoundError: If any path does not exist.
    """
    for path in paths:
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(path)
        ctype, _ = mimetypes.guess_type(path.name)
        maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
        msg.add_attachment(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )
    return msg


def as_reply(msg: EmailMessage, parent: EmailMessage) -> EmailMessage:
    """Thread a message as a reply to ``parent``.

    Sets In-Reply-To and extends the References chain. Mail clients thread
    on References, not on subject, so a reply that omits it starts a new
    conversation in the recipient's inbox even when the subject matches.

    Args:
        msg: The reply; modified in place. Keeps its own Message-ID.
        parent: The message being replied to.

    Returns:
        The same message, for chaining.

    Raises:
        ValueError: If the parent has no Message-ID to reference.
    """
    parent_id = parent["Message-ID"]
    if not parent_id:
        raise ValueError("parent message has no Message-ID; cannot thread")

    chain = [ref for ref in (parent.get("References", "").split()) if ref]
    chain.append(parent_id)

    del msg["In-Reply-To"]
    del msg["References"]
    msg["In-Reply-To"] = parent_id
    msg["References"] = " ".join(chain)
    return msg


def send(msg: EmailMessage, config: SmtpConfig | None = None) -> str:
    """Send a message over SMTP, resolving credentials at call time.

    Refuses to send a message missing headers that RFC 5322 requires. The
    MTA will often accept such a message anyway, which makes the defect
    invisible until threading breaks weeks later -- so the check belongs
    here, before the message leaves.

    Args:
        msg: A message built by :func:`build_message`.
        config: Server and credential references; defaults to the winnow mailbox.

    Returns:
        The sent message's Message-ID, for recording against an application.

    Raises:
        ValueError: If a required header is missing.
        RuntimeError: If credentials cannot be resolved.
        smtplib.SMTPException: If the server rejects the message.
    """
    # Falls back to the configured mailbox rather than to empty defaults: an
    # unset host is a machine with no mailbox, not a reason to dial nowhere.
    config = config or SmtpConfig.from_settings(app_config.settings().mail)

    missing = [h for h in ("From", "To", "Date", "Message-ID") if not msg[h]]
    if missing:
        raise ValueError(f"refusing to send; missing headers: {', '.join(missing)}")

    username = resolve_secret(config.username_ref)
    password = resolve_secret(config.password_ref)

    with smtplib.SMTP(config.host, config.port, timeout=30) as server:
        server.ehlo()
        if config.starttls:
            server.starttls()
            server.ehlo()
        server.login(username, password)
        server.send_message(msg)

    return msg["Message-ID"]
