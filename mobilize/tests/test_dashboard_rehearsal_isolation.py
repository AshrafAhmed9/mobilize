"""D1: rehearsal (simulated) runs must never mutate the operational registry
or governance state. Before this fix, `run_mobilization` called
`record_outcomes` and `save_registry_json(REGISTRY_STATE_PATH)` after BOTH
rehearsal and live runs -- so every free rehearsal silently overwrote real
accept/show-up rates and counters. These tests prove byte-for-byte isolation
across a rehearsal run, that live runs still persist as before, and that
repeated rehearsals are deterministic after the (no-op) reset.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import mobilize.app.dashboard as dashboard_module
from mobilize.app.dashboard import app
from mobilize.core.registry import save_registry_json

TRUSTED_ORIGIN = "http://127.0.0.1:8731"

SAMPLE_CSV = (
    "name,phone,timezone,accept_rate,showup_rate\n"
    "Asha Rao,+15550101001,Asia/Kolkata,0.5,0.5\n"
    "Ben Lee,+15550101002,America/New_York,0.5,0.5\n"
)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_module, "REGISTRY_STATE_PATH", tmp_path / "registry.json")
    monkeypatch.setattr(dashboard_module, "REGISTRY_SOURCE_MARKER_PATH", tmp_path / "registry_source.txt")
    monkeypatch.setattr(dashboard_module, "GOVERNANCE_STATE_PATH", tmp_path / "governance.json")
    yield


def _seed_registry():
    # Seed REGISTRY_STATE_PATH directly via save_registry_json, matching
    # what a coordinator's persisted state actually looks like on disk.
    from io import StringIO
    import csv as csv_module
    from mobilize.core.registry import Registry, Person

    reader = csv_module.DictReader(StringIO(SAMPLE_CSV))
    registry = Registry()
    for row in reader:
        registry.people[row["name"]] = Person(
            id=row["name"], name=row["name"], phone=row["phone"], timezone=row["timezone"],
            accept_rate=float(row["accept_rate"]), showup_rate=float(row["showup_rate"]),
        )
    save_registry_json(registry, dashboard_module.REGISTRY_STATE_PATH)
    dashboard_module.REGISTRY_SOURCE_MARKER_PATH.write_text("uploaded")


def _run_ws(simulate: bool, confirm: bool = False):
    client = TestClient(app)
    events = []
    with client.websocket_connect("/ws/run", headers={"origin": TRUSTED_ORIGIN}) as ws:
        ws.send_json({
            "need_label": "test", "need_count": 5, "deadline_minutes": 60,
            "max_calls": 10, "simulate": simulate, "confirm": confirm,
        })
        while True:
            msg = ws.receive_json()
            events.append(msg)
            if msg["event"] in ("final", "error"):
                break
    return events


def test_rehearsal_does_not_touch_registry_state_file_bytes():
    _seed_registry()
    before = dashboard_module.REGISTRY_STATE_PATH.read_bytes()

    events = _run_ws(simulate=True)
    assert events[-1]["event"] == "final"
    assert events[-1]["data"]["mode"] == "rehearsal"

    after = dashboard_module.REGISTRY_STATE_PATH.read_bytes()
    assert after == before


def test_rehearsal_does_not_create_governance_state_file():
    _seed_registry()
    assert not dashboard_module.GOVERNANCE_STATE_PATH.exists()

    _run_ws(simulate=True)

    assert not dashboard_module.GOVERNANCE_STATE_PATH.exists()


def test_repeated_rehearsal_leaves_identical_bytes_each_time():
    _seed_registry()
    baseline = dashboard_module.REGISTRY_STATE_PATH.read_bytes()

    for _ in range(3):
        events = _run_ws(simulate=True)
        assert events[-1]["data"]["mode"] == "rehearsal"
        assert dashboard_module.REGISTRY_STATE_PATH.read_bytes() == baseline


def test_final_event_reports_active_mode_for_rehearsal_and_live(monkeypatch):
    _seed_registry()
    rehearsal_events = _run_ws(simulate=True)
    assert rehearsal_events[-1]["data"]["mode"] == "rehearsal"

    async def _fake_mobilize(need, candidates, transport, **kwargs):
        from mobilize.core.types import MobilizeResult
        return MobilizeResult(need=need, confirmed=[], all_results=[], waves=[],
                               calls_used=0, time_to_fill_seconds=None, filled=False,
                               over_recruitment_ratio=0.0)

    class _FakeCalleTransport:
        def __init__(self, *a, **kw):
            pass

    monkeypatch.setattr(dashboard_module, "mobilize", _fake_mobilize)
    monkeypatch.setattr("mobilize.transports.calle.CalleTransport", _FakeCalleTransport)

    live_events = _run_ws(simulate=False, confirm=True)
    assert live_events[-1]["data"]["mode"] == "live"


def test_live_run_still_persists_registry_state(monkeypatch):
    """Live behavior must be unchanged by the D1 fix: real runs still write
    REGISTRY_STATE_PATH."""
    _seed_registry()
    before = dashboard_module.REGISTRY_STATE_PATH.read_bytes()

    async def _fake_mobilize(need, candidates, transport, **kwargs):
        from mobilize.core.types import CallOutcome, CallResult, MobilizeResult

        result = CallResult(call_id="c1", candidate_id="Asha Rao", outcome=CallOutcome.FIRM_YES,
                             commitment_score=0.9, stated_yes=True, evidence="yes")
        return MobilizeResult(need=need, confirmed=[result], all_results=[result], waves=[],
                               calls_used=1, time_to_fill_seconds=1.0, filled=True,
                               over_recruitment_ratio=0.0)

    class _FakeCalleTransport:
        def __init__(self, *a, **kw):
            pass

    monkeypatch.setattr(dashboard_module, "mobilize", _fake_mobilize)
    monkeypatch.setattr("mobilize.transports.calle.CalleTransport", _FakeCalleTransport)

    events = _run_ws(simulate=False, confirm=True)
    assert events[-1]["data"]["mode"] == "live"
    assert events[-1]["data"]["learned_from_outcomes"] == 1

    after = dashboard_module.REGISTRY_STATE_PATH.read_bytes()
    assert after != before  # real outcome was persisted, as before this fix
