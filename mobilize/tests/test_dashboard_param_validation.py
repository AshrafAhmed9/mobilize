"""Lane D / A4: the WebSocket run endpoint (`/ws/run`) had no server-side
schema enforcement on client-supplied parameters -- it trusted the browser's
own input types. Two concrete, exploitable gaps this covers:

1. Type confusion on the security-critical `confirm`/`simulate` flags.
   Python's bool() coerces any non-empty string to True, including the
   string "false": `bool("false") is True`. A client (or a compromised
   trusted-origin page) sending {"confirm": "false"} as a JSON string was
   silently treated as confirm:true by the old `bool(params.get(...))`
   code, bypassing the real-dispatch confirmation gate entirely.
2. No bounds checking on need_count/max_calls/deadline_minutes before they
   reach Need/mobilize(). A non-positive need_count makes dispatcher.py's
   `len(confirmed) >= need.count` true before any call is placed, reporting
   a need as "filled" with zero confirmations and zero calls used.

These tests exercise the actual FastAPI WebSocket endpoint, not the
validator function in isolation.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import mobilize.app.dashboard as dashboard_module
from mobilize.app.dashboard import _validate_run_params, app

TRUSTED_ORIGIN = "http://127.0.0.1:8731"


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_module, "REGISTRY_STATE_PATH", tmp_path / "registry.json")
    monkeypatch.setattr(dashboard_module, "REGISTRY_SOURCE_MARKER_PATH", tmp_path / "registry_source.txt")
    monkeypatch.setattr(dashboard_module, "GOVERNANCE_STATE_PATH", tmp_path / "governance.json")
    yield


def test_confirm_as_truthy_string_is_rejected_not_coerced_to_true(monkeypatch):
    """The core type-confusion bug: {"confirm": "false"} must NOT be
    treated as confirm:true. bool("false") is True in Python, so the old
    `bool(params.get("confirm", False))` code would have silently placed
    real calls here. CalleTransport must never even be constructed."""
    class _ExplodingCalleTransport:
        def __init__(self, *a, **kw):
            raise AssertionError("CalleTransport must never be constructed for a malformed confirm value")

    monkeypatch.setattr("mobilize.transports.calle.CalleTransport", _ExplodingCalleTransport)

    client = TestClient(app)
    with client.websocket_connect("/ws/run", headers={"origin": TRUSTED_ORIGIN}) as ws:
        ws.send_json({
            "need_label": "test", "need_count": 1, "deadline_minutes": 60,
            "max_calls": 5, "simulate": False, "confirm": "false",
        })
        msg = ws.receive_json()

    assert msg["event"] == "error"
    assert "confirm" in msg["data"]["message"].lower()


def test_simulate_as_string_is_rejected():
    """Same type-confusion class on `simulate`: a string value must be
    rejected rather than silently coerced true/false by Python's bool()."""
    client = TestClient(app)
    with client.websocket_connect("/ws/run", headers={"origin": TRUSTED_ORIGIN}) as ws:
        ws.send_json({
            "need_label": "test", "need_count": 1, "deadline_minutes": 60,
            "max_calls": 5, "simulate": "true",
        })
        msg = ws.receive_json()

    assert msg["event"] == "error"
    assert "simulate" in msg["data"]["message"].lower()


@pytest.mark.parametrize("need_count", [0, -1, -1000])
def test_non_positive_need_count_is_rejected(need_count):
    """A non-positive need_count would otherwise make dispatcher.py report
    the need as filled before any call is placed."""
    client = TestClient(app)
    with client.websocket_connect("/ws/run", headers={"origin": TRUSTED_ORIGIN}) as ws:
        ws.send_json({
            "need_label": "test", "need_count": need_count, "deadline_minutes": 60,
            "max_calls": 5, "simulate": True,
        })
        msg = ws.receive_json()

    assert msg["event"] == "error"
    assert "need_count" in msg["data"]["message"]


@pytest.mark.parametrize("max_calls", [0, -5])
def test_non_positive_max_calls_is_rejected(max_calls):
    client = TestClient(app)
    with client.websocket_connect("/ws/run", headers={"origin": TRUSTED_ORIGIN}) as ws:
        ws.send_json({
            "need_label": "test", "need_count": 1, "deadline_minutes": 60,
            "max_calls": max_calls, "simulate": True,
        })
        msg = ws.receive_json()

    assert msg["event"] == "error"
    assert "max_calls" in msg["data"]["message"]


@pytest.mark.parametrize("deadline", [0, -30])
def test_non_positive_deadline_minutes_is_rejected(deadline):
    client = TestClient(app)
    with client.websocket_connect("/ws/run", headers={"origin": TRUSTED_ORIGIN}) as ws:
        ws.send_json({
            "need_label": "test", "need_count": 1, "deadline_minutes": deadline,
            "max_calls": 5, "simulate": True,
        })
        msg = ws.receive_json()

    assert msg["event"] == "error"
    assert "deadline_minutes" in msg["data"]["message"]


def test_need_count_exceeding_max_calls_is_rejected():
    client = TestClient(app)
    with client.websocket_connect("/ws/run", headers={"origin": TRUSTED_ORIGIN}) as ws:
        ws.send_json({
            "need_label": "test", "need_count": 100, "deadline_minutes": 60,
            "max_calls": 5, "simulate": True,
        })
        msg = ws.receive_json()

    assert msg["event"] == "error"


def test_non_string_need_label_is_rejected():
    client = TestClient(app)
    with client.websocket_connect("/ws/run", headers={"origin": TRUSTED_ORIGIN}) as ws:
        ws.send_json({
            "need_label": 12345, "need_count": 1, "deadline_minutes": 60,
            "max_calls": 5, "simulate": True,
        })
        msg = ws.receive_json()

    assert msg["event"] == "error"


def test_valid_params_still_run_successfully():
    """Confirms the validator isn't overly strict for the legitimate,
    well-typed case the real UI actually sends."""
    client = TestClient(app)
    with client.websocket_connect("/ws/run", headers={"origin": TRUSTED_ORIGIN}) as ws:
        ws.send_json({
            "need_label": "test", "need_count": 1, "deadline_minutes": 60,
            "max_calls": 5, "simulate": True, "confirm": False,
        })
        events = []
        while True:
            msg = ws.receive_json()
            events.append(msg["event"])
            if msg["event"] == "final":
                break
    assert "final" in events


def test_validator_function_directly_rejects_bool_as_int_need_count():
    """Python's bool is a subclass of int, so isinstance(True, int) is
    True -- a naive `isinstance(x, int)` check would accept need_count:true
    as if it were 1. Guard against that specific footgun explicitly."""
    with pytest.raises(ValueError):
        _validate_run_params({"need_count": True, "max_calls": 5, "deadline_minutes": 60})
