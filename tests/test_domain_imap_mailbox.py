from __future__ import annotations

from email.message import EmailMessage

from core.domain_imap_mailbox import DomainImapCatchallMailbox


def _raw_message(*, recipient: str, code: str, message_id: str) -> bytes:
    message = EmailMessage()
    message["To"] = "Catchall <catchall@example.test>"
    message["Delivered-To"] = recipient
    message["Subject"] = "Your verification code"
    message["Message-ID"] = message_id
    message.set_content(f"Use this verification code: {code}")
    return message.as_bytes()


class FakeImap:
    def __init__(self, messages: dict[bytes, bytes]):
        self.messages = messages
        self.login_args = None
        self.selected = None
        self.logged_out = False

    def login(self, username, password):
        self.login_args = (username, password)
        return "OK", [b"logged in"]

    def select(self, folder, readonly=True):
        self.selected = (folder, readonly)
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, command, *args):
        if command.lower() == "search":
            return "OK", [b" ".join(self.messages.keys())]
        if command.lower() == "fetch":
            uid = args[0]
            return "OK", [(b"1 (RFC822 {1}", self.messages[uid])]
        raise AssertionError(f"unexpected IMAP command: {command}")

    def logout(self):
        self.logged_out = True
        return "BYE", [b"logout"]


def _mailbox(tmp_path, messages):
    imap = FakeImap(messages)
    mailbox = DomainImapCatchallMailbox(
        domain="example.test",
        host="mail.example.test",
        username="catchall@example.test",
        password="secret",
        state_file=str(tmp_path / "state.json"),
        poll_interval=0,
        imap_factory=lambda _config: imap,
    )
    return mailbox, imap


def test_domain_imap_allocates_unique_addresses_and_records_delivery_metadata(tmp_path):
    mailbox, _ = _mailbox(tmp_path, {})

    first = mailbox.get_email()
    second = mailbox.get_email()

    assert first.email.endswith("@example.test")
    assert first.email.startswith("reg-")
    assert first.email != second.email
    assert first.extra["provider_resource"]["metadata"]["delivery"] == "catch_all_imap"


def test_domain_imap_waits_for_code_addressed_to_the_allocated_address(tmp_path):
    mailbox, _ = _mailbox(tmp_path, {})
    account = mailbox.get_email()
    other = "reg-other@example.test"
    mailbox, _ = _mailbox(
        tmp_path,
        {
            b"1": _raw_message(recipient=account.email, code="654321", message_id="<target>"),
            b"2": _raw_message(recipient=other, code="111111", message_id="<other>"),
        },
    )

    assert mailbox.wait_for_code(account, timeout=1) == "654321"


def test_domain_imap_connection_test_authenticates_and_selects_inbox(tmp_path):
    mailbox, imap = _mailbox(tmp_path, {})

    mailbox.test_connection()

    assert imap.login_args == ("catchall@example.test", "secret")
    assert imap.selected == ("INBOX", True)
    assert imap.logged_out
