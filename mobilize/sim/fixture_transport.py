"""Deterministic fixture transport for E1: routes API-shaped CALL-E payloads
through the REAL production translation ladder (`transports.calle._to_call_result`)
instead of a reimplementation.

Why this exists: `SimulatedTransport` (transports/simulated.py) computes its own
outcome/commitment directly from `mobilize.sim.population.simulate_call` -- it never
touches `_to_call_result`, so nothing about the ordinary evaluation harness or the
CLI/dashboard's default "rehearsal" path exercises the real retraction/governance/
binding-mismatch branches in the actual production code that decides a call's
outcome from a CALL-E API response. That gap is exactly what this module closes:
each scenario below is a hand-authored, API-shaped `call` dict (the same shape
`CalleTransport.poll()` gets back from `GET /v1/calls/{id}`), and `poll()` here
hands it to `_to_call_result` unmodified -- the identical function a real call
result would go through.

No network is possible: there is no `httpx.AsyncClient`, no base URL, no API key.
`dispatch()` never leaves the process.

Determinism: `dispatch()` returns a caller-supplied or content-derived call_id (no
randomness), and `poll()` returns the fixed payload immediately (no simulated
latency, no clock dependency). `completed_at` on the returned `CallResult` still
comes from `_to_call_result` calling `utcnow()` internally (real wall-clock,
inherited from production code this module intentionally does not modify) -- callers
comparing traces across runs must normalize `completed_at`, `call_id`, and any
elapsed-time field rather than assume byte-identical output. See `normalize_trace`
below.
"""

from __future__ import annotations

import copy
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from mobilize.core.types import Candidate, CallResult, Need
from mobilize.transports.calle import _to_call_result

__all__ = [
    "FixtureTransport", "FixtureScenario", "SCENARIOS", "make_scenario_candidate",
    "normalize_trace", "normalize_call_result", "run_fixture_scenario",
]


@dataclass(frozen=True)
class FixtureScenario:
    """One named, reproducible scenario: a candidate plus the exact API-shaped
    `call` payload CALL-E would return for it. `expected_outcome` and
    `expected_decision_reason` are asserted by tests, not consumed by the
    transport -- they document, next to the fixture, what the real ladder is
    supposed to do with it."""

    name: str
    description: str
    candidate: Candidate
    call_payload: dict
    expected_outcome: str
    expected_decision_reason: str


def make_scenario_candidate(scenario_name: str, *, phone_suffix: str) -> Candidate:
    return Candidate(
        id=f"fixture_{scenario_name}",
        phone=f"+1555000{phone_suffix}",
        name=f"Fixture {scenario_name.replace('_', ' ').title()}",
        days_since_last_action=90,
        distance_km=5,
        historical_accept_rate=0.5,
        historical_showup_rate=0.7,
        timezone="America/New_York",
    )


def _call_payload(candidate: Candidate, *, status: str, task_completed: bool | None,
                   structured_result: dict, transcript_turns: list[dict]) -> dict:
    """Build a `call` dict in the exact shape `CalleTransport.poll()` parses:
    top-level status/metadata, one recipient with structured_result and
    attempts[].transcript_turns. Mirrors calle_contract_c0.md, not a made-up shape."""
    return {
        "id": f"fixture_call_{candidate.id}",
        "status": status,
        "task_completed": task_completed,
        "metadata": {"candidate_id": candidate.id},
        "recipients": [
            {
                "phones": [candidate.phone],
                "structured_result": structured_result,
                "attempts": [{"transcript_turns": transcript_turns}],
            }
        ],
    }


def _bot_turn(need_label: str = "Can you help with an urgent request right now?") -> dict:
    return {"speaker": "bot", "text": need_label}


def _build_success_scenario() -> FixtureScenario:
    candidate = make_scenario_candidate("success", phone_suffix="0001")
    payload = _call_payload(
        candidate,
        status="completed",
        task_completed=True,
        structured_result={
            "can_come": "yes",
            "final_position": "confirmed",
            "eta_minutes": "15",
            "evidence_summary": "Yes, I'll be there in about 15 minutes, definitely.",
            "wants_no_further_contact": "no",
        },
        transcript_turns=[
            _bot_turn(),
            {"speaker": "user", "text": "Yes, I'll be there in about 15 minutes, definitely."},
        ],
    )
    return FixtureScenario(
        name="success",
        description="Recipient clearly and firmly agrees; CALL-E's own task_completed "
                     "and final_position corroborate it, and the recipient's own words "
                     "affirm -- should land as a real, corroborated confirmation.",
        candidate=candidate,
        call_payload=payload,
        expected_outcome="firm_yes",
        expected_decision_reason="confirmed",
    )


def _build_refusal_scenario() -> FixtureScenario:
    candidate = make_scenario_candidate("refusal", phone_suffix="0002")
    payload = _call_payload(
        candidate,
        status="completed",
        task_completed=True,
        structured_result={
            "can_come": "no",
            "final_position": "declined_or_withdrawn",
            "eta_minutes": "unknown",
            "evidence_summary": "No, I can't help right now, sorry.",
            "wants_no_further_contact": "no",
        },
        transcript_turns=[
            _bot_turn(),
            {"speaker": "user", "text": "No, I can't help right now, sorry."},
        ],
    )
    return FixtureScenario(
        name="refusal",
        description="Recipient explicitly declines. Exercises the plain "
                     "RECIPIENT_DECLINED / can_come==\"no\" branch of _to_call_result.",
        candidate=candidate,
        call_payload=payload,
        expected_outcome="no",
        expected_decision_reason="recipient_declined",
    )


def _build_opt_out_scenario() -> FixtureScenario:
    candidate = make_scenario_candidate("opt_out", phone_suffix="0003")
    payload = _call_payload(
        candidate,
        status="completed",
        task_completed=True,
        structured_result={
            "can_come": "no",
            "final_position": "declined_or_withdrawn",
            "eta_minutes": "unknown",
            "evidence_summary": "Please stop calling me, take me off your list.",
            "wants_no_further_contact": "yes",
        },
        transcript_turns=[
            _bot_turn(),
            {"speaker": "user", "text": "Please stop calling me, take me off your list."},
        ],
    )
    return FixtureScenario(
        name="opt_out",
        description="Recipient declines AND explicitly asks never to be contacted "
                     "again -- exercises stop_requested / wants_no_further_contact, "
                     "which the dispatcher acts on as a permanent do-not-call entry. "
                     "Not reachable through the plain SimulatedTransport at all, which "
                     "has no wants_no_further_contact concept.",
        candidate=candidate,
        call_payload=payload,
        expected_outcome="no",
        expected_decision_reason="recipient_declined",
    )


def _build_ambiguity_scenario() -> FixtureScenario:
    """A retraction: the recipient says yes, then withdraws it later in the same
    call using phrasing with no negation word at all ("scratch that"). Only
    `final_position` (a genuine semantic judgment CALL-E itself makes across the
    whole call) can catch this open-ended class of reversal -- a fixed local
    pattern list cannot. can_come is left "yes" deliberately, to prove the real
    ladder does not trust can_come alone."""
    candidate = make_scenario_candidate("ambiguity", phone_suffix="0004")
    payload = _call_payload(
        candidate,
        status="completed",
        task_completed=True,
        structured_result={
            "can_come": "yes",
            "final_position": "declined_or_withdrawn",
            "eta_minutes": "unknown",
            "evidence_summary": "Yes, I can come. Actually, scratch that, I can't make it after all.",
            "wants_no_further_contact": "no",
        },
        transcript_turns=[
            _bot_turn(),
            {"speaker": "user", "text": "Yes, I can come."},
            {"speaker": "user", "text": "Actually, scratch that, I can't make it after all."},
        ],
    )
    return FixtureScenario(
        name="ambiguity",
        description="Provider-authored can_come==\"yes\" is stale/wrong: the "
                     "recipient retracted it later in the call ('scratch that'), "
                     "with no negation word CALL-E's local corroboration regex would "
                     "catch on its own. Exercises FINAL_POSITION_UNCLEAR -- the "
                     "branch that depends on final_position, CALL-E's own "
                     "whole-call judgment, rather than the local pattern list. This "
                     "is the retraction/governance-adjacent path the plain simulator "
                     "cannot generate at all, since it never calls _to_call_result.",
        candidate=candidate,
        call_payload=payload,
        expected_outcome="no_answer",
        expected_decision_reason="final_position_unclear",
    )


SCENARIOS: dict[str, FixtureScenario] = {
    s.name: s
    for s in (
        _build_success_scenario(),
        _build_refusal_scenario(),
        _build_opt_out_scenario(),
        _build_ambiguity_scenario(),
    )
}


class FixtureTransport:
    """Implements the same `Transport` protocol as `SimulatedTransport` and
    `CalleTransport`, but `poll()` returns a result computed by handing a
    fixed, API-shaped payload to the real `_to_call_result` -- not a
    reimplementation of its logic. One fixture transport instance serves one
    or more scenarios (keyed by candidate id), deterministically and with zero
    network access."""

    def __init__(self, scenarios: dict[str, FixtureScenario] | None = None):
        self._scenarios = scenarios if scenarios is not None else SCENARIOS
        self._by_candidate_id = {s.candidate.id: s for s in self._scenarios.values()}
        self.calls_placed = 0

    async def dispatch(self, candidate: Candidate, need_label: str, location: str, *, idempotency_key: str) -> str:
        if candidate.id not in self._by_candidate_id:
            raise KeyError(
                f"FixtureTransport has no scenario for candidate {candidate.id!r} -- "
                f"known scenario candidates: {sorted(self._by_candidate_id)}"
            )
        self.calls_placed += 1
        scenario = self._by_candidate_id[candidate.id]
        return scenario.call_payload["id"]

    async def poll(self, call_id: str, *, expected_candidate: Candidate | None = None) -> CallResult | None:
        for scenario in self._scenarios.values():
            if scenario.call_payload["id"] == call_id:
                # Real production translation, not a copy of its logic.
                candidate = expected_candidate or scenario.candidate
                return _to_call_result(call_id, copy.deepcopy(scenario.call_payload), candidate)
        return None

    async def aclose(self) -> None:
        return None


def normalize_call_result(result: CallResult) -> dict:
    """Strip non-reproducible fields (call_id, completed_at, started_at, any
    elapsed timing) from a CallResult for cross-run/cross-adapter comparison.
    Everything semantic -- outcome, commitment_score, decision_reason,
    contact_outcome, evidence, transcript, stated_yes, stop_requested -- is kept."""
    return {
        "candidate_id": result.candidate_id,
        "outcome": result.outcome.value if hasattr(result.outcome, "value") else result.outcome,
        "commitment_score": round(result.commitment_score, 6),
        "stated_yes": result.stated_yes,
        "stop_requested": result.stop_requested,
        "evidence": result.evidence,
        "transcript": result.transcript,
        "decision_reason": result.decision_reason,
        "contact_outcome": result.contact_outcome,
    }


async def run_fixture_scenario(scenario_name: str, *, mobilization_id: str | None = None) -> dict:
    """Shared, deterministic scenario runner used identically by the CLI
    (`--fixture-scenario`), the dashboard (`/api/fixture/{scenario}`), and the
    MCP tool (`mobilize_fixture`) -- so "the same scenario reproduces via
    CLI/dashboard/MCP" is one function, not three parallel reimplementations
    that could drift from each other.

    Runs the real `mobilize()` dispatcher (core/dispatcher.py, untouched) against
    a single-candidate pool and a `FixtureTransport` -- so the full dispatch/
    ledger/idempotency path runs for real, with the call outcome itself coming
    from the real `_to_call_result` translation via the fixture payload. No
    network call is possible: FixtureTransport never constructs an HTTP client.

    Fresh namespace per run: unless `mobilization_id` is given explicitly, a
    fresh uuid-derived one is used, and the ledger is a throwaway temp file
    (deleted after the run) -- so repeat invocations never collide with each
    other or with any operational ledger, and the CLI's existing --seed has no
    bearing here (this scenario is fully deterministic on its own, independent
    of the dashboard's own unseeded registry-backed simulator).
    """
    from mobilize.core.dispatcher import mobilize
    from mobilize.core.ledger import Ledger

    if scenario_name not in SCENARIOS:
        raise ValueError(f"Unknown fixture scenario {scenario_name!r}. Known: {sorted(SCENARIOS)}")
    scenario = SCENARIOS[scenario_name]

    run_id = mobilization_id or f"fixture_{scenario_name}_{uuid.uuid4().hex[:12]}"
    fd, ledger_name = tempfile.mkstemp(suffix=".jsonl", prefix=f"mobilize_fixture_{scenario_name}_")
    import os as _os
    _os.close(fd)
    ledger_path = Path(ledger_name)
    try:
        transport = FixtureTransport({scenario.name: scenario})
        need = Need(label=f"Fixture scenario: {scenario.description}", count=1,
                    deadline_minutes=5, location="Fixture", max_calls=1)
        events: list[tuple[str, dict]] = []

        def on_progress(event: str, data: dict) -> None:
            events.append((event, dict(data)))

        ledger = Ledger(str(ledger_path))
        result = await mobilize(need, [scenario.candidate], transport, ledger=ledger,
                                 on_progress=on_progress, mobilization_id=run_id,
                                 poll_interval_s=0.01, poll_timeout_s=5.0)
        await transport.aclose()

        return {
            "scenario": scenario.name,
            "description": scenario.description,
            "mobilization_id": run_id,
            "expected_outcome": scenario.expected_outcome,
            "expected_decision_reason": scenario.expected_decision_reason,
            "filled": result.filled,
            "calls_used": result.calls_used,
            "confirmed": [normalize_call_result(r) for r in result.confirmed],
            "all_results": [normalize_call_result(r) for r in result.all_results],
            "trace": normalize_trace(events),
        }
    finally:
        ledger_path.unlink(missing_ok=True)


def normalize_trace(events: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    """Normalize a list of (event, data) progress-callback pairs the same way:
    drop wave-timestamp-shaped and uuid-shaped fields, sort candidate lists,
    so the same fixture scenario dispatched through different adapters (CLI,
    dashboard, MCP) can be compared for semantic equality rather than exact
    text equality."""
    _VOLATILE_KEYS = {"call_id", "started_at", "completed_at", "time_to_fill_seconds",
                       "mobilization_id", "elapsed", "elapsed_s"}
    normalized = []
    for event, data in events:
        clean = {k: v for k, v in data.items() if k not in _VOLATILE_KEYS}
        if "candidates" in clean and isinstance(clean["candidates"], list):
            clean["candidates"] = sorted(clean["candidates"])
        normalized.append((event, clean))
    return normalized
