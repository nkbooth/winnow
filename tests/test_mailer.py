"""Tests for the winnow mail helper.

The bug that prompted this module: a hand-built message was relayed with
`INVALID HEADER: MISSING REQUIRED HEADER FIELD - Date`, and was delivered
anyway. Nothing failed at send time, so nothing was noticed. These tests
assert the headers exist before a message ever reaches SMTP.
"""

import unittest
from dataclasses import FrozenInstanceError
from email.message import EmailMessage
from pathlib import Path

from winnow.mailer import SmtpConfig, as_reply, attach_files, build_message


class TestRequiredHeaders(unittest.TestCase):
    """RFC 5322 s3.6 requires an origination date and a From field."""

    def setUp(self) -> None:
        self.msg = build_message(
            sender="alex@example.invalid",
            to="someone@example.com",
            subject="Test",
            body="Body text.",
        )

    def test_returns_email_message(self) -> None:
        self.assertIsInstance(self.msg, EmailMessage)

    def test_date_header_present(self) -> None:
        self.assertIsNotNone(self.msg["Date"], "Date is required by RFC 5322")

    def test_date_header_is_rfc5322_format(self) -> None:
        # e.g. "Fri, 18 Sep 2026 02:27:04 -0400"
        self.assertRegex(
            self.msg["Date"],
            r"^[A-Z][a-z]{2}, \d{1,2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2} [+-]\d{4}$",
        )

    def test_message_id_present(self) -> None:
        self.assertIsNotNone(self.msg["Message-ID"], "threading depends on Message-ID")

    def test_message_id_is_bracketed_and_has_domain(self) -> None:
        mid = self.msg["Message-ID"]
        self.assertTrue(mid.startswith("<") and mid.endswith(">"))
        self.assertIn("@", mid)

    def test_message_id_uses_configured_domain(self) -> None:
        self.assertTrue(self.msg["Message-ID"].rstrip(">").endswith("localhost"))

    def test_message_id_is_unique_per_message(self) -> None:
        other = build_message(
            sender="alex@example.invalid",
            to="someone@example.com",
            subject="Test",
            body="Body text.",
        )
        self.assertNotEqual(self.msg["Message-ID"], other["Message-ID"])

    def test_from_to_subject_present(self) -> None:
        self.assertEqual(self.msg["From"], "alex@example.invalid")
        self.assertEqual(self.msg["To"], "someone@example.com")
        self.assertEqual(self.msg["Subject"], "Test")

    def test_body_is_set(self) -> None:
        self.assertIn("Body text.", self.msg.get_content())

    def test_display_name_is_preserved(self) -> None:
        msg = build_message(
            sender="Alex Rivera <alex@example.invalid>",
            to="someone@example.com",
            subject="s",
            body="b",
        )
        self.assertEqual(msg["From"], "Alex Rivera <alex@example.invalid>")

    def test_multiple_recipients(self) -> None:
        msg = build_message(
            sender="alex@example.invalid",
            to=["a@example.com", "b@example.com"],
            subject="s",
            body="b",
        )
        self.assertIn("a@example.com", msg["To"])
        self.assertIn("b@example.com", msg["To"])


class TestValidation(unittest.TestCase):
    def test_rejects_empty_recipients(self) -> None:
        with self.assertRaises(ValueError):
            build_message(sender="a@b.com", to=[], subject="s", body="b")

    def test_rejects_missing_sender(self) -> None:
        with self.assertRaises(ValueError):
            build_message(sender="", to="a@b.com", subject="s", body="b")


class TestAttachments(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(__file__).resolve().parent / "_fixtures"
        self.tmp.mkdir(exist_ok=True)
        self.pdf = self.tmp / "sample.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 fake")
        self.txt = self.tmp / "notes.txt"
        self.txt.write_text("plain")
        self.msg = build_message(sender="a@b.com", to="c@d.com", subject="s", body="b")

    def tearDown(self) -> None:
        for f in self.tmp.iterdir():
            f.unlink()
        self.tmp.rmdir()

    def test_attaching_makes_message_multipart(self) -> None:
        attach_files(self.msg, [self.pdf])
        self.assertTrue(self.msg.is_multipart())

    def test_attachment_filename_preserved(self) -> None:
        attach_files(self.msg, [self.pdf])
        names = [p.get_filename() for p in self.msg.iter_attachments()]
        self.assertIn("sample.pdf", names)

    def test_pdf_gets_correct_content_type(self) -> None:
        attach_files(self.msg, [self.pdf])
        part = next(iter(self.msg.iter_attachments()))
        self.assertEqual(part.get_content_type(), "application/pdf")

    def test_multiple_attachments(self) -> None:
        attach_files(self.msg, [self.pdf, self.txt])
        self.assertEqual(len(list(self.msg.iter_attachments())), 2)

    def test_headers_survive_attaching(self) -> None:
        attach_files(self.msg, [self.pdf])
        self.assertIsNotNone(self.msg["Date"])
        self.assertIsNotNone(self.msg["Message-ID"])

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            attach_files(self.msg, [self.tmp / "nope.pdf"])


class TestThreading(unittest.TestCase):
    def setUp(self) -> None:
        self.original = build_message(
            sender="them@example.com",
            to="alex@example.invalid",
            subject="Your application",
            body="hello",
        )

    def test_reply_sets_in_reply_to(self) -> None:
        reply = build_message(
            sender="alex@example.invalid",
            to="them@example.com",
            subject="Re: Your application",
            body="thanks",
        )
        as_reply(reply, self.original)
        self.assertEqual(reply["In-Reply-To"], self.original["Message-ID"])

    def test_reply_sets_references(self) -> None:
        reply = build_message(
            sender="alex@example.invalid", to="them@example.com", subject="Re: x", body="b"
        )
        as_reply(reply, self.original)
        self.assertIn(self.original["Message-ID"], reply["References"])

    def test_references_chain_accumulates(self) -> None:
        first_id = self.original["Message-ID"]
        reply1 = build_message(sender="a@b.com", to="c@d.com", subject="r1", body="b")
        as_reply(reply1, self.original)
        reply2 = build_message(sender="a@b.com", to="c@d.com", subject="r2", body="b")
        as_reply(reply2, reply1)
        self.assertIn(first_id, reply2["References"])
        self.assertIn(reply1["Message-ID"], reply2["References"])

    def test_reply_keeps_its_own_message_id(self) -> None:
        reply = build_message(sender="a@b.com", to="c@d.com", subject="r", body="b")
        own = reply["Message-ID"]
        as_reply(reply, self.original)
        self.assertEqual(reply["Message-ID"], own)
        self.assertNotEqual(reply["Message-ID"], self.original["Message-ID"])

    def test_parent_without_message_id_raises(self) -> None:
        orphan = EmailMessage()
        reply = build_message(sender="a@b.com", to="c@d.com", subject="r", body="b")
        with self.assertRaises(ValueError):
            as_reply(reply, orphan)


class TestSmtpConfig(unittest.TestCase):
    """Credentials resolve from 1Password; nothing is ever hardcoded."""

    def test_defaults_name_no_mailbox(self) -> None:
        """Sending is optional, so the default is a configuration that cannot.

        A default host would be somebody's server, and a tool that ships
        pointing at a stranger's mail infrastructure is worse than one that
        ships pointing nowhere.
        """
        cfg = SmtpConfig()
        self.assertEqual(cfg.host, "")
        self.assertEqual(cfg.port, 587)
        self.assertTrue(cfg.starttls)

    def test_credentials_are_references_never_values(self) -> None:
        """Whatever backend resolves them, a config file holds no password."""
        cfg = SmtpConfig()
        for reference in (cfg.username_ref, cfg.password_ref):
            self.assertRegex(reference, r"^(env:|file:|op://)")

    def test_config_is_immutable(self) -> None:
        cfg = SmtpConfig()
        with self.assertRaises(FrozenInstanceError):
            cfg.host = "evil.example.com"  # type: ignore[misc]

    def test_credentials_are_not_stored_on_the_config(self) -> None:
        cfg = SmtpConfig()
        blob = repr(cfg).lower()
        self.assertNotIn("password=", blob.replace("password_ref=", ""))

    def test_host_and_port_are_overridable(self) -> None:
        cfg = SmtpConfig(host="localhost", port=1025, starttls=False)
        self.assertEqual((cfg.host, cfg.port, cfg.starttls), ("localhost", 1025, False))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestSettingsWiring(unittest.TestCase):
    """The mailer reads the same [mail] section the listener does."""

    def test_smtp_config_is_built_from_settings(self) -> None:
        from winnow.settings import MailSettings

        mail = MailSettings(
            smtp_host="smtp.example.com",
            smtp_port=465,
            message_id_domain="example.com",
            username_ref="env:U",
            password_ref="env:P",
        )

        cfg = SmtpConfig.from_settings(mail)

        self.assertEqual(cfg.host, "smtp.example.com")
        self.assertEqual(cfg.port, 465)
        self.assertEqual(cfg.username_ref, "env:U")
        self.assertEqual(cfg.password_ref, "env:P")
