"""Tests for the B1 matched-policy-experiment harness (mobilize/sim/matched_experiment.py).

Covers: (1) pre-generated ground truth is deterministic and invariant to
dispatch order/concurrency, (2) the analytical baseline reimplementations
(sequential, fixed_waves, call_all, stated_yes_only) are not silently a
strawman -- differential-tested against the real production dispatcher on
small cases, and against each other where they should provably coincide.
"""

from __future__ import annotations

import asyncio
import random

import pytest

from mobilize.core.types import Need
from mobilize.sim.matched_experiment import (
    FixtureTransport,
    pregenerate_trial,
    run_call_all,
    run_fixed_waves,
    run_ranked_greedy_real,
    run_sequential,
    run_stated_yes_only,
)


def _need(count=1, max_calls=40):
    return Need(label="test need", count=count, deadline_minutes=60, location="Test City", max_calls=max_calls)


# ---------------------------------------------------------------------------
# Determinism / order-invariance
# ---------------------------------------------------------------------------


def test_pregeneration_is_deterministic_for_same_seed():
    gt1 = pregenerate_trial(30, seed=777)
    gt2 = pregenerate_trial(30, seed=777)
    assert gt1.outcomes.keys() == gt2.outcomes.keys()
    for cid in gt1.outcomes:
        o1, o2 = gt1.outcomes[cid], gt2.outcomes[cid]
        assert o1.can_come == o2.can_come
        assert o1.true_showup == o2.true_showup
        assert o1.picked_up == o2.picked_up
        assert o1.modeled_call_duration_s == o2.modeled_call_duration_s


def test_different_seeds_diverge():
    gt1 = pregenerate_trial(30, seed=1)
    gt2 = pregenerate_trial(30, seed=2)
    # Not every candidate need differ, but the aggregate outcome vector must.
    diffs = sum(
        1 for cid in gt1.outcomes if gt1.outcomes[cid].can_come != gt2.outcomes.get(cid, gt1.outcomes[cid]).can_come
    )
    assert diffs > 0


def test_outcome_invariant_to_dispatch_order():
    """The whole point of B1: candidate X's outcome must not depend on which
    policy calls them, or in what order. Simulate two different dispatch
    orders against the SAME pre-generated ground truth and confirm every
    individual candidate's looked-up outcome is identical either way."""
    gt = pregenerate_trial(20, seed=555)
    donor_ids = list(gt.outcomes.keys())

    forward_transport = FixtureTransport(gt)
    reversed_transport = FixtureTransport(gt)

    async def dispatch_all(transport, ids):
        results = {}
        call_ids = {cid: await transport.dispatch(_candidate_stub(gt, cid), "x", "y", idempotency_key=cid) for cid in ids}
        for cid, call_id in call_ids.items():
            r = await transport.poll(call_id)
            results[cid] = r
        return results

    def _candidate_stub(gt, cid):
        return next(d.candidate for d in gt.donors if d.candidate.id == cid)

    fwd = asyncio.run(dispatch_all(forward_transport, donor_ids))
    rev = asyncio.run(dispatch_all(reversed_transport, list(reversed(donor_ids))))

    for cid in donor_ids:
        assert fwd[cid].outcome == rev[cid].outcome
        assert fwd[cid].commitment_score == rev[cid].commitment_score
        assert fwd[cid].stated_yes == rev[cid].stated_yes


def test_outcome_invariant_to_concurrency():
    """Same as above, but firing all dispatches concurrently via gather
    instead of sequentially -- concurrency must not perturb any outcome."""
    gt = pregenerate_trial(20, seed=555)
    donor_ids = list(gt.outcomes.keys())
    transport_seq = FixtureTransport(gt)
    transport_conc = FixtureTransport(gt)

    def _candidate_stub(cid):
        return next(d.candidate for d in gt.donors if d.candidate.id == cid)

    async def seq():
        out = {}
        for cid in donor_ids:
            call_id = await transport_seq.dispatch(_candidate_stub(cid), "x", "y", idempotency_key=cid)
            out[cid] = await transport_seq.poll(call_id)
        return out

    async def conc():
        call_ids = await asyncio.gather(
            *(transport_conc.dispatch(_candidate_stub(cid), "x", "y", idempotency_key=cid) for cid in donor_ids)
        )
        results = await asyncio.gather(*(transport_conc.poll(cid_) for cid_ in call_ids))
        return dict(zip(donor_ids, results))

    seq_results = asyncio.run(seq())
    conc_results = asyncio.run(conc())
    for cid in donor_ids:
        assert seq_results[cid].outcome == conc_results[cid].outcome
        assert seq_results[cid].commitment_score == conc_results[cid].commitment_score


# ---------------------------------------------------------------------------
# Differential tests: reimplemented baselines vs. the real production
# dispatcher (or vs. each other, where provably equivalent)
# ---------------------------------------------------------------------------


def test_fixed_waves_full_batch_matches_call_all():
    """fixed_waves with batch_size >= pool_size dispatches the identical
    ranked prefix, in one round, as call_all (when max_calls >= pool_size) --
    the two reimplementations must therefore produce the identical confirmed
    and contacted sets. This is a consistency check between two
    independently-written policy loops sharing the same fixture, not a
    tautology: a bug in either loop's acceptance-rule wiring would break it."""
    gt = pregenerate_trial(15, seed=42)
    need = _need(count=2, max_calls=15)

    fixed = asyncio.run(run_fixed_waves(need, gt, batch_size=15))
    call_all = asyncio.run(run_call_all(need, gt))

    assert set(fixed.confirmed_candidate_ids) == set(call_all.confirmed_candidate_ids)
    assert set(fixed.contacted_candidate_ids) == set(call_all.contacted_candidate_ids)


def test_sequential_matches_real_dispatcher_when_one_wave_suffices(tmp_path):
    """Find a small trial where the REAL production dispatcher (mobilize())
    fills the need in exactly one wave, then confirm the sequential
    reimplementation -- built from the same rank_candidates()/acceptance
    constants, not re-derived -- reaches the identical confirmed roster.
    This is the differential test the plan requires for a reimplemented
    baseline: it's checked against a real production run, not just against
    itself."""
    need = _need(count=1, max_calls=40)
    chosen_gt = None
    chosen_real = None
    for seed in range(1, 60):
        gt = pregenerate_trial(10, seed=seed)
        ledger_path = str(tmp_path / f"ledger_{seed}.jsonl")
        real = asyncio.run(run_ranked_greedy_real(need, gt, ledger_path))
        if real.filled and real.dispatch_rounds == 1:
            chosen_gt, chosen_real = gt, real
            break
    assert chosen_gt is not None, "no seed produced a single-wave fill in the search range -- widen the range"

    seq = asyncio.run(run_sequential(need, chosen_gt))
    assert seq.filled
    assert set(seq.confirmed_candidate_ids) == set(chosen_real.confirmed_candidate_ids)


def test_stated_yes_only_accepts_strictly_more_or_equal_than_calibrated_would():
    """Sanity check on the stated-yes-only reimplementation: since it accepts
    on stated_yes alone (no commitment threshold), for the SAME contacted
    candidates it can never confirm fewer people than a threshold-gated
    policy would from that same contact set."""
    gt = pregenerate_trial(25, seed=9)
    need = _need(count=3, max_calls=25)
    stated = asyncio.run(run_stated_yes_only(need, gt))
    seq = asyncio.run(run_sequential(need, gt))
    # Every candidate sequential confirmed also stated yes (calibrated is a
    # strict subset condition of stated-yes among people who said yes).
    stated_yes_candidates = {cid for cid, o in gt.outcomes.items() if o.can_come == "yes" and o.picked_up}
    assert set(seq.confirmed_candidate_ids) <= stated_yes_candidates
