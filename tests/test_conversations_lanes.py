"""
Tests for the ``conversations`` / ``lane_conversations`` layer in utils/database.py.

Lanes point at conversations (many-to-many, one conversation can span several threads
across sources) instead of threads directly. Every existing thread gets its own
auto-created 1:1 conversation; the external ``/api/lanes/*`` contract — in particular
``load_lane_thread_memberships``'s ``{lane_id: [thread_ids]}`` shape, which the Slack/
Text/LinkedIn/Meet "visible" tracking logic also reads directly — must not change.
"""
import sqlite3

import pytest

from utils import database as db


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "fivelanes-test.db")
    db.ensure_database_schema(path)
    return path


def test_new_tables_are_created(db_path):
    conn = sqlite3.connect(db_path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"conversations", "conversation_threads", "lane_conversations"} <= tables


def test_add_thread_to_lane_creates_conversation_and_membership(db_path):
    lane = db.create_lane(db_path, name="Test Lane")
    assert db.add_thread_to_lane(db_path, lane_id=lane["id"], inbox_thread_id="rfc:abc123")

    memberships = db.load_lane_thread_memberships(db_path)
    assert memberships == {str(lane["id"]): ["rfc:abc123"]}
    assert db.lane_ids_for_thread(db_path, "rfc:abc123") == [lane["id"]]


def test_add_thread_to_lane_is_source_agnostic(db_path):
    lane = db.create_lane(db_path, name="Test Lane")
    db.add_thread_to_lane(db_path, lane_id=lane["id"], inbox_thread_id="rfc:abc123")
    db.add_thread_to_lane(db_path, lane_id=lane["id"], inbox_thread_id="slack:xyz789")

    memberships = db.load_lane_thread_memberships(db_path)
    assert memberships[str(lane["id"])] == ["rfc:abc123", "slack:xyz789"]


def test_add_thread_to_lane_missing_lane_returns_false(db_path):
    assert db.add_thread_to_lane(db_path, lane_id=99999, inbox_thread_id="rfc:abc123") is False


def test_remove_thread_from_lane(db_path):
    lane = db.create_lane(db_path, name="Test Lane")
    db.add_thread_to_lane(db_path, lane_id=lane["id"], inbox_thread_id="rfc:abc123")

    assert db.remove_thread_from_lane(db_path, lane_id=lane["id"], inbox_thread_id="rfc:abc123")
    assert db.load_lane_thread_memberships(db_path) == {}
    assert db.lane_ids_for_thread(db_path, "rfc:abc123") == []


def test_delete_lane_cascades_lane_conversations(db_path):
    lane = db.create_lane(db_path, name="Test Lane")
    db.add_thread_to_lane(db_path, lane_id=lane["id"], inbox_thread_id="rfc:abc123")

    assert db.delete_lane(db_path, lane_id=lane["id"])
    assert db.load_lane_thread_memberships(db_path) == {}

    conn = sqlite3.connect(db_path)
    remaining = conn.execute("SELECT COUNT(*) FROM lane_conversations").fetchone()[0]
    conn.close()
    assert remaining == 0


def test_same_thread_in_two_lanes_shares_one_conversation(db_path):
    lane_a = db.create_lane(db_path, name="Lane A")
    lane_b = db.create_lane(db_path, name="Lane B")
    db.add_thread_to_lane(db_path, lane_id=lane_a["id"], inbox_thread_id="rfc:shared")
    db.add_thread_to_lane(db_path, lane_id=lane_b["id"], inbox_thread_id="rfc:shared")

    conn = sqlite3.connect(db_path)
    conversation_ids = {
        row[0]
        for row in conn.execute(
            "SELECT conversation_id FROM conversation_threads WHERE inbox_thread_id = ?",
            ("rfc:shared",),
        )
    }
    conn.close()
    assert len(conversation_ids) == 1
    assert sorted(db.lane_ids_for_thread(db_path, "rfc:shared")) == sorted([lane_a["id"], lane_b["id"]])


def test_remap_dashboard_thread_id_merges_spurious_conversations(db_path):
    # Simulates the Phase-1 duplicate-collapse path: two thread_tracking rows for what
    # turns out to be the same physical thread each got their own auto-created conversation.
    lane_a = db.create_lane(db_path, name="Lane A")
    lane_b = db.create_lane(db_path, name="Lane B")
    db.add_thread_to_lane(db_path, lane_id=lane_a["id"], inbox_thread_id="unresolved:acct:msg1")
    db.add_thread_to_lane(db_path, lane_id=lane_b["id"], inbox_thread_id="rfc:realid")

    db.remap_dashboard_thread_id(db_path, "unresolved:acct:msg1", "rfc:realid")

    memberships = db.load_lane_thread_memberships(db_path)
    assert memberships == {
        str(lane_a["id"]): ["rfc:realid"],
        str(lane_b["id"]): ["rfc:realid"],
    }

    conn = sqlite3.connect(db_path)
    conversation_ids = {
        row[0]
        for row in conn.execute("SELECT DISTINCT conversation_id FROM conversation_threads")
    }
    orphaned = conn.execute(
        "SELECT COUNT(*) FROM conversation_threads WHERE inbox_thread_id = ?",
        ("unresolved:acct:msg1",),
    ).fetchone()[0]
    conn.close()
    assert len(conversation_ids) == 1  # the two spurious conversations collapsed into one
    assert orphaned == 0


def test_backfill_migrates_pre_existing_lane_threads_data(tmp_path):
    # Simulate a database created before the conversations layer existed.
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE lanes (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE lane_threads (lane_id INTEGER NOT NULL, inbox_thread_id TEXT NOT NULL, "
        "created_at TEXT NOT NULL, PRIMARY KEY (lane_id, inbox_thread_id))"
    )
    conn.execute("INSERT INTO lanes (id, name, created_at, updated_at) VALUES (1, 'Legacy Lane', 'x', 'x')")
    conn.execute("INSERT INTO lane_threads (lane_id, inbox_thread_id, created_at) VALUES (1, 'rfc:legacy1', 'x')")
    conn.commit()
    conn.close()

    db.ensure_database_schema(path)

    assert db.load_lane_thread_memberships(path) == {"1": ["rfc:legacy1"]}


def test_link_thread_to_matching_thread_groups_into_same_conversation(db_path):
    lane = db.create_lane(db_path, name="Test Lane")
    db.add_thread_to_lane(db_path, lane_id=lane["id"], inbox_thread_id="rfc:email123")

    conversation_id = db.link_thread_to_matching_thread(
        db_path, inbox_thread_id="cal:event1", matched_inbox_thread_id="rfc:email123"
    )

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT inbox_thread_id FROM conversation_threads WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchall()
    conn.close()
    assert {r[0] for r in rows} == {"rfc:email123", "cal:event1"}
    # The calendar thread now surfaces in the same lane as the email thread it was matched to.
    assert lane["id"] in db.lane_ids_for_thread(db_path, "cal:event1")


def test_link_thread_to_matching_thread_missing_ids_is_noop(db_path):
    assert db.link_thread_to_matching_thread(db_path, inbox_thread_id="", matched_inbox_thread_id="rfc:x") == 0
    assert db.link_thread_to_matching_thread(db_path, inbox_thread_id="cal:x", matched_inbox_thread_id="") == 0
