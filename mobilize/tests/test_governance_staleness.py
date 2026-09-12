"""A3: governance (do-not-call, cooldown, fatigue, calling-hours) must be
checked close to actual dispatch time for EVERY wave, not only once when the
roster is first loaded. A mobilize() run can span multiple waves separated
by real poll-wait time; governance state (e.g. an opt-out delivered through
a different channel) can change in that window, and a later wave must not
dispatch to someone who has since become non-callable.

Before this fix, `filter_callable` ran exactly once, before the first wave,
against the pool built from the original roster. Nothing re-checked
`is_callable` per-candidate at the point each wave actually dispatches, so a
candidate who became do-not-call between wave 1 and wave 2 was still dialed
in wave 2.
"""

from __future__ import annotations

import pytest

from mobilize.core.dispatcher import mobilize
from mobilize.core.ledger import Ledger
from mobilize.core.policy import GovernancePolicy, GovernanceState
from mobilize.core.types import CallOutcome, CallResult, Need, utcnow
from mobilize.tests.test_planner import make_candidate


class MidRunGovernanceChangeTransport:
    """c1 and c2 (wave 1, both needed to hit the safety-margin target) both
    come back CallOutcome.NO. While handling c2's result -- i.e. strictly
    between wave 1 finishing and wave 2 being planned -- c3 is added to the
    do-not-call list, simulating an opt-out delivered through some other
    channel (e.g. a text) while wave 1 was still in flight. Wave 2 must not
    dispatch to c3."""

    def __init__(self, state: GovernanceState) -> None:
        self.state = state
        self.dispatched_ids: list[str] = []

    async def dispatch(self, candidate, need_label, location, *, idempotency_key):
        self.dispatched_ids.append(candidate.id)
        return f"call_{candidate.id}"

    async def poll(self, call_id, *, expected_candidate=None):
        candidate_id = expected_candidate.id
        if candidate_id == "c2":
            self.state.do_not_call.add("c3")
        return CallResult(
            call_id=call_id,
            candidate_id=candidate_id,
            outcome=CallOutcome.NO,
            commitment_score=0.0,
            stated_yes=False,
            evidence="not available",
            transcript=[],
            completed_at=utcnow(),
        )


@pytest.mark.asyncio
async def test_governance_rechecked_close_to_each_waves_dispatch(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    state = GovernanceState()
    # emergency_override sidesteps calling-hours so the test only exercises
    # do-not-call staleness, deterministically.
    policy = GovernancePolicy(emergency_override=True)
    transport = MidRunGovernanceChangeTransport(state)

    pool = [
        make_candidate("c1", accept=0.9, showup=0.9),
        make_candidate("c2", accept=0.9, showup=0.9),
        make_candidate("c3", accept=0.9, showup=0.9),
    ]
    # need_count=1, safety_margin default 1.3: c1+c2's priors alone clear the
    # target, so wave 1 is exactly [c1, c2], leaving c3 for a second wave.
    need = Need(label="x", count=1, deadline_minutes=60, location="loc", max_calls=3)

    await mobilize(
        need, pool, transport, ledger=ledger, mobilization_id="mob_stale_gov",
        governance_state=state, governance_policy=policy,
        poll_timeout_s=1.0,
    )

    assert transport.dispatched_ids[:2] == ["c1", "c2"] or set(transport.dispatched_ids[:2]) == {"c1", "c2"}
    assert "c3" not in transport.dispatched_ids, (
        "c3 was added to do-not-call between wave 1 and wave 2, but was still "
        "dispatched -- governance was only checked once, at roster load, not "
        "close to actual dispatch time."
    )
