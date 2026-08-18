from types import SimpleNamespace

import pytest

from core.base_identity import MailboxIdentityProvider
from core.base_mailbox import MailboxAccount
from core.registration.adapters import BrowserRegistrationAdapter, ProtocolMailboxAdapter
from core.registration.flows import BrowserRegistrationFlow, ProtocolMailboxFlow
from core.registration.models import RegistrationContext, RegistrationResult


class TrackingMailbox:
    def __init__(self):
        self.account = MailboxAccount(email="leased@example.com")
        self.committed = []
        self.released = []

    def get_email(self):
        return self.account

    def get_current_ids(self, _account):
        return set()

    def commit_email(self, account):
        self.committed.append(account.email)
        return True

    def release_email(self, account):
        self.released.append(account.email)
        return True


def _context(mailbox):
    platform = SimpleNamespace(mailbox=mailbox)
    identity = SimpleNamespace(
        identity_provider="mailbox",
        email=mailbox.account.email,
        mailbox_account=mailbox.account,
        has_mailbox=True,
    )
    return RegistrationContext(
        platform_name="test",
        platform_display_name="Test",
        platform=platform,
        identity=identity,
        config=SimpleNamespace(executor_type="protocol", proxy=None, extra={}),
        email=None,
        password="password",
        log_fn=lambda _message: None,
    )


def test_protocol_flow_keeps_lease_until_outer_workflow_finishes():
    mailbox = TrackingMailbox()
    adapter = ProtocolMailboxAdapter(
        worker_builder=lambda _ctx, _artifacts: object(),
        register_runner=lambda _worker, _ctx, _artifacts: {"ok": True},
        result_mapper=lambda _ctx, _raw: RegistrationResult(
            email=mailbox.account.email,
            password="password",
        ),
    )

    result = ProtocolMailboxFlow(adapter).run(_context(mailbox))

    assert result.email == mailbox.account.email
    assert mailbox.committed == []
    assert mailbox.released == []
    assert mailbox.commit_email(mailbox.account) is True
    assert mailbox.committed == [mailbox.account.email]


def test_protocol_flow_releases_mailbox_on_registration_failure():
    mailbox = TrackingMailbox()

    def fail(_worker, _ctx, _artifacts):
        raise RuntimeError("network failed")

    adapter = ProtocolMailboxAdapter(
        worker_builder=lambda _ctx, _artifacts: object(),
        register_runner=fail,
        result_mapper=lambda _ctx, _raw: None,
    )

    with pytest.raises(RuntimeError, match="network failed"):
        ProtocolMailboxFlow(adapter).run(_context(mailbox))

    assert mailbox.committed == []
    assert mailbox.released == [mailbox.account.email]


def test_browser_preflight_failure_releases_mailbox():
    mailbox = TrackingMailbox()
    adapter = BrowserRegistrationAdapter(
        result_mapper=lambda _ctx, _raw: None,
        preflight=lambda _ctx: (_ for _ in ()).throw(RuntimeError("browser unavailable")),
    )

    with pytest.raises(RuntimeError, match="browser unavailable"):
        BrowserRegistrationFlow(adapter).run(_context(mailbox))

    assert mailbox.committed == []
    assert mailbox.released == [mailbox.account.email]


def test_identity_resolution_releases_lease_if_snapshot_fails():
    mailbox = TrackingMailbox()

    def fail_snapshot(_account):
        raise RuntimeError("mailbox snapshot failed")

    mailbox.get_current_ids = fail_snapshot

    with pytest.raises(RuntimeError, match="mailbox snapshot failed"):
        MailboxIdentityProvider(mailbox=mailbox).resolve()

    assert mailbox.released == [mailbox.account.email]
