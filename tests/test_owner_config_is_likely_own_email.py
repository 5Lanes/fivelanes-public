"""Tests for utils.owner_config.is_likely_own_email."""
from utils import owner_config


def test_matches_exact_source_account(monkeypatch):
    monkeypatch.setenv("SOURCE_ACCOUNT", "owner@example.com")
    monkeypatch.delenv("OWNER_EMAIL_ALIASES", raising=False)
    assert owner_config.is_likely_own_email("owner@example.com") is True
    assert owner_config.is_likely_own_email("Owner@Example.com") is True


def test_matches_plus_tagged_variant(monkeypatch):
    monkeypatch.setenv("SOURCE_ACCOUNT", "owner@example.com")
    monkeypatch.delenv("OWNER_EMAIL_ALIASES", raising=False)
    assert owner_config.is_likely_own_email("owner+scheduling@example.com") is True


def test_matches_alias_domain_hint(monkeypatch):
    monkeypatch.setenv("SOURCE_ACCOUNT", "owner@example.com")
    monkeypatch.setenv("OWNER_EMAIL_ALIASES", "owner@company.io")
    assert owner_config.is_likely_own_email("owner@company.io") is True


def test_does_not_match_external_email(monkeypatch):
    monkeypatch.setenv("SOURCE_ACCOUNT", "owner@example.com")
    monkeypatch.delenv("OWNER_EMAIL_ALIASES", raising=False)
    assert owner_config.is_likely_own_email("counterparty@other.com") is False


def test_missing_source_account_does_not_raise(monkeypatch):
    monkeypatch.delenv("SOURCE_ACCOUNT", raising=False)
    monkeypatch.delenv("OWNER_EMAIL_ALIASES", raising=False)
    assert owner_config.is_likely_own_email("anyone@example.com") is False


def test_empty_or_invalid_email(monkeypatch):
    monkeypatch.setenv("SOURCE_ACCOUNT", "owner@example.com")
    assert owner_config.is_likely_own_email("") is False
    assert owner_config.is_likely_own_email("not-an-email") is False
