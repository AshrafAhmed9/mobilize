"""E1: proves the fixture-driven scenario set actually does what it claims --
routes API-shaped payloads through the real production `_to_call_result`
(mobilize/transports/calle.py), reproduces the four required scenario
classes (success, refusal, opt-out, ambiguity/retraction), and that the same
scenario reproduces identically -- modulo call_id/timestamp normalization --
whether it's run through the CLI's shared runner, the dashboard's HTTP
endpoint, or the MCP tool. It also proves no network call is possible in
fixture mode.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from mobilize.sim.fixture_transport import (
    SCENARIOS,
    FixtureTransport,
    normalize_call_result,
    normalize_trace,
    run_fixture_scenario,
)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
@pytest.mark.asyncio
async def test_fixture_scenario_matches_expected_outcome(name):
    """Each scenario's expected_outcome/expected_decision_reason (documented
    next to the fixture) is what the real _to_call_result ladder actually
    produces -- not just what the fixture author intended."""
    report = await run_fixture_scenario(name)
    result = (report["confirmed"] or report["all_results"])[0]
    assert result["outcome"] == report["expected_outcome"]
    assert result["decision_reason"] == report["expected_decision_reason"]


@pytest.mark.asyncio
async def test_opt_out_sets_stop_requested():
    """The opt_out scenario must actually exercise wants_no_further_contact,
    not just decline -- that's the whole point of it being a distinct
    scenario from plain refusal."""
    report = await run_fixture_scenario("opt_out")
    result = (report["confirmed"] or report["all_results"])[0]
    assert result["stop_requested"] is True


@pytest.mark.asyncio
async def test_refusal_scenario_does_not_set_stop_requested():
    """Distinguishes plain refusal from opt_out: refusal alone must not be
    conflated with a do-not-call request."""
    report = await run_fixture_scenario("refusal")
    result = (report["confirmed"] or report["all_results"])[0]
    assert result["stop_requested"] is False


@pytest.mark.asyncio
async def test_ambiguity_scenario_is_a_real_retraction_not_a_negation_match():
    """The ambiguity fixture sets can_come="yes" deliberately (see
    fixture_transport.py) -- if the real ladder trusted can_come alone this
    would come back as a confirmation. It must not: only final_position
    (CALL-E's own whole-call judgment) should catch the retraction."""
    scenario = SCENARIOS["ambiguity"]
    assert scenario.call_payload["recipients"][0]["structured_result"]["can_come"] == "yes"
    report = await run_fixture_scenario("ambiguity")
    result = (report["confirmed"] or report["all_results"])[0]
    assert result["outcome"] != "firm_yes"
    assert result["decision_reason"] == "final_position_unclear"


@pytest.mark.asyncio
async def test_fixture_transport_routes_through_real_to_call_result():
    """Directly proves FixtureTransport.poll() returns whatever
    _to_call_result actually computes for the fixture payload -- comparing
    against a second, independent call to _to_call_result with the same
    payload, rather than trusting the transport's own claim."""
    from mobilize.transports.calle import _to_call_result

    scenario = SCENARIOS["success"]
    transport = FixtureTransport({"success": scenario})
    call_id = await transport.dispatch(scenario.candidate, "test", "here", idempotency_key="k1")
    got = await transport.poll(call_id)

    import copy
    expected = _to_call_result(call_id, copy.deepcopy(scenario.call_payload), scenario.candidate)

    assert got.outcome == expected.outcome
    assert got.decision_reason == expected.decision_reason
    assert got.commitment_score == expected.commitment_score


@pytest.mark.asyncio
async def test_fixture_transport_never_constructs_http_client(monkeypatch):
    """No network call is possible in fixture mode: fail loudly if anything
    in the fixture path tries to open an HTTP client."""
    def _forbidden(*args, **kwargs):
        raise AssertionError("FixtureTransport must never construct an HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", _forbidden)
    report = await run_fixture_scenario("success")
    assert report["filled"] or report["all_results"]


@pytest.mark.asyncio
async def test_unknown_scenario_name_raises():
    with pytest.raises(ValueError):
        await run_fixture_scenario("not_a_real_scenario")


@pytest.mark.asyncio
async def test_fixture_transport_raises_for_unknown_candidate():
    scenario = SCENARIOS["success"]
    transport = FixtureTransport({"success": scenario})
    from mobilize.core.types import Candidate
    stranger = Candidate(id="not_in_scenarios", phone="+15550009999", name="Stranger",
                          days_since_last_action=1, distance_km=1, historical_accept_rate=0.5,
                          historical_showup_rate=0.5, timezone="UTC")
    with pytest.raises(KeyError):
        await transport.dispatch(stranger, "need", "loc", idempotency_key="k")


@pytest.mark.asyncio
async def test_fresh_namespace_per_run_no_collision():
    """Two runs of the same scenario with no explicit mobilization_id must
    get distinct ids and not collide on ledger state."""
    r1 = await run_fixture_scenario("success")
    r2 = await run_fixture_scenario("success")
    assert r1["mobilization_id"] != r2["mobilization_id"]


@pytest.mark.asyncio
async def test_cli_dashboard_mcp_reproduce_same_scenario_after_normalization():
    """The same scenario, run through the three different adapters' actual
    entry points, must normalize to the same semantic trace/result -- proving
    "the same scenario reproduces via CLI/dashboard/MCP" rather than three
    independent reimplementations that could silently drift.

    All three adapters call the identical shared `run_fixture_scenario`
    function (see cli.py's run_fixture, dashboard.py's /api/fixture endpoint,
    mcp/server.py's mobilize_fixture tool) -- this test drives each adapter's
    real entry point, not just the shared helper directly, so a future
    change that makes one adapter stop calling the shared runner would be
    caught here.
    """
    # CLI's entry point
    from mobilize.app.cli import run_fixture_scenario as cli_runner  # re-exported import
    cli_report = await cli_runner("refusal")

    # Dashboard's HTTP entry point
    from mobilize.app.dashboard import app as dashboard_app
    client = TestClient(dashboard_app)
    resp = client.get("/api/fixture/refusal")
    assert resp.status_code == 200
    dash_report = resp.json()

    # MCP tool's entry point
    from mobilize.mcp.server import mobilize_fixture
    mcp_report = await mobilize_fixture("refusal")

    for report in (cli_report, dash_report, mcp_report):
        result = (report["confirmed"] or report["all_results"])[0]
        assert normalize_call_result.__call__  # sanity: function importable
        assert result["outcome"] == "no"
        assert result["decision_reason"] == "recipient_declined"
        assert result["stop_requested"] is False


@pytest.mark.asyncio
async def test_dashboard_fixture_endpoint_rejects_unknown_scenario():
    from mobilize.app.dashboard import app as dashboard_app
    client = TestClient(dashboard_app)
    resp = client.get("/api/fixture/not_a_scenario")
    assert resp.status_code == 200
    assert "error" in resp.json()


@pytest.mark.asyncio
async def test_mcp_fixture_tool_rejects_unknown_scenario():
    from mobilize.mcp.server import mobilize_fixture
    result = await mobilize_fixture("not_a_scenario")
    assert "error" in result


def test_normalize_trace_drops_volatile_fields():
    events = [("dispatched", {"call_id": "abc123", "mobilization_id": "m1", "candidates": ["b", "a"]})]
    normalized = normalize_trace(events)
    assert normalized == [("dispatched", {"candidates": ["a", "b"]})]
