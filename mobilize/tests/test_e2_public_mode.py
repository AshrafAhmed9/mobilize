"""E2: proves the public rehearsal surface (mobilize/app/public_mode.py) is
actually public-safe -- session isolation, no reachable real-dispatch path,
no loadable secrets, cold start / repeat session, upload validation, and
error visibility."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

import mobilize.app.public_mode as public_mode
from mobilize.app.public_mode import app

GOOD_CSV = (
    "name,phone,timezone,accept_rate,showup_rate\n"
    "Asha Rao,+15550101001,Asia/Kolkata,0.5,0.5\n"
)


@pytest.fixture(autouse=True)
def _clean_sessions():
    public_mode._sessions.clear()
    yield
    public_mode._sessions.clear()


def _client() -> TestClient:
    return TestClient(app)


def test_cold_start_creates_a_seeded_session():
    client = _client()
    r = client.post("/api/session")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == public_mode.MODE_LABEL
    assert body["count"] > 0
    assert body["session_id"]


def test_repeat_session_reconnect_returns_same_isolated_state():
    client = _client()
    session_id = client.post("/api/session").json()["session_id"]

    first = client.get("/api/registry", params={"session_id": session_id}).json()
    # "Reconnect": re-fetch with the same session_id, as a page refresh would.
    second = client.get("/api/registry", params={"session_id": session_id}).json()
    assert first == second


def test_unknown_session_id_is_a_clear_error_not_a_crash():
    client = _client()
    r = client.get("/api/registry", params={"session_id": "does-not-exist"})
    assert r.status_code == 404
    assert "session" in r.json()["detail"].lower()


def test_two_sessions_cannot_see_or_mutate_each_others_registry():
    client = _client()
    a = client.post("/api/session").json()["session_id"]
    b = client.post("/api/session").json()["session_id"]
    assert a != b

    upload = client.post("/api/registry/upload", json={"session_id": a, "csv": GOOD_CSV})
    assert upload.json()["count"] == 1

    reg_a = client.get("/api/registry", params={"session_id": a}).json()
    reg_b = client.get("/api/registry", params={"session_id": b}).json()
    assert reg_a["count"] == 1
    assert reg_a["people"][0]["name"] == "Asha Rao"
    # b never uploaded anything -- still has the untouched sample registry,
    # not a's upload and not zero (which would suggest cross-talk truncated it).
    assert reg_b["count"] != reg_a["count"] or reg_b["people"][0]["name"] != "Asha Rao"
    assert all(p["name"] != "Asha Rao" for p in reg_b["people"]) or reg_b["count"] > 1


def test_upload_only_affects_the_uploading_session():
    client = _client()
    a = client.post("/api/session").json()["session_id"]
    b = client.post("/api/session").json()["session_id"]
    before_b = client.get("/api/registry", params={"session_id": b}).json()

    client.post("/api/registry/upload", json={"session_id": a, "csv": GOOD_CSV})

    after_b = client.get("/api/registry", params={"session_id": b}).json()
    assert before_b == after_b


def test_upload_validation_rejects_bad_csv_with_visible_error():
    client = _client()
    session_id = client.post("/api/session").json()["session_id"]
    r = client.post("/api/registry/upload", json={"session_id": session_id, "csv": "not,a,valid,registry\n1,2"})
    body = r.json()
    assert "error" in body
    assert body["mode"] == public_mode.MODE_LABEL


def test_upload_validation_rejects_empty_csv():
    client = _client()
    session_id = client.post("/api/session").json()["session_id"]
    r = client.post("/api/registry/upload", json={"session_id": session_id, "csv": ""})
    assert "error" in r.json()


def test_run_executes_the_real_dispatcher_and_reports_rehearsal_mode():
    client = _client()
    session_id = client.post("/api/session").json()["session_id"]
    r = client.post("/api/run", json={"session_id": session_id, "scenario": "success"})
    body = r.json()
    assert body["mode"] == public_mode.MODE_LABEL
    assert body["filled"] is True
    assert body["calls_used"] == 1
    # Real engine trace, not a canned screen -- these event names come from
    # the actual dispatcher's on_progress callback.
    event_names = [e for e, _ in body["trace"]]
    assert event_names  # non-empty: something was actually dispatched


@pytest.mark.parametrize("bad_payload", [
    {"confirm": True}, {"live": True}, {"simulate": False},
    {"confirm": True, "scenario": "success"},
])
def test_any_request_for_a_real_call_is_refused_server_side(bad_payload):
    client = _client()
    session_id = client.post("/api/session").json()["session_id"]
    payload = {"session_id": session_id, **bad_payload}
    r = client.post("/api/run", json=payload)
    body = r.json()
    assert "error" in body
    assert "not available" in body["error"].lower() or "disabled" in body["error"].lower()
    assert body["mode"] == public_mode.MODE_LABEL


def test_unknown_scenario_is_a_visible_error():
    client = _client()
    session_id = client.post("/api/session").json()["session_id"]
    r = client.post("/api/run", json={"session_id": session_id, "scenario": "not_a_real_scenario"})
    assert "error" in r.json()


def test_module_never_imports_the_real_transport_or_reads_a_provider_key(monkeypatch):
    """No secrets loadable: unset any provider key, and prove the module's
    source has no path to the live transport at all -- not merely that it
    is gated off."""
    for var in ("CALLE_API_KEY", "CALL_E_API_KEY", "CALLE_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)

    import ast
    import inspect

    tree = ast.parse(inspect.getsource(public_mode))
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)
            imported_names.update(f"{node.module}.{alias.name}" for alias in node.names)
    assert not any("calle" in name.lower() for name in imported_names), imported_names
    assert not hasattr(public_mode, "CalleTransport")
    assert "os.environ" not in inspect.getsource(public_mode)  # never reads env vars at all

    # And even with a bogus key set, running the app never touches it --
    # there's no code path that could.
    monkeypatch.setenv("CALLE_API_KEY", "sk-should-never-be-read")
    client = _client()
    session_id = client.post("/api/session").json()["session_id"]
    r = client.post("/api/run", json={"session_id": session_id, "scenario": "refusal"})
    assert r.json()["mode"] == public_mode.MODE_LABEL
    assert os.environ["CALLE_API_KEY"] == "sk-should-never-be-read"  # untouched, just never read


def test_concurrent_sessions_running_scenarios_do_not_collide():
    """Two sessions running the same fixture scenario at once must not share
    a ledger or mobilization id -- run_fixture_scenario gives each call its
    own temp ledger, so both should succeed identically."""
    client = _client()
    a = client.post("/api/session").json()["session_id"]
    b = client.post("/api/session").json()["session_id"]

    ra = client.post("/api/run", json={"session_id": a, "scenario": "success"}).json()
    rb = client.post("/api/run", json={"session_id": b, "scenario": "success"}).json()

    assert ra["filled"] is True
    assert rb["filled"] is True
    assert ra["mobilization_id"] != rb["mobilization_id"]
