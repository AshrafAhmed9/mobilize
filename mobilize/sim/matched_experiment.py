"""B1: matched policy experiment on shared, pre-generated ground truth.

Unlike `mobilize/sim/harness.py` (kept untouched as the historical baseline
per the B0 audit), every policy compared here scores against the SAME
per-candidate outcomes, pre-generated once per trial before any policy runs.
Swapping which policy "sees" a candidate cannot change what that candidate
would have said or done -- the outcome is a fixed fact of the trial, not a
draw from a shared stream that call order or concurrency could perturb.

Three independent RNG streams per trial:
  - population stream: `generate_population(seed=seed)` (unchanged from the
    old harness -- donor priors/eligibility).
  - outcome stream: one `random.Random` per candidate, keyed off
    `(seed, candidate.id, "outcome")`, feeding the existing
    `mobilize.sim.population.simulate_call`. Because each candidate's stream
    is keyed by identity rather than drawn sequentially from one shared
    generator, the outcome for candidate X is identical no matter which
    policy calls them, what order they're called in, or how many OTHER
    candidates get called first.
  - duration stream: a second per-candidate `random.Random` keyed off
    `(seed, candidate.id, "duration")`, producing a modeled call-handling
    duration independent of the outcome draw (explicitly labeled "modeled"
    in every report -- never real measured call timing).

Policies only ever see what `FixtureTransport.poll()` returns -- a
`CallResult` shaped exactly like the real transport's. The ground-truth
`_true_showup` / `_picked_up` flags live only in `TrialGroundTruth`, used by
the evaluator after the fact, never exposed to a policy's decision logic.
"""

from __future__ import annotations

import hashlib
import random
import statistics
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from mobilize.core.commitment import calibrated_commitment
from mobilize.core.dispatcher import CONFIRMED_OUTCOMES, COMMITMENT_THRESHOLD, mobilize
from mobilize.core.ledger import Ledger
from mobilize.core.planner import rank_candidates
from mobilize.core.types import Candidate, CallOutcome, CallResult, Need, utcnow
from mobilize.sim.population import SyntheticDonor, generate_population, simulate_call

# ---------------------------------------------------------------------------
# Pre-generation
# ---------------------------------------------------------------------------


def _keyed_rng(seed: int, candidate_id: str, salt: str) -> random.Random:
    """Deterministic per-candidate RNG, independent of dispatch order.

    Seeded from a hash of (seed, candidate_id, salt) rather than drawn from
    a shared sequential stream, so the result for one candidate cannot be
    perturbed by how many other candidates were drawn from first.
    """
    key = f"{seed}:{candidate_id}:{salt}".encode()
    digest = hashlib.sha256(key).hexdigest()
    return random.Random(int(digest[:16], 16))


@dataclass(frozen=True)
class GroundTruthOutcome:
    """Everything the evaluator knows about one candidate's hypothetical
    call, fixed once per trial. Only `visible()` fields ever reach a policy."""

    candidate_id: str
    can_come: str  # "yes" | "no" | "unknown"
    eta_minutes: str
    evidence_summary: str
    picked_up: bool
    true_showup: bool  # HIDDEN ground truth: would they actually arrive
    modeled_call_duration_s: float  # HIDDEN-ish: independent duration draw, reported only in aggregate

    def visible(self) -> dict:
        """Fields a real transport result would legitimately expose."""
        return {
            "can_come": self.can_come,
            "eta_minutes": self.eta_minutes,
            "evidence_summary": self.evidence_summary,
            "_picked_up": self.picked_up,
        }


@dataclass(frozen=True)
class TrialGroundTruth:
    seed: int
    donors: list[SyntheticDonor]
    outcomes: dict[str, GroundTruthOutcome]  # candidate_id -> outcome


def pregenerate_trial(pool_size: int, seed: int) -> TrialGroundTruth:
    donors = generate_population(pool_size, seed=seed)
    outcomes: dict[str, GroundTruthOutcome] = {}
    for donor in donors:
        cid = donor.candidate.id
        outcome_rng = _keyed_rng(seed, cid, "outcome")
        duration_rng = _keyed_rng(seed, cid, "duration")
        raw = simulate_call(donor, outcome_rng)
        # Modeled call-handling duration: dial + conversation time, drawn from
        # an independent stream. Not a measured real timing -- purely a
        # planning assumption fed into "modeled time to sufficient
        # confirmations" below.
        duration_s = duration_rng.uniform(15.0, 90.0)
        outcomes[cid] = GroundTruthOutcome(
            candidate_id=cid,
            can_come=raw["can_come"],
            eta_minutes=raw["eta_minutes"],
            evidence_summary=raw["evidence_summary"],
            picked_up=raw["_picked_up"],
            true_showup=raw["_true_showup"],
            modeled_call_duration_s=duration_s,
        )
    return TrialGroundTruth(seed=seed, donors=donors, outcomes=outcomes)


# ---------------------------------------------------------------------------
# Fixture transport: same Transport interface, reads pre-generated outcomes
# ---------------------------------------------------------------------------


class FixtureTransport:
    """Implements the same interface as SimulatedTransport, but every
    candidate's outcome comes from a pre-generated, order-invariant
    dictionary instead of a shared sequential RNG stream. Safe for
    concurrent dispatch: each candidate's outcome is looked up, not drawn."""

    def __init__(self, ground_truth: TrialGroundTruth, *, poll_latency_s: float = 0.0):
        self._by_candidate = {d.candidate.id: d for d in ground_truth.donors}
        self._outcomes = ground_truth.outcomes
        self._poll_latency_s = poll_latency_s
        self._pending: dict[str, tuple[str, datetime]] = {}  # call_id -> (candidate_id, dispatched_at)
        self.calls_placed = 0
        self.dispatched_candidate_ids: list[str] = []

    async def dispatch(self, candidate: Candidate, need_label: str, location: str, *, idempotency_key: str) -> str:
        call_id = f"fx_{candidate.id}_{self.calls_placed}"
        self._pending[call_id] = (candidate.id, utcnow())
        self.calls_placed += 1
        self.dispatched_candidate_ids.append(candidate.id)
        return call_id

    async def poll(self, call_id: str, *, expected_candidate: Candidate | None = None) -> CallResult | None:
        entry = self._pending.get(call_id)
        if entry is None:
            return None
        candidate_id, dispatched_at = entry
        elapsed = (utcnow() - dispatched_at).total_seconds()
        if elapsed < self._poll_latency_s:
            return None

        gt = self._outcomes[candidate_id]
        donor = self._by_candidate[candidate_id]
        can_come = gt.can_come
        evidence = gt.evidence_summary
        picked_up = gt.picked_up

        if not picked_up:
            outcome, commitment = CallOutcome.NO_ANSWER, 0.0
        elif can_come == "no":
            outcome = CallOutcome.INELIGIBLE if not donor.candidate.eligible else CallOutcome.NO
            commitment = 0.0
        else:
            commitment = calibrated_commitment(
                evidence=evidence,
                candidate_prior_showup_rate=donor.candidate.historical_showup_rate,
            )
            outcome = CallOutcome.FIRM_YES if commitment >= 0.6 else CallOutcome.SOFT_YES

        return CallResult(
            call_id=call_id,
            candidate_id=candidate_id,
            outcome=outcome,
            commitment_score=commitment,
            stated_yes=(can_come == "yes"),
            evidence=evidence,
            transcript=[{"speaker": "bot", "text": "Can you help?"}, {"speaker": "user", "text": evidence}],
            completed_at=utcnow(),
            raw={"candidate_id": candidate_id},  # deliberately excludes true_showup: not visible to policies
        )


# ---------------------------------------------------------------------------
# Per-trial result record
# ---------------------------------------------------------------------------


@dataclass
class PolicyRunResult:
    policy: str
    trial_seed: int
    pool_size: int
    need_count: int
    calls_used: int
    accepted_by_provider: int
    contact_attempts: int
    ambiguous_count: int
    dispatch_rounds: int
    confirmed_candidate_ids: list[str]  # policy's believed-confirmed roster
    contacted_candidate_ids: list[str]  # everyone actually contacted (incl. rejected soft-yeses)
    filled: bool
    modeled_time_to_fill_s: float | None  # sum of modeled_call_duration_s along the critical dispatch path


def _true_showup(gt: TrialGroundTruth, candidate_id: str) -> bool:
    return gt.outcomes[candidate_id].true_showup


def _modeled_time_for(gt: TrialGroundTruth, candidate_ids: list[str]) -> float:
    """Sum of modeled per-call durations for the candidates actually dispatched,
    as a rough proxy for "time to run through this many contact attempts."
    Explicitly modeled/assumption-based -- not measured wall-clock."""
    return sum(gt.outcomes[cid].modeled_call_duration_s for cid in candidate_ids if cid in gt.outcomes)


# ---------------------------------------------------------------------------
# Policy 1: real production dispatcher, through FixtureTransport
# ---------------------------------------------------------------------------


async def run_ranked_greedy_real(need: Need, gt: TrialGroundTruth, ledger_path: str) -> PolicyRunResult:
    transport = FixtureTransport(gt)
    pool = [d.candidate for d in gt.donors]
    ledger = Ledger(ledger_path)
    result = await mobilize(need, pool, transport, ledger=ledger, mobilization_id=f"b1_{gt.seed}")

    confirmed_ids = [r.candidate_id for r in result.confirmed]
    contacted_ids = [r.candidate_id for r in result.all_results]
    return PolicyRunResult(
        policy="ranked_greedy_real",
        trial_seed=gt.seed,
        pool_size=len(pool),
        need_count=need.count,
        calls_used=result.calls_used,
        accepted_by_provider=result.counts.get("accepted_by_provider", len(contacted_ids)),
        contact_attempts=result.calls_used,
        ambiguous_count=len(result.ambiguous_candidate_ids),
        dispatch_rounds=len(result.waves),
        confirmed_candidate_ids=confirmed_ids,
        contacted_candidate_ids=contacted_ids,
        filled=result.filled,
        modeled_time_to_fill_s=_modeled_time_for(gt, contacted_ids) if contacted_ids else None,
    )


# ---------------------------------------------------------------------------
# Reimplemented analytical baselines (2, 3, 5) -- share ranking/acceptance
# rule with the real dispatcher via imported constants/functions, never a
# re-derived threshold or ranking formula.
# ---------------------------------------------------------------------------


async def _dispatch_and_resolve(transport: FixtureTransport, candidates: list[Candidate], need: Need) -> list[CallResult]:
    call_ids = {}
    for c in candidates:
        call_id = await transport.dispatch(c, need.label, need.location, idempotency_key=f"k_{c.id}")
        call_ids[c.id] = call_id
    results = []
    for cid, call_id in call_ids.items():
        r = None
        for _ in range(50):
            r = await transport.poll(call_id)
            if r is not None:
                break
        if r is not None:
            results.append(r)
    return results


async def run_sequential(need: Need, gt: TrialGroundTruth) -> PolicyRunResult:
    """One candidate at a time, same ranking (`rank_candidates`) and
    acceptance rule (`CONFIRMED_OUTCOMES` + `COMMITMENT_THRESHOLD`) as the
    real dispatcher. Differs from ranked-greedy-real only in wave size (1
    instead of a batch)."""
    transport = FixtureTransport(gt)
    ranked = rank_candidates([d.candidate for d in gt.donors])
    confirmed: list[str] = []
    contacted: list[str] = []
    rounds = 0
    for candidate in ranked:
        if len(confirmed) >= need.count or transport.calls_placed >= need.max_calls:
            break
        results = await _dispatch_and_resolve(transport, [candidate], need)
        rounds += 1
        for r in results:
            contacted.append(r.candidate_id)
            if r.outcome in CONFIRMED_OUTCOMES and r.commitment_score >= COMMITMENT_THRESHOLD:
                confirmed.append(r.candidate_id)

    return PolicyRunResult(
        policy="sequential",
        trial_seed=gt.seed,
        pool_size=len(gt.donors),
        need_count=need.count,
        calls_used=transport.calls_placed,
        accepted_by_provider=transport.calls_placed,
        contact_attempts=transport.calls_placed,
        ambiguous_count=0,
        dispatch_rounds=rounds,
        confirmed_candidate_ids=confirmed,
        contacted_candidate_ids=contacted,
        filled=len(confirmed) >= need.count,
        modeled_time_to_fill_s=_modeled_time_for(gt, contacted) if contacted else None,
    )


async def run_fixed_waves(need: Need, gt: TrialGroundTruth, *, batch_size: int) -> PolicyRunResult:
    """Predeclared, fixed-size batches (not adaptively sized like
    `plan_wave`'s expected-value target), same ranking and acceptance rule."""
    transport = FixtureTransport(gt)
    ranked = rank_candidates([d.candidate for d in gt.donors])
    confirmed: list[str] = []
    contacted: list[str] = []
    rounds = 0
    i = 0
    while i < len(ranked) and len(confirmed) < need.count and transport.calls_placed < need.max_calls:
        budget_left = need.max_calls - transport.calls_placed
        batch = ranked[i : i + min(batch_size, budget_left)]
        if not batch:
            break
        results = await _dispatch_and_resolve(transport, batch, need)
        rounds += 1
        i += len(batch)
        for r in results:
            contacted.append(r.candidate_id)
            if r.outcome in CONFIRMED_OUTCOMES and r.commitment_score >= COMMITMENT_THRESHOLD:
                confirmed.append(r.candidate_id)

    return PolicyRunResult(
        policy=f"fixed_waves_{batch_size}",
        trial_seed=gt.seed,
        pool_size=len(gt.donors),
        need_count=need.count,
        calls_used=transport.calls_placed,
        accepted_by_provider=transport.calls_placed,
        contact_attempts=transport.calls_placed,
        ambiguous_count=0,
        dispatch_rounds=rounds,
        confirmed_candidate_ids=confirmed,
        contacted_candidate_ids=contacted,
        filled=len(confirmed) >= need.count,
        modeled_time_to_fill_s=_modeled_time_for(gt, contacted) if contacted else None,
    )


async def run_call_all(need: Need, gt: TrialGroundTruth) -> PolicyRunResult:
    """Extreme contact baseline: dispatch to everyone up to the hard call
    budget in one shot, same acceptance rule as the other policies."""
    transport = FixtureTransport(gt)
    ranked = rank_candidates([d.candidate for d in gt.donors])
    batch = ranked[: need.max_calls]
    results = await _dispatch_and_resolve(transport, batch, need)
    confirmed = [r.candidate_id for r in results if r.outcome in CONFIRMED_OUTCOMES and r.commitment_score >= COMMITMENT_THRESHOLD]
    contacted = [r.candidate_id for r in results]

    return PolicyRunResult(
        policy="call_all",
        trial_seed=gt.seed,
        pool_size=len(gt.donors),
        need_count=need.count,
        calls_used=transport.calls_placed,
        accepted_by_provider=transport.calls_placed,
        contact_attempts=transport.calls_placed,
        ambiguous_count=0,
        dispatch_rounds=1,
        confirmed_candidate_ids=confirmed,
        contacted_candidate_ids=contacted,
        filled=len(confirmed) >= need.count,
        modeled_time_to_fill_s=_modeled_time_for(gt, contacted) if contacted else None,
    )


async def run_stated_yes_only(need: Need, gt: TrialGroundTruth) -> PolicyRunResult:
    """Trust any stated yes with pickup, ignoring commitment calibration
    entirely -- against the SAME fixture ground truth as every other policy
    (not an independently-sampled stream, unlike the old harness's version)."""
    transport = FixtureTransport(gt)
    ranked = rank_candidates([d.candidate for d in gt.donors])
    confirmed: list[str] = []
    contacted: list[str] = []
    rounds = 0
    for candidate in ranked:
        if len(confirmed) >= need.count or transport.calls_placed >= need.max_calls:
            break
        results = await _dispatch_and_resolve(transport, [candidate], need)
        rounds += 1
        for r in results:
            contacted.append(r.candidate_id)
            if r.stated_yes and r.outcome != CallOutcome.NO_ANSWER:
                confirmed.append(r.candidate_id)

    return PolicyRunResult(
        policy="stated_yes_only",
        trial_seed=gt.seed,
        pool_size=len(gt.donors),
        need_count=need.count,
        calls_used=transport.calls_placed,
        accepted_by_provider=transport.calls_placed,
        contact_attempts=transport.calls_placed,
        ambiguous_count=0,
        dispatch_rounds=rounds,
        confirmed_candidate_ids=confirmed,
        contacted_candidate_ids=contacted,
        filled=len(confirmed) >= need.count,
        modeled_time_to_fill_s=_modeled_time_for(gt, contacted) if contacted else None,
    )


POLICIES = ["ranked_greedy_real", "sequential", "fixed_waves_5", "stated_yes_only", "call_all"]


async def run_all_policies(need: Need, gt: TrialGroundTruth, ledger_path: str) -> dict[str, PolicyRunResult]:
    return {
        "ranked_greedy_real": await run_ranked_greedy_real(need, gt, ledger_path),
        "sequential": await run_sequential(need, gt),
        "fixed_waves_5": await run_fixed_waves(need, gt, batch_size=5),
        "stated_yes_only": await run_stated_yes_only(need, gt),
        "call_all": await run_call_all(need, gt),
    }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


@dataclass
class TrialRow:
    """One flat row per (policy, trial) -- the raw per-trial data the plan
    requires alongside aggregates."""

    policy: str
    seed: int
    pool_size: int
    need_count: int
    filled_counted: bool  # accepted commitments reached need.count
    accepted_roster_size: int
    accepted_roster_true_showups: int  # actual synthetic attendance among the accepted roster
    contacted_count: int
    contacted_true_showups: int  # actual synthetic attendance among ALL contacted (incl. rejected soft-yeses)
    calls_used: int
    contact_attempts: int
    dispatch_rounds: int
    ambiguous_count: int
    modeled_time_to_fill_s: float | None


def build_trial_rows(gt: TrialGroundTruth, run_results: dict[str, PolicyRunResult]) -> list[TrialRow]:
    rows = []
    for policy, r in run_results.items():
        accepted_showups = sum(1 for cid in r.confirmed_candidate_ids if _true_showup(gt, cid))
        contacted_showups = sum(
            1 for cid in r.contacted_candidate_ids
            if gt.outcomes[cid].can_come == "yes" and _true_showup(gt, cid)
        )
        rows.append(TrialRow(
            policy=policy,
            seed=gt.seed,
            pool_size=r.pool_size,
            need_count=r.need_count,
            filled_counted=r.filled,
            accepted_roster_size=len(r.confirmed_candidate_ids),
            accepted_roster_true_showups=accepted_showups,
            contacted_count=len(r.contacted_candidate_ids),
            contacted_true_showups=contacted_showups,
            calls_used=r.calls_used,
            contact_attempts=r.contact_attempts,
            dispatch_rounds=r.dispatch_rounds,
            ambiguous_count=r.ambiguous_count,
            modeled_time_to_fill_s=r.modeled_time_to_fill_s,
        ))
    return rows


def summarize_policy(rows: list[TrialRow]) -> dict:
    n = len(rows)
    if n == 0:
        return {}
    counted_target_attainment = statistics.mean(1.0 if r.filled_counted else 0.0 for r in rows)
    missed_target_rate = 1.0 - counted_target_attainment

    # Actual synthetic target attainment among the accepted roster: did the
    # accepted roster's TRUE showups reach need_count?
    actual_attainment_accepted = statistics.mean(
        1.0 if r.accepted_roster_true_showups >= r.need_count else 0.0 for r in rows
    )
    # Actual synthetic attendance among ALL contacted (rejected soft-yeses included).
    actual_attainment_all_contacted = statistics.mean(
        1.0 if r.contacted_true_showups >= r.need_count else 0.0 for r in rows
    )

    excess_positive_commitments = statistics.mean(max(0, r.accepted_roster_size - r.need_count) for r in rows)
    excess_actual_arrivals = statistics.mean(max(0, r.accepted_roster_true_showups - r.need_count) for r in rows)

    mean_calls_used = statistics.mean(r.calls_used for r in rows)
    mean_contact_attempts = statistics.mean(r.contact_attempts for r in rows)
    mean_dispatch_rounds = statistics.mean(r.dispatch_rounds for r in rows)
    zero_confirmation_rate = statistics.mean(1.0 if r.accepted_roster_size == 0 else 0.0 for r in rows)

    # Precision: of the accepted roster, what fraction would actually show up.
    precision_vals = [r.accepted_roster_true_showups / r.accepted_roster_size for r in rows if r.accepted_roster_size > 0]
    precision = statistics.mean(precision_vals) if precision_vals else None
    precision_n = len(precision_vals)

    # Recall (meaningful only relative to contacted true positives): of
    # everyone contacted who would truly show up, what fraction did the
    # policy accept into its roster.
    recall_vals = []
    for r in rows:
        if r.contacted_true_showups > 0:
            recall_vals.append(min(1.0, r.accepted_roster_true_showups / r.contacted_true_showups))
    recall = statistics.mean(recall_vals) if recall_vals else None
    recall_n = len(recall_vals)

    contacts_per_useful_outcome = statistics.mean(
        (r.contact_attempts / r.accepted_roster_size) if r.accepted_roster_size > 0 else float("nan")
        for r in rows
        if r.accepted_roster_size > 0
    ) if any(r.accepted_roster_size > 0 for r in rows) else None

    modeled_times = [r.modeled_time_to_fill_s for r in rows if r.modeled_time_to_fill_s is not None]
    mean_modeled_time = statistics.mean(modeled_times) if modeled_times else None

    untouched = statistics.mean(max(0, r.pool_size - r.contacted_count) for r in rows)

    return {
        "n_trials": n,
        "counted_target_attainment": round(counted_target_attainment, 4),
        "missed_target_rate": round(missed_target_rate, 4),
        "actual_target_attainment_accepted_roster": round(actual_attainment_accepted, 4),
        "actual_target_attainment_all_contacted": round(actual_attainment_all_contacted, 4),
        "excess_positive_commitments_mean": round(excess_positive_commitments, 3),
        "excess_actual_arrivals_mean": round(excess_actual_arrivals, 3),
        "mean_calls_used": round(mean_calls_used, 3),
        "mean_contact_attempts": round(mean_contact_attempts, 3),
        "mean_dispatch_rounds": round(mean_dispatch_rounds, 3),
        "zero_confirmation_rate": round(zero_confirmation_rate, 4),
        "precision_mean": round(precision, 4) if precision is not None else None,
        "precision_denominator": "accepted_roster_true_showups / accepted_roster_size, macro-averaged over trials with a nonempty roster",
        "precision_n_trials": precision_n,
        "recall_mean": round(recall, 4) if recall is not None else None,
        "recall_denominator": "accepted_roster_true_showups / contacted_true_showups, only over trials with >=1 true-showup contact",
        "recall_n_trials": recall_n,
        "contacts_per_useful_outcome_mean": round(contacts_per_useful_outcome, 3) if contacts_per_useful_outcome is not None else None,
        "definitely_untouched_eligible_mean": round(untouched, 3),
        "modeled_mean_time_to_fill_s": round(mean_modeled_time, 2) if mean_modeled_time is not None else None,
    }


def bootstrap_diff_ci(a_vals: list[float], b_vals: list[float], *, n_boot: int = 2000, seed: int = 42, alpha: float = 0.05) -> dict:
    """Paired bootstrap CI on mean(a - b) across matched trials (same seed
    order in both lists)."""
    assert len(a_vals) == len(b_vals)
    n = len(a_vals)
    diffs = [a - b for a, b in zip(a_vals, b_vals)]
    point = statistics.mean(diffs) if diffs else 0.0
    rng = random.Random(seed)
    boot_means = []
    for _ in range(n_boot):
        sample = [diffs[rng.randrange(n)] for _ in range(n)]
        boot_means.append(statistics.mean(sample))
    boot_means.sort()
    lo_idx = int((alpha / 2) * n_boot)
    hi_idx = int((1 - alpha / 2) * n_boot) - 1
    return {
        "point_estimate": round(point, 4),
        "ci_low": round(boot_means[max(0, lo_idx)], 4),
        "ci_high": round(boot_means[min(n_boot - 1, hi_idx)], 4),
        "n_trials": n,
        "n_boot": n_boot,
    }
