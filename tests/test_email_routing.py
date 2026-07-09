"""
Tests for email inbox routing and thread-identity resolution.

Covers the To:-is-always-a-forward / Cc-Bcc-is-a-live-thread business rule and the
deterministic ``unresolved:`` fallback when RFC extraction fails (see PRD.md).
"""
from services.email.config import SOURCE_OAUTH_ACCOUNT_ID
from services.email.inbox_process import build_tracking_row
from services.email.inbox_route import (
    RFC_THREAD_PREFIX,
    UNRESOLVED_THREAD_PREFIX,
    InboxRoute,
    cc_bcc_fivelanes_thread_id,
    gmail_inbox_thread_id_for_tracking,
    is_fivelanes_derived_thread_id,
    is_rfc_fivelanes_thread_id,
    is_unresolved_fivelanes_thread_id,
    route_inbox_message,
)

INBOX = "source@fivelanes.example"


def _msg(**overrides):
    base = {
        "subject": "Re: quarterly plan",
        "recipients": {"to": "", "cc": "", "bcc": ""},
    }
    base.update(overrides)
    return base


class TestRouteInboxMessage:
    def test_to_match_without_todo_subject_is_always_forward(self):
        # A To: match is a forward even when the body has no embedded forward block —
        # there is no legitimate "direct" delivery case.
        m = _msg(recipients={"to": INBOX, "cc": "", "bcc": ""}, body="just some prose")
        assert route_inbox_message(m, INBOX) == InboxRoute.FORWARD_TO

    def test_to_match_with_todo_subject_is_todo_plan(self):
        m = _msg(recipients={"to": INBOX, "cc": "", "bcc": ""}, subject="Todo: renew passport")
        assert route_inbox_message(m, INBOX) == InboxRoute.TODO_PLAN

    def test_cc_only_match_is_cc_bcc(self):
        m = _msg(recipients={"to": "someone@else.example", "cc": INBOX, "bcc": ""})
        assert route_inbox_message(m, INBOX) == InboxRoute.CC_BCC

    def test_bcc_only_match_is_cc_bcc(self):
        m = _msg(recipients={"to": "someone@else.example", "cc": "", "bcc": INBOX})
        assert route_inbox_message(m, INBOX) == InboxRoute.CC_BCC

    def test_no_match_falls_back_to_direct_to(self):
        # Defensive fallback only; not reachable from real inbox-search results.
        m = _msg(recipients={"to": "someone@else.example", "cc": "", "bcc": ""})
        assert route_inbox_message(m, INBOX) == InboxRoute.DIRECT_TO


class TestThreadIdPrefixHelpers:
    def test_cc_bcc_fivelanes_thread_id_empty_without_ref(self):
        assert cc_bcc_fivelanes_thread_id("") == ""

    def test_cc_bcc_fivelanes_thread_id_prefixes_ref(self):
        assert cc_bcc_fivelanes_thread_id("<abc@example.com>") == f"{RFC_THREAD_PREFIX}abc@example.com"

    def test_is_rfc_fivelanes_thread_id(self):
        assert is_rfc_fivelanes_thread_id(f"{RFC_THREAD_PREFIX}abc") is True
        assert is_rfc_fivelanes_thread_id(f"{UNRESOLVED_THREAD_PREFIX}acct:msg") is False
        assert is_rfc_fivelanes_thread_id("18abcgmailthreadid") is False

    def test_is_unresolved_fivelanes_thread_id(self):
        assert is_unresolved_fivelanes_thread_id(f"{UNRESOLVED_THREAD_PREFIX}acct:msg") is True
        assert is_unresolved_fivelanes_thread_id(f"{RFC_THREAD_PREFIX}abc") is False

    def test_is_fivelanes_derived_thread_id_covers_both_prefixes(self):
        assert is_fivelanes_derived_thread_id(f"{RFC_THREAD_PREFIX}abc") is True
        assert is_fivelanes_derived_thread_id(f"{UNRESOLVED_THREAD_PREFIX}acct:msg") is True
        assert is_fivelanes_derived_thread_id("18abcgmailthreadid") is False

    def test_gmail_inbox_thread_id_for_tracking_prefers_stored_value(self):
        row = {"gmail_inbox_thread_id": "18gmail123", "inbox_thread_id": f"{RFC_THREAD_PREFIX}abc"}
        assert gmail_inbox_thread_id_for_tracking(row) == "18gmail123"

    def test_gmail_inbox_thread_id_for_tracking_blank_for_derived_id_without_stored_value(self):
        row = {"gmail_inbox_thread_id": "", "inbox_thread_id": f"{UNRESOLVED_THREAD_PREFIX}acct:msg"}
        assert gmail_inbox_thread_id_for_tracking(row) == ""

    def test_gmail_inbox_thread_id_for_tracking_falls_back_to_raw_id(self):
        row = {"gmail_inbox_thread_id": "", "inbox_thread_id": "18gmail123"}
        assert gmail_inbox_thread_id_for_tracking(row) == "18gmail123"


class TestBuildTrackingRow:
    def test_missing_thread_id_returns_none(self):
        m = _msg(forwarder_email="a@b.example")
        assert build_tracking_row(m, InboxRoute.FORWARD_TO, now_iso="2026-01-01T00:00:00+00:00") is None

    def test_missing_forwarder_email_returns_none(self):
        m = _msg(thread_id="18gmail123")
        assert build_tracking_row(m, InboxRoute.FORWARD_TO, now_iso="2026-01-01T00:00:00+00:00") is None

    def test_forward_with_resolvable_rfc_uses_rfc_prefixed_id(self):
        m = _msg(
            thread_id="18gmail123",
            forwarder_email="original-sender@example.com",
            message_id="gmailmsg1",
            body="no embedded forward block here",
            header_in_reply_to="<abc@originaldomain.com>",
        )
        row = build_tracking_row(m, InboxRoute.FORWARD_TO, now_iso="2026-01-01T00:00:00+00:00")
        assert row["inbox_thread_id"] == f"{RFC_THREAD_PREFIX}abc@originaldomain.com"
        assert row["gmail_inbox_thread_id"] == "18gmail123"
        assert row["resolution_error"] == ""

    def test_forward_with_unresolvable_rfc_uses_deterministic_unresolved_id(self):
        m = _msg(
            thread_id="18gmail123",
            forwarder_email="original-sender@example.com",
            message_id="gmailmsg1",
            body="no embedded forward, no headers",
        )
        row = build_tracking_row(m, InboxRoute.FORWARD_TO, now_iso="2026-01-01T00:00:00+00:00")
        expected = f"{UNRESOLVED_THREAD_PREFIX}{SOURCE_OAUTH_ACCOUNT_ID}:gmailmsg1"
        assert row["inbox_thread_id"] == expected
        assert row["gmail_inbox_thread_id"] == "18gmail123"
        assert row["resolution_error"] == "rfc_extraction_failed"

    def test_forward_unresolved_id_is_deterministic_across_runs(self):
        m = _msg(
            thread_id="18gmail123",
            forwarder_email="original-sender@example.com",
            message_id="gmailmsg1",
            body="no embedded forward, no headers",
        )
        row1 = build_tracking_row(m, InboxRoute.FORWARD_TO, now_iso="2026-01-01T00:00:00+00:00")
        row2 = build_tracking_row(m, InboxRoute.FORWARD_TO, now_iso="2026-01-02T00:00:00+00:00")
        assert row1["inbox_thread_id"] == row2["inbox_thread_id"]

    def test_cc_bcc_keeps_raw_thread_id_even_when_rfc_resolvable(self):
        # Cc/Bcc is a live copy of an existing thread — no identity re-derivation.
        m = _msg(
            thread_id="18gmail456",
            forwarder_email="original-sender@example.com",
            message_id="gmailmsg2",
            body="",
            header_in_reply_to="<abc@originaldomain.com>",
        )
        row = build_tracking_row(m, InboxRoute.CC_BCC, now_iso="2026-01-01T00:00:00+00:00")
        assert row["inbox_thread_id"] == "18gmail456"
        assert "gmail_inbox_thread_id" not in row
        assert row["resolution_error"] == ""
        # inner_rfc_message_id is still recorded for later enrichment, just not promoted.
        assert row["inner_rfc_message_id"] == "abc@originaldomain.com"
