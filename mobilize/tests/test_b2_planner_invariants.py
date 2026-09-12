"""B2 invariant review for mobilize/core/planner.py.

This file does NOT change planner.py's behavior. B1's matched-policy
experiment (mobilize/artifacts/matched_policy_experiment_b1.md) found no
policy dominates on every axis across need_count in {2, 5, 10}, and never
exercised need_count == 1 at all. The plan's own instructions permit
retaining the current heuristic and publishing why, rather than forcing a
new "improved" policy into production without evidence covering the case
it would target. See mobilize/artifacts/b2_dispatch_policy_analysis.md for
the full writeup and decision rationale.

These tests exist to pin down, by hand-worked exact examples and explicit
monotonicity checks, the invariants the CURRENT planner must never violate
-- so a future change to planner.py (or a future attempt to add a
probabilistic variant) has a concrete regression net to run against.
"""

from mobilize.core.types import Candidate
from mobilize.core.planner import plan_wave, rank_candidates


def make_candidate(id_, accept=0.5, showup=0.5, eligible=True, distance=5.0, days=90) -> Candidate:
    return Candidate(
        id=id_,
        phone="+15550000000",
        name=id_,
        days_since_last_action=days,
        distance_km=distance,
        historical_accept_rate=accept,
        historical_showup_rate=showup,
        eligible=eligible,
    )


# ---------------------------------------------------------------------------
# Invariant 1: plan_wave always chooses a PREFIX of rank_candidates(pool).
# It never skips a higher-ranked candidate to pick a lower-ranked one.
# ---------------------------------------------------------------------------

def test_invariant_chosen_is_a_prefix_of_ranked_order():
    pool = [make_candidate(f"c{i}", accept=0.9 - i * 0.05, showup=0.9 - i * 0.05) for i in range(10)]
    ranked_ids = [c.id for c in rank_candidates(pool)]
    plan = plan_wave(pool, need_count=3, safety_margin=1.0)
    chosen_ids = [c.id for c in plan.candidates]
    assert chosen_ids == ranked_ids[: len(chosen_ids)]


# ---------------------------------------------------------------------------
# Invariant 2 (hand-worked, exact): target unreachable -> full pool returned,
# expected_confirmations reported HONESTLY below target, not padded or
# silently truncated. No false-confidence result.
# ---------------------------------------------------------------------------

def test_hand_worked_exact_target_unreachable_returns_full_pool_short_of_target():
    # Three candidates, priors chosen so prior_score() is exactly known:
    # prior_score = 0.4*accept + 0.4*showup + 0.1*recency + 0.1*distance,
    # so pin accept==showup and hold recency/distance identical across
    # candidates to make prior_score == accept (== showup) plus a constant
    # recency/distance term shared by all three -- ranking is then exactly
    # by accept/showup and the relative cumulative sums are exact.
    a = make_candidate("a", accept=0.5, showup=0.5)
    b = make_candidate("b", accept=0.4, showup=0.4)
    c = make_candidate("c", accept=0.3, showup=0.3)
    pool = [c, a, b]  # deliberately unordered input

    plan = plan_wave(pool, need_count=2, safety_margin=1.0)  # target = 2.0

    # Ranked order by prior_score descending: a, b, c (each candidate's
    # prior_score differs only in the accept/showup term since recency and
    # distance are identical across all three).
    assert [x.id for x in plan.candidates] == ["a", "b", "c"]
    # Sum of the three candidates' prior_score is well below target=2.0
    # (each prior_score is at most ~0.5*0.8 + 0.1 + 0.1 < 0.6), so the pool
    # is exhausted without reaching target -- and the plan says so honestly
    # via expected_confirmations, rather than claiming the target was met.
    assert plan.expected_confirmations < 2.0
    assert len(plan.candidates) == 3  # entire pool consumed, no more to add


def test_hand_worked_exact_target_reached_stops_at_smallest_sufficient_prefix():
    # Two strong candidates whose combined prior_score comfortably clears a
    # low target after just two picks, verified by hand: prior_score() for
    # accept=showup=0.9 candidates (identical recency/distance) is the same
    # constant for both, call it s. Target = 1.3. s > 0.65 for these priors
    # (0.4*0.9+0.4*0.9 = 0.72 alone, before the +0.1 recency/distance terms),
    # so 2*s > 1.3 and 1*s < 1.3 (since s < 1.0) -- the smallest sufficient
    # prefix is exactly 2, never 1 and never all of the pool.
    pool = [make_candidate(f"c{i}", accept=0.9, showup=0.9) for i in range(5)]
    plan = plan_wave(pool, need_count=1, safety_margin=1.3)
    assert len(plan.candidates) == 2
    assert plan.expected_confirmations >= 1.3


# ---------------------------------------------------------------------------
# Invariant 3: more need_count (holding pool and margin fixed) never
# DECREASES the wave size. Filling a bigger need should never call fewer
# people than filling a smaller one under the same policy.
# ---------------------------------------------------------------------------

def test_monotonic_wave_size_nondecreasing_in_need_count():
    pool = [make_candidate(f"c{i}", accept=0.4, showup=0.4, distance=float(i)) for i in range(30)]
    sizes = [len(plan_wave(pool, need_count=n, safety_margin=1.3).candidates) for n in range(1, 10)]
    assert all(sizes[i] <= sizes[i + 1] for i in range(len(sizes) - 1))


# ---------------------------------------------------------------------------
# Invariant 4: more safety_margin (holding need_count and pool fixed) never
# DECREASES the wave size -- asking for a bigger cushion should never call
# fewer people.
# ---------------------------------------------------------------------------

def test_monotonic_wave_size_nondecreasing_in_safety_margin():
    pool = [make_candidate(f"c{i}", accept=0.4, showup=0.4, distance=float(i)) for i in range(30)]
    margins = [1.0, 1.1, 1.3, 1.5, 2.0]
    sizes = [len(plan_wave(pool, need_count=5, safety_margin=m).candidates) for m in margins]
    assert all(sizes[i] <= sizes[i + 1] for i in range(len(sizes) - 1))


# ---------------------------------------------------------------------------
# Invariant 5: stronger priors across the WHOLE pool never require MORE
# calls to reach the same target than weaker priors would (better
# candidates should never force a bigger wave).
# ---------------------------------------------------------------------------

def test_monotonic_wave_size_nonincreasing_in_candidate_quality():
    weak_pool = [make_candidate(f"c{i}", accept=0.2, showup=0.2, distance=float(i)) for i in range(30)]
    strong_pool = [make_candidate(f"c{i}", accept=0.8, showup=0.8, distance=float(i)) for i in range(30)]
    weak_size = len(plan_wave(weak_pool, need_count=5, safety_margin=1.3).candidates)
    strong_size = len(plan_wave(strong_pool, need_count=5, safety_margin=1.3).candidates)
    assert strong_size <= weak_size


# ---------------------------------------------------------------------------
# Invariant 6: max_wave_size is a hard cap that always wins, even when the
# target would otherwise not be reached -- the planner never silently
# exceeds a declared budget to chase the safety margin.
# ---------------------------------------------------------------------------

def test_max_wave_size_hard_cap_wins_over_target():
    pool = [make_candidate(f"c{i}", accept=0.05, showup=0.05, distance=float(i)) for i in range(100)]
    plan = plan_wave(pool, need_count=50, safety_margin=1.3, max_wave_size=7)
    assert len(plan.candidates) == 7
    # target is nowhere near met (priors are deliberately terrible) --
    # confirms the cap, not the target, decided the stopping point.
    assert plan.expected_confirmations < 50 * 1.3


# ---------------------------------------------------------------------------
# Invariant 7: ineligible candidates are never chosen, regardless of prior.
# ---------------------------------------------------------------------------

def test_ineligible_never_chosen_even_with_best_priors():
    ineligible_best = make_candidate("best", accept=0.99, showup=0.99, eligible=False)
    eligible_rest = [make_candidate(f"c{i}", accept=0.1, showup=0.1) for i in range(5)]
    plan = plan_wave([ineligible_best, *eligible_rest], need_count=1, safety_margin=1.0)
    assert all(c.id != "best" for c in plan.candidates)


# ---------------------------------------------------------------------------
# Invariant 8: empty pool always produces an empty, zero-expectation plan
# -- never an error, never a fabricated nonzero expectation.
# ---------------------------------------------------------------------------

def test_empty_pool_is_empty_plan_not_an_error():
    plan = plan_wave([], need_count=3)
    assert plan.candidates == []
    assert plan.expected_confirmations == 0.0
