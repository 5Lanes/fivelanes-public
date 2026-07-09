"""Tests for services.calendar_events.matching: attendee/topic/date matching logic."""
import pytest

from services.calendar_events import matching as m


@pytest.fixture(autouse=True)
def no_owner_emails(monkeypatch):
    """By default, nothing is treated as the owner's own address."""
    monkeypatch.setattr(m, "is_likely_own_email", lambda email: False)


def test_normalize_email_strips_plus_tag_and_lowercases():
    assert m.normalize_email("Alice+Scheduling@Example.com") == "alice@example.com"


def test_extract_emails_from_text_plain_header():
    assert m.extract_emails_from_text("Alice <alice@example.com>, bob@example.com") == [
        "alice@example.com",
        "bob@example.com",
    ]


def test_extract_emails_from_text_json_recipients_blob():
    raw = '{"to": "alice@example.com", "cc": "bob@example.com"}'
    assert set(m.extract_emails_from_text(raw)) == {"alice@example.com", "bob@example.com"}


def test_extract_emails_from_text_json_array_falls_back_to_regex():
    raw = '["alice@example.com", "bob@example.com"]'
    assert set(m.extract_emails_from_text(raw)) == {"alice@example.com", "bob@example.com"}


def test_external_emails_excludes_owner(monkeypatch):
    monkeypatch.setattr(m, "is_likely_own_email", lambda email: email == "owner@example.com")
    result = m.external_emails(["owner@example.com", "counterparty@example.com"])
    assert result == {"counterparty@example.com"}


def test_find_matching_conversation_thread_by_attendee_overlap():
    ctx_a = m.ThreadMatchContext(
        thread_id="rfc:a", label="Budget review", snoozed=0, latest_iso="2026-07-01",
        contact_emails=["alice@example.com"],
    )
    ctx_b = m.ThreadMatchContext(
        thread_id="rfc:b", label="Onboarding", snoozed=0, latest_iso="2026-07-01",
        contact_emails=["bob@example.com", "carol@example.com"],
    )
    match = m.find_matching_conversation_thread(
        ["bob@example.com", "carol@example.com"], [ctx_a, ctx_b]
    )
    assert match is not None and match.thread_id == "rfc:b"


def test_find_matching_conversation_thread_no_overlap_returns_none():
    ctx = m.ThreadMatchContext(
        thread_id="rfc:a", label="", snoozed=0, latest_iso="", contact_emails=["alice@example.com"]
    )
    assert m.find_matching_conversation_thread(["zed@example.com"], [ctx]) is None


def test_find_matching_conversation_thread_skips_removed_threads():
    ctx_removed = m.ThreadMatchContext(
        thread_id="rfc:removed", label="", snoozed=2, latest_iso="",
        contact_emails=["alice@example.com"],
    )
    assert m.find_matching_conversation_thread(["alice@example.com"], [ctx_removed]) is None


def test_find_matching_conversation_thread_tie_broken_by_topic():
    ctx_a = m.ThreadMatchContext(
        thread_id="rfc:budget", label="Q3 budget planning", snoozed=0, latest_iso="2026-07-01",
        contact_emails=["alice@example.com"],
    )
    ctx_b = m.ThreadMatchContext(
        thread_id="rfc:onboarding", label="New hire onboarding", snoozed=0, latest_iso="2026-07-01",
        contact_emails=["alice@example.com"],
    )
    match = m.find_matching_conversation_thread(
        ["alice@example.com"],
        [ctx_a, ctx_b],
        meeting_summary="Q3 budget planning sync",
    )
    assert match is not None and match.thread_id == "rfc:budget"


def test_find_matching_conversation_thread_tie_broken_by_date_proximity():
    ctx_far = m.ThreadMatchContext(
        thread_id="rfc:far", label="", snoozed=0, latest_iso="2026-01-01T00:00:00+00:00",
        contact_emails=["alice@example.com"],
    )
    ctx_near = m.ThreadMatchContext(
        thread_id="rfc:near", label="", snoozed=0, latest_iso="2026-07-08T12:00:00+00:00",
        contact_emails=["alice@example.com"],
    )
    match = m.find_matching_conversation_thread(
        ["alice@example.com"],
        [ctx_far, ctx_near],
        meeting_start_iso="2026-07-09T12:00:00+00:00",
    )
    assert match is not None and match.thread_id == "rfc:near"
