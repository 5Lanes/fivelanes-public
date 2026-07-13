"""
Tests for services/digest/store.py — the day-scoped persistence backing "one briefing per
day" and permanent "Clear"/"Add to plans" dismissal (services/digest/build.py wraps this;
dashboard_server.py's ``/api/digest/dismiss`` calls ``dismiss_item`` directly). Isolated from
the real ``fivelanes-data/`` via ``FIVELANES_DATA_ROOT`` so tests never touch real user data.
"""
from datetime import date, timedelta

import pytest

from services.digest import store


@pytest.fixture(autouse=True)
def isolated_data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("FIVELANES_DATA_ROOT", str(tmp_path))
    yield


def test_load_daily_digest_returns_none_when_nothing_stored():
    assert store.load_daily_digest() is None


def test_save_then_load_same_day_round_trips():
    saved = store.save_daily_digest({"ok": True, "items": [{"id": "a", "text": "hi"}]})
    assert saved["date"] == date.today().isoformat()

    loaded = store.load_daily_digest()
    assert loaded is not None
    assert loaded["items"] == [{"id": "a", "text": "hi"}]


def test_load_daily_digest_ignores_a_stale_day():
    # A digest stored "today" must not be served once the calendar day has rolled over — this
    # is what forces build_digest_payload to regenerate exactly once per day.
    store.save_daily_digest({"ok": True, "items": []})
    tomorrow = date.today() + timedelta(days=1)
    assert store.load_daily_digest(as_of=tomorrow) is None


def test_dismiss_item_marks_matching_id_and_persists():
    store.save_daily_digest(
        {
            "ok": True,
            "items": [
                {"id": "keep", "text": "Keep me", "dismissed": False},
                {"id": "gone", "text": "Dismiss me", "dismissed": False},
            ],
        }
    )

    assert store.dismiss_item("gone") is True

    reloaded = store.load_daily_digest()
    by_id = {item["id"]: item for item in reloaded["items"]}
    assert by_id["gone"]["dismissed"] is True
    assert by_id["keep"]["dismissed"] is False


def test_dismiss_item_unknown_id_returns_false_and_changes_nothing():
    store.save_daily_digest({"ok": True, "items": [{"id": "keep", "text": "Keep me", "dismissed": False}]})

    assert store.dismiss_item("does-not-exist") is False

    reloaded = store.load_daily_digest()
    assert reloaded["items"][0]["dismissed"] is False


def test_dismiss_item_with_no_stored_digest_returns_false():
    assert store.dismiss_item("anything") is False


def test_item_id_is_stable_and_content_derived():
    assert store.item_id("Follow up with Paul Rios.") == store.item_id("Follow up with Paul Rios.")
    assert store.item_id("Follow up with Paul Rios.") != store.item_id("Follow up with Wade Merritt.")
