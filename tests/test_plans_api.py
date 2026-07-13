"""
HTTP-level tests for the ``/api/plans/create`` and ``/api/plans/delete`` endpoints in
dashboard_server.py — the actual contract the frontend calls (JSON in, JSON out, status
codes), as opposed to tests/test_thread_plans.py which only exercises the utils/database.py
layer underneath. Added after a report that "Add to plans"/"removing plans isn't working" in
the UI; that turned out to be a frontend cache-staleness bug, not a backend one, but these
pin the HTTP contract itself so a real backend regression is still caught automatically.

Spins up the real ``DashboardHandler`` on an OS-assigned loopback port, pointed at an isolated
tmp database — never the real ``fivelanes-data/timeline.db``.
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import dashboard_server
from utils import database as db


@pytest.fixture
def server(tmp_path, monkeypatch):
    db_path = str(tmp_path / "fivelanes-test.db")
    db.ensure_database_schema(db_path)
    monkeypatch.setattr(dashboard_server, "DB_PATH", db_path)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), dashboard_server.DashboardHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", db_path
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def _post(base_url: str, path: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        base_url + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_create_plan_via_http_persists_to_db(server):
    base_url, db_path = server
    status, body = _post(
        base_url, "/api/plans/create", {"thread_id": "rfc:abc123", "action": "Follow up"}
    )

    assert status == 200
    assert body["ok"] is True
    assert body["plan"]["inbox_thread_id"] == "rfc:abc123"
    assert body["plan"]["action"] == "Follow up"

    plans = db.load_all_thread_plans(db_path)
    assert any(p["id"] == body["plan"]["id"] for p in plans)


def test_create_plan_via_http_missing_action_is_bad_request(server):
    base_url, _ = server
    status, body = _post(base_url, "/api/plans/create", {"thread_id": "rfc:abc123", "action": ""})

    assert status == 400
    assert body["ok"] is False


def test_delete_plan_via_http_removes_it(server):
    base_url, db_path = server
    _, created = _post(
        base_url, "/api/plans/create", {"thread_id": "rfc:abc123", "action": "Follow up"}
    )
    plan_id = created["plan"]["id"]

    status, body = _post(base_url, "/api/plans/delete", {"id": plan_id})

    assert status == 200
    assert body["ok"] is True
    assert not any(p["id"] == plan_id for p in db.load_all_thread_plans(db_path))


def test_delete_plan_via_http_unknown_id_is_not_found(server):
    base_url, _ = server
    status, body = _post(base_url, "/api/plans/delete", {"id": 999999})

    assert status == 404
    assert body["ok"] is False
    assert body["error"] == "plan_not_found"


def test_delete_plan_via_http_survives_a_db_layer_exception(server, monkeypatch):
    # Regression: _post_plan_delete used to call delete_thread_plan with no try/except at all
    # (every sibling handler — create, update — already wrapped its DB call). An exception here
    # — e.g. a SQLite busy-timeout while a background pipeline run holds a write lock — killed
    # the response before any access-log line was written, so a real failed delete was
    # indistinguishable from the browser never sending the request in the first place.
    def _boom(*args, **kwargs):
        raise RuntimeError("simulated db failure")

    monkeypatch.setattr(dashboard_server, "delete_thread_plan", _boom)
    base_url, _ = server

    status, body = _post(base_url, "/api/plans/delete", {"id": 1})

    assert status == 500
    assert body["ok"] is False


def test_create_then_delete_round_trip_leaves_no_trace(server):
    base_url, db_path = server
    _, created = _post(
        base_url, "/api/plans/create", {"thread_id": "rfc:xyz789", "action": "Review contract"}
    )
    plan_id = created["plan"]["id"]
    before_ids = {p["id"] for p in db.load_all_thread_plans(db_path)}
    assert plan_id in before_ids

    _post(base_url, "/api/plans/delete", {"id": plan_id})

    after_ids = {p["id"] for p in db.load_all_thread_plans(db_path)}
    assert after_ids == before_ids - {plan_id}
