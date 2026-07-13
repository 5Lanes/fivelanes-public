"""
Tests for the thread_plans create/delete layer in utils/database.py — the functions
dashboard_server.py's ``/api/plans/create`` and ``/api/plans/delete`` wrap directly. Added
after a report that "removing plans isn't working" in the UI; these pin the backend contract
so a regression here (as opposed to a frontend cache-staleness bug) is caught automatically.
"""
import pytest

from utils import database as db


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "fivelanes-test.db")
    db.ensure_database_schema(path)
    return path


def test_create_then_delete_thread_plan_round_trip(db_path):
    plan = db.create_thread_plan(db_path, inbox_thread_id="rfc:abc123", action="Follow up")
    assert plan["inbox_thread_id"] == "rfc:abc123"
    assert plan["action"] == "Follow up"
    assert any(p["id"] == plan["id"] for p in db.load_all_thread_plans(db_path))

    assert db.delete_thread_plan(db_path, plan_id=plan["id"]) is True
    assert not any(p["id"] == plan["id"] for p in db.load_all_thread_plans(db_path))


def test_delete_thread_plan_missing_id_returns_false(db_path):
    assert db.delete_thread_plan(db_path, plan_id=999999) is False


def test_delete_thread_plan_is_idempotent(db_path):
    plan = db.create_thread_plan(db_path, inbox_thread_id="rfc:abc123", action="Follow up")
    assert db.delete_thread_plan(db_path, plan_id=plan["id"]) is True
    # Deleting again (e.g. a double-click, or a retried request) must not error or resurrect it.
    assert db.delete_thread_plan(db_path, plan_id=plan["id"]) is False


def test_create_thread_plan_requires_action(db_path):
    with pytest.raises(ValueError):
        db.create_thread_plan(db_path, inbox_thread_id="rfc:abc123", action="")


def test_delete_thread_plan_only_removes_the_targeted_row(db_path):
    keep = db.create_thread_plan(db_path, inbox_thread_id="rfc:keep", action="Keep me")
    remove = db.create_thread_plan(db_path, inbox_thread_id="rfc:remove", action="Remove me")

    assert db.delete_thread_plan(db_path, plan_id=remove["id"]) is True

    remaining_ids = {p["id"] for p in db.load_all_thread_plans(db_path)}
    assert remaining_ids == {keep["id"]}
