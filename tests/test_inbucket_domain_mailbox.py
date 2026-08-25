from __future__ import annotations

from core.inbucket_domain_mailbox import InbucketDomainMailbox


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


class _Session:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.urls = []

    def get(self, url, **_kwargs):
        self.urls.append(url)
        payload = self.payloads.pop(0) if len(self.payloads) > 1 else self.payloads[0]
        return _Response(payload)


def test_inbucket_domain_mailbox_generates_unique_addresses(tmp_path):
    mailbox = InbucketDomainMailbox(
        domain="example.test",
        state_file=str(tmp_path / "state.json"),
        session=_Session([[]]),
    )

    first = mailbox.get_email()
    second = mailbox.get_email()

    assert first.email.endswith("@example.test")
    assert first.email != second.email
    assert first.extra["provider_resource"]["metadata"]["delivery"] == "inbucket_smtp"


def test_inbucket_domain_mailbox_reads_the_new_verification_code(tmp_path):
    account = "reg-test@example.test"
    session = _Session([
        [{"id": "old"}],
        {"id": "old", "subject": "Old", "body": {"text": "111111", "html": ""}},
        [{"id": "new"}],
        {"id": "new", "subject": "Code", "body": {"text": "Verification code: 654321", "html": ""}},
    ])
    mailbox = InbucketDomainMailbox(
        domain="example.test",
        state_file=str(tmp_path / "state.json"),
        poll_interval=0,
        session=session,
    )
    from core.base_mailbox import MailboxAccount

    mailbox_account = MailboxAccount(email=account)
    before_ids = mailbox.get_current_ids(mailbox_account)

    assert mailbox.wait_for_code(mailbox_account, timeout=1, before_ids=before_ids) == "654321"
    assert any("reg-test%40example.test" in url for url in session.urls)


def test_inbucket_domain_mailbox_connection_test_checks_api(monkeypatch, tmp_path):
    monkeypatch.delenv("CHATGPT_INBUCKET_API_URL", raising=False)
    session = _Session([[]])
    mailbox = InbucketDomainMailbox(
        domain="example.test",
        state_file=str(tmp_path / "state.json"),
        session=session,
    )

    mailbox.test_connection()

    assert session.urls == ["http://127.0.0.1:9000/api/v1/mailbox/healthcheck%40example.test"]


def test_inbucket_domain_mailbox_uses_environment_api_override(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "CHATGPT_INBUCKET_API_URL",
        "http://127.0.0.1:19000/api/v1/",
    )

    mailbox = InbucketDomainMailbox.from_config(
        {
            "inbucket_domain": "example.test",
            "inbucket_api_url": "http://inbucket:9000/api/v1",
            "inbucket_state_file": str(tmp_path / "state.json"),
        }
    )

    assert mailbox.api_url == "http://127.0.0.1:19000/api/v1"
