# B1 — Matched policy experiment with strong baselines

Owner: Lane B. Additive only — no changes to `mobilize/core/dispatcher.py`,
`mobilize/core/policy.py`, or `mobilize/core/commitment.py`. The historical
harness (`mobilize/sim/harness.py`, audited in `benchmark_audit_b0.md`) is
untouched and kept as-is.

## What this is

Five policies run against the SAME pre-generated, per-candidate ground truth
per trial — not five independently-sampled runs that happen to share a seed
number (that was the old harness's limitation, documented in B0). Ground
truth (final intent, pickup, stated answer, commitment-bearing evidence text,
true show-up, and a modeled call duration) is generated once per trial,
before any policy runs, using per-candidate RNG streams keyed by
`(seed, candidate_id, salt)` rather than a shared sequential stream — so a
candidate's outcome cannot be perturbed by which policy calls them, what
order they're called in, or how many other candidates are dispatched first.
Verified directly by `test_outcome_invariant_to_dispatch_order` and
`test_outcome_invariant_to_concurrency` in
`mobilize/tests/test_matched_experiment.py`.

Policies compared:

1. **`ranked_greedy_real`** — the REAL production `mobilize()` dispatcher
   (`mobilize/core/dispatcher.py`), run through a `FixtureTransport` that
   implements the same `Transport` interface as `SimulatedTransport` but
   resolves each candidate's call from the pre-generated ground truth
   instead of drawing from a shared RNG. Not a reimplementation.
2. **`sequential`** — one candidate at a time, same `rank_candidates()`
   ordering and the same acceptance rule (`CONFIRMED_OUTCOMES` +
   `COMMITMENT_THRESHOLD`, both imported from `dispatcher.py`, not
   re-derived). Differential-tested against #1 in
   `test_sequential_matches_real_dispatcher_when_one_wave_suffices`: on a
   small case where production fills within a single wave, the sequential
   reimplementation's confirmed roster is asserted to match production's
   exactly.
3. **`fixed_waves_5`** — predeclared fixed batch size of 5, same ranking and
   acceptance rule. Cross-checked against `call_all` in
   `test_fixed_waves_full_batch_matches_call_all` (same acceptance logic,
   full-pool batch, must produce identical confirmed/contacted sets).
4. **`stated_yes_only`** — trusts any stated yes with pickup, ignoring
   commitment calibration, run against the same fixture ground truth as
   every other policy (unlike the old harness's independently-sampled
   version).
5. **`call_all`** — dispatch everyone in ranked order up to the hard
   `max_calls` budget in one round, same acceptance rule.

(#6, an improved planner from B2, is out of scope for B1 per the plan.)

## Reproduction

- Command: `.venv/bin/python -m mobilize.sim.run_matched_experiment`
- Environment: `.venv/bin/python` = Python 3.14.6
- Commit at run time: `35ff46782dd785a5bdeb4e6fbfeb04b86ca39dff`
- Run timestamp: 2026-09-11T19:43:20Z
- Actual wall-clock: **1.50s** real (`/usr/bin/time -p`), 0.87s measured
  inside the script itself (difference is Python interpreter startup).
  Far under the "few hundred trials, reasonable time" budget — the fixture
  transport has zero real I/O and near-zero artificial latency
  (`poll_latency_s=0.0`), unlike `SimulatedTransport`'s deliberate
  0.05–0.4s per-call latency draws, which is why this experiment is ~200x
  faster than the old 200-trial harness's 4–5 minutes.
- Seeds: **dev** = `range(2000, 2020)` (20 seeds), **held-out** =
  `range(9000, 9040)` (40 seeds) — disjoint ranges, held-out was never
  inspected while building the policy code above; it exists purely to
  confirm the dev numbers weren't a fluke of the dev seed range.
- Roster/need sweep: `(pool_size=20, need=2)`, `(pool_size=60, need=5)`,
  `(pool_size=150, need=10)` — three sizes, `max_calls = min(pool_size, 40)`,
  `deadline_minutes=60` for every trial, identical across all five policies.
- Total trial-policy runs: dev 20 seeds × 3 sizes × 5 policies = 300 rows;
  held-out 40 seeds × 3 sizes × 5 policies = 600 rows. 900 rows total, all
  included in the aggregates below (no filtering to "filled" runs only).

## Dev-split aggregates (sanity check only — not the headline)

```json
{
  "ranked_greedy_real": {"n_trials": 60, "counted_target_attainment": 0.7167, "missed_target_rate": 0.2833, "actual_target_attainment_accepted_roster": 0.3667, "actual_target_attainment_all_contacted": 0.6667, "excess_positive_commitments_mean": 0.067, "excess_actual_arrivals_mean": 0.017, "mean_calls_used": 23.033, "mean_contact_attempts": 23.033, "mean_dispatch_rounds": 3.267, "zero_confirmation_rate": 0.0167, "precision_mean": 0.8479, "precision_n_trials": 59, "recall_mean": 0.7758, "recall_n_trials": 59, "contacts_per_useful_outcome_mean": 5.744, "definitely_untouched_eligible_mean": 53.633, "modeled_mean_time_to_fill_s": 1224.75},
  "sequential": {"n_trials": 60, "counted_target_attainment": 0.7167, "missed_target_rate": 0.2833, "actual_target_attainment_accepted_roster": 0.35, "actual_target_attainment_all_contacted": 0.6667, "excess_positive_commitments_mean": 0, "excess_actual_arrivals_mean": 0, "mean_calls_used": 22.117, "mean_dispatch_rounds": 22.117, "zero_confirmation_rate": 0.0167, "precision_mean": 0.8483, "precision_n_trials": 59, "recall_mean": 0.7792, "recall_n_trials": 59, "contacts_per_useful_outcome_mean": 5.494, "definitely_untouched_eligible_mean": 54.55, "modeled_mean_time_to_fill_s": 1176.5},
  "fixed_waves_5": {"n_trials": 60, "counted_target_attainment": 0.7167, "missed_target_rate": 0.2833, "actual_target_attainment_accepted_roster": 0.3667, "actual_target_attainment_all_contacted": 0.6833, "excess_positive_commitments_mean": 0.217, "excess_actual_arrivals_mean": 0.133, "mean_calls_used": 23.75, "mean_dispatch_rounds": 4.8, "zero_confirmation_rate": 0.0167, "precision_mean": 0.8493, "precision_n_trials": 59, "recall_mean": 0.7727, "recall_n_trials": 59, "contacts_per_useful_outcome_mean": 5.78, "definitely_untouched_eligible_mean": 52.917, "modeled_mean_time_to_fill_s": 1264.43},
  "stated_yes_only": {"n_trials": 60, "counted_target_attainment": 0.95, "missed_target_rate": 0.05, "actual_target_attainment_accepted_roster": 0.25, "actual_target_attainment_all_contacted": 0.25, "excess_positive_commitments_mean": 0, "excess_actual_arrivals_mean": 0, "mean_calls_used": 16.283, "mean_dispatch_rounds": 16.283, "zero_confirmation_rate": 0.0, "precision_mean": 0.7406, "precision_n_trials": 60, "recall_mean": 1.0, "recall_n_trials": 57, "contacts_per_useful_outcome_mean": 3.027, "definitely_untouched_eligible_mean": 60.383, "modeled_mean_time_to_fill_s": 865.39},
  "call_all": {"n_trials": 60, "counted_target_attainment": 0.7167, "missed_target_rate": 0.2833, "actual_target_attainment_accepted_roster": 0.5667, "actual_target_attainment_all_contacted": 0.7833, "excess_positive_commitments_mean": 1.3, "excess_actual_arrivals_mean": 0.767, "mean_calls_used": 31.35, "mean_dispatch_rounds": 1, "zero_confirmation_rate": 0.0167, "precision_mean": 0.8465, "precision_n_trials": 59, "recall_mean": 0.745, "recall_n_trials": 59, "contacts_per_useful_outcome_mean": 6.48, "definitely_untouched_eligible_mean": 45.317, "modeled_mean_time_to_fill_s": 1666.47}
}
```

## Held-out aggregates (the real headline — all 600 rows, all trials included)

```json
{
  "ranked_greedy_real": {"n_trials": 120, "counted_target_attainment": 0.65, "missed_target_rate": 0.35, "actual_target_attainment_accepted_roster": 0.4167, "actual_target_attainment_all_contacted": 0.7583, "excess_positive_commitments_mean": 0.175, "excess_actual_arrivals_mean": 0.108, "mean_calls_used": 23.933, "mean_dispatch_rounds": 3.583, "zero_confirmation_rate": 0.025, "precision_mean": 0.8941, "precision_n_trials": 117, "recall_mean": 0.7565, "recall_n_trials": 118, "contacts_per_useful_outcome_mean": 5.682, "definitely_untouched_eligible_mean": 52.733, "modeled_mean_time_to_fill_s": 1254.72},
  "sequential": {"n_trials": 120, "counted_target_attainment": 0.65, "missed_target_rate": 0.35, "actual_target_attainment_accepted_roster": 0.3833, "actual_target_attainment_all_contacted": 0.7417, "excess_positive_commitments_mean": 0, "excess_actual_arrivals_mean": 0, "mean_calls_used": 23.15, "mean_dispatch_rounds": 23.15, "zero_confirmation_rate": 0.025, "precision_mean": 0.8923, "precision_n_trials": 117, "recall_mean": 0.7531, "recall_n_trials": 117, "contacts_per_useful_outcome_mean": 5.641, "definitely_untouched_eligible_mean": 53.517, "modeled_mean_time_to_fill_s": 1212.88},
  "fixed_waves_5": {"n_trials": 120, "counted_target_attainment": 0.65, "missed_target_rate": 0.35, "actual_target_attainment_accepted_roster": 0.45, "actual_target_attainment_all_contacted": 0.775, "excess_positive_commitments_mean": 0.275, "excess_actual_arrivals_mean": 0.167, "mean_calls_used": 24.45, "mean_dispatch_rounds": 4.942, "zero_confirmation_rate": 0.025, "precision_mean": 0.8991, "precision_n_trials": 117, "recall_mean": 0.7542, "recall_n_trials": 118, "contacts_per_useful_outcome_mean": 5.734, "definitely_untouched_eligible_mean": 52.217, "modeled_mean_time_to_fill_s": 1285.35},
  "stated_yes_only": {"n_trials": 120, "counted_target_attainment": 0.9667, "missed_target_rate": 0.0333, "actual_target_attainment_accepted_roster": 0.3583, "actual_target_attainment_all_contacted": 0.3583, "excess_positive_commitments_mean": 0, "excess_actual_arrivals_mean": 0, "mean_calls_used": 15.55, "mean_dispatch_rounds": 15.55, "zero_confirmation_rate": 0.0, "precision_mean": 0.7945, "precision_n_trials": 120, "recall_mean": 1.0, "recall_n_trials": 116, "contacts_per_useful_outcome_mean": 3.048, "definitely_untouched_eligible_mean": 61.117, "modeled_mean_time_to_fill_s": 812.45},
  "call_all": {"n_trials": 120, "counted_target_attainment": 0.65, "missed_target_rate": 0.35, "actual_target_attainment_accepted_roster": 0.5667, "actual_target_attainment_all_contacted": 0.8167, "excess_positive_commitments_mean": 1.225, "excess_actual_arrivals_mean": 0.825, "mean_calls_used": 31.283, "mean_dispatch_rounds": 1, "zero_confirmation_rate": 0.025, "precision_mean": 0.8858, "precision_n_trials": 117, "recall_mean": 0.7367, "recall_n_trials": 118, "contacts_per_useful_outcome_mean": 6.377, "definitely_untouched_eligible_mean": 45.383, "modeled_mean_time_to_fill_s": 1645.12}
}
```

Dev and held-out numbers are close for every policy on every metric — no
sign of overfitting to the dev seed range, and nothing here was tuned after
looking at held-out.

## Paired bootstrap CIs (held-out split, 2000 resamples, matched by trial)

`ranked_greedy_real` minus each other policy, on `calls_used` and on
accepted-roster precision (`accepted_roster_true_showups / accepted_roster_size`
per trial, 0 for empty rosters):

```json
{
  "sequential": {
    "calls_used_diff": {"point_estimate": 0.7833, "ci_low": 0.625, "ci_high": 0.9583},
    "precision_diff":  {"point_estimate": 0.0018, "ci_low": -0.0078, "ci_high": 0.0117}
  },
  "fixed_waves_5": {
    "calls_used_diff": {"point_estimate": -0.5167, "ci_low": -0.7583, "ci_high": -0.2833},
    "precision_diff":  {"point_estimate": -0.0049, "ci_low": -0.0127, "ci_high": 0.0008}
  },
  "stated_yes_only": {
    "calls_used_diff": {"point_estimate": 8.3833, "ci_low": 7.1417, "ci_high": 9.6333},
    "precision_diff":  {"point_estimate": 0.0772, "ci_low": 0.0296, "ci_high": 0.1225}
  },
  "call_all": {
    "calls_used_diff": {"point_estimate": -7.35, "ci_low": -8.9333, "ci_high": -5.8083},
    "precision_diff":  {"point_estimate": 0.0081, "ci_low": -0.0028, "ci_high": 0.0197}
  }
}
```

n=120 paired trials for every comparison. Full dev-split CI block is in the
raw output file (same shape, n=60).

## What this actually shows — read the tradeoffs, don't pick a winner

- **`ranked_greedy_real` vs. `sequential`**: CI on `calls_used_diff` is
  entirely positive (0.63–0.96) — production's batched waves place
  measurably *more* calls than strict one-at-a-time sequential dispatch for
  the same fill outcome, at statistically indistinguishable precision (CI
  straddles 0). Sequential is the more call-efficient policy here; its cost
  is dispatch latency (`mean_dispatch_rounds` ≈ `mean_calls_used`, i.e. one
  round-trip per call, vs. ~3.6 rounds for the batched policy) — real-world
  wall-clock, not modeled here beyond the synthetic per-call duration.
- **`ranked_greedy_real` vs. `fixed_waves_5`**: fixed-size waves place
  *fewer* calls on average (CI entirely negative, −0.76 to −0.28) with a
  precision CI that leans slightly negative but straddles 0 — a small,
  not-clearly-significant edge for fixed-size batching on this call-budget
  metric alone. Not a dominance result either way.
- **`ranked_greedy_real` vs. `stated_yes_only`**: stated-yes-only uses far
  fewer calls (CI 7.1–9.6 fewer) but its accepted-roster precision is
  measurably *worse* (CI 0.030–0.123 lower) — exactly the calibration gap
  this whole project exists to catch. `actual_target_attainment_all_contacted`
  for stated-yes-only (0.358) equals its `actual_target_attainment_accepted_roster`
  (0.358) by construction (stated-yes-only's "accepted" and "contacted-who-
  said-yes" sets coincide), while the calibrated policy's much higher
  `actual_target_attainment_all_contacted` (0.758) shows real attendance
  potential sitting outside its accepted roster among rejected soft-yeses —
  over-recruitment the calibrated policy chose not to count as confirmed,
  not people it failed to reach.
- **`ranked_greedy_real` vs. `call_all`**: call-all places far more calls
  (CI 5.8–8.9 more) for a precision difference straddling 0 — no clear
  precision win from calling everyone, just more contact volume and much
  higher `excess_positive_commitments_mean` (1.225 vs 0.175) and
  `excess_actual_arrivals_mean` (0.825 vs 0.108) — call-all substantially
  over-recruits.
- **`missed_target_rate` is 0.35 for every batched/call-all-style policy on
  held-out**, notably worse than the old harness's 0.5% miss rate. This is
  not a regression — it's the same policy logic under materially harsher
  constraints than the old harness used: `max_calls` capped at 40 (or the
  pool size) instead of a fixed 40 against a fixed pool of 200, and a
  three-way size sweep including a 150-person pool needing 10 confirmations
  off a shared 40-call budget. That configuration is deliberately closer to
  the plan's "large target, scarce budget" adverse scenario than the old
  single-configuration harness ever tested. `stated_yes_only`'s much lower
  miss rate (0.033) here is exactly what you'd expect from a policy that
  accepts more people per call at the cost of the precision loss shown
  above — a real, quantified tradeoff, not a free win.
- No policy dominates on every axis. This report does not select a winner;
  B2 is where an improved planner gets proposed and separately evaluated
  against this same fixture.

## Differential/consistency tests (proving the reimplementations aren't a strawman)

`mobilize/tests/test_matched_experiment.py`, 7 tests, all passing:

- `test_pregeneration_is_deterministic_for_same_seed`
- `test_different_seeds_diverge`
- `test_outcome_invariant_to_dispatch_order` — same ground truth, forward vs.
  reversed dispatch order, per-candidate outcome identical either way.
- `test_outcome_invariant_to_concurrency` — same ground truth, sequential vs.
  `asyncio.gather`-concurrent dispatch, per-candidate outcome identical.
- `test_fixed_waves_full_batch_matches_call_all` — two independently-written
  policy loops, same acceptance wiring, must produce identical
  confirmed/contacted sets when their dispatch sets coincide.
- `test_sequential_matches_real_dispatcher_when_one_wave_suffices` — searches
  seeds 1–59 for a trial where the REAL `mobilize()` dispatcher fills in one
  wave, then asserts the `sequential` reimplementation's confirmed roster is
  set-identical to production's. This is the actual differential test
  against a small production run the plan requires for a reimplemented
  baseline — not a comparison between two of my own reimplementations.
- `test_stated_yes_only_accepts_strictly_more_or_equal_than_calibrated_would` —
  every candidate the calibrated-acceptance `sequential` policy confirms
  also stated yes; sanity check on the trust boundary between the two
  acceptance rules.

## Raw per-trial data

Full JSON (aggregates, bootstrap CIs, and all 600 held-out per-trial rows —
policy, seed, pool_size, need_count, filled_counted, accepted_roster_size,
accepted_roster_true_showups, contacted_count, contacted_true_showups,
calls_used, dispatch_rounds, ambiguous_count, modeled_time_to_fill_s) is
saved verbatim, unedited, at
`mobilize/artifacts/matched_policy_experiment_b1_raw_output.json`
(9,474 lines — the literal stdout of the reproduction command above).

First 15 raw rows (seeds 9000–9002, pool_size=20, need_count=2), as a sample:

```json
[
  {"policy": "ranked_greedy_real", "seed": 9000, "pool_size": 20, "need_count": 2, "filled_counted": true, "accepted_roster_size": 2, "accepted_roster_true_showups": 2, "contacted_count": 4, "contacted_true_showups": 2, "calls_used": 4, "dispatch_rounds": 1, "ambiguous_count": 0, "modeled_time_to_fill_s": 168.46},
  {"policy": "sequential", "seed": 9000, "pool_size": 20, "need_count": 2, "filled_counted": true, "accepted_roster_size": 2, "accepted_roster_true_showups": 2, "contacted_count": 3, "contacted_true_showups": 2, "calls_used": 3, "dispatch_rounds": 3, "ambiguous_count": 0, "modeled_time_to_fill_s": 147.85},
  {"policy": "fixed_waves_5", "seed": 9000, "pool_size": 20, "need_count": 2, "filled_counted": true, "accepted_roster_size": 3, "accepted_roster_true_showups": 3, "contacted_count": 5, "contacted_true_showups": 3, "calls_used": 5, "dispatch_rounds": 1, "ambiguous_count": 0, "modeled_time_to_fill_s": 255.84},
  {"policy": "stated_yes_only", "seed": 9000, "pool_size": 20, "need_count": 2, "filled_counted": true, "accepted_roster_size": 2, "accepted_roster_true_showups": 2, "contacted_count": 3, "contacted_true_showups": 2, "calls_used": 3, "dispatch_rounds": 3, "ambiguous_count": 0, "modeled_time_to_fill_s": 147.85},
  {"policy": "call_all", "seed": 9000, "pool_size": 20, "need_count": 2, "filled_counted": true, "accepted_roster_size": 4, "accepted_roster_true_showups": 4, "contacted_count": 17, "contacted_true_showups": 4, "calls_used": 17, "dispatch_rounds": 1, "ambiguous_count": 0, "modeled_time_to_fill_s": 857.12}
]
```

## Measurement dictionary additions (beyond B0's)

- **`filled_counted`** — accepted commitments (policy's believed-confirmed
  roster) reached `need.count`. Same meaning as the old harness's `filled`.
- **`actual_target_attainment_accepted_roster`** — fraction of trials where
  the accepted roster's TRUE (hidden ground-truth) show-ups reached
  `need.count`. Can be lower than `filled_counted` when the roster filled on
  paper but some accepted people wouldn't actually show.
- **`actual_target_attainment_all_contacted`** — fraction of trials where
  true show-ups among ALL contacted people (including rejected soft-yeses
  who stated yes but were below the commitment threshold, or below-threshold
  declines) reached `need.count`. Deliberately not restricted to the
  accepted roster — a rejected soft-yes who'd actually show is a form of
  under-counted attendance potential, and excluding them would understate
  what the population could support and overstate the marginal value of a
  stricter roster.
- **`excess_positive_commitments_mean`** / **`excess_actual_arrivals_mean`** —
  mean, across ALL trials (unfilled included, contributing 0 as appropriate,
  not excluded), of `max(0, accepted_roster_size - need.count)` and
  `max(0, accepted_roster_true_showups - need.count)` respectively.
- **`precision_mean`** — macro-averaged (mean of per-trial ratios, not
  pooled) `accepted_roster_true_showups / accepted_roster_size`, over trials
  with a nonempty roster only (`precision_n_trials` states the denominator
  trial count explicitly, distinct from `n_trials`).
- **`recall_mean`** — macro-averaged `accepted_roster_true_showups /
  contacted_true_showups`, over trials with at least one true-showup contact.
  Meaningful only relative to who was actually contacted in that trial, not
  a claim about the whole eligible pool.
- **`contacts_per_useful_outcome_mean`** — mean `contact_attempts /
  accepted_roster_size`, over trials with a nonempty roster.
- **`definitely_untouched_eligible_mean`** — mean `pool_size -
  contacted_count`, i.e. candidates never dispatched to at all in that trial.
- **`modeled_time_to_fill_s`** — sum of each contacted candidate's
  independently-drawn `modeled_call_duration_s` (uniform 15–90s, drawn from
  a stream distinct from the outcome stream). Explicitly a planning
  assumption, not measured real-call timing — kept in a field named
  `modeled_*` everywhere it appears, never conflated with a real duration.

## Limitations

- This is still simulation against a synthetic population
  (`mobilize/sim/population.py`), same hidden show-up model as the old
  harness. It validates policy LOGIC differences against a known oracle, not
  a claim about real human behavior beyond the separate real-call
  smoketests.
- `modeled_time_to_fill_s` is explicitly a planning assumption (independent
  synthetic duration draw), never measured wall-clock, and is reported as
  such everywhere it appears.
- The three roster/need sizes and two `max_calls` regimes tested are a
  deliberately harder configuration than the old harness's single
  fixed-200-pool/need-3/max-40 setup — this was a genuine design choice to
  probe the "large target, scarce budget" adverse scenario the plan calls
  for, not an attempt to find a flattering number. The 0.35 held-out
  missed-target rate should not be read as a regression against the old
  harness's 0.5%; the configurations are not comparable.
- `n=120` held-out paired trials is enough for the CIs above to be clearly
  non-degenerate and mostly bounded away from 0, but this is not a claim of
  a large-sample guarantee — a follow-up with a larger held-out set would
  tighten these further. Given the ~1.5s wall-clock cost of this run, that
  would be cheap to do if narrower CIs are needed later.
- `sequential` and `fixed_waves_5` bypass the ledger/crash-safety/governance
  machinery in `mobilize()` entirely (they dispatch/poll directly against
  `FixtureTransport`) — this is deliberate, since B1's job is to isolate
  ranking/acceptance/wave-shape tradeoffs, not re-test crash safety (already
  covered by `test_ledger.py` / `test_crash_safety.py`). It does mean these
  two policies' `calls_used` accounting doesn't include the ambiguous-dispatch
  bookkeeping production has; both reported 0 ambiguous calls throughout
  this run, consistent with `FixtureTransport` never raising.

## Test suite status

`.venv/bin/python -m pytest mobilize/tests/ -q` — see the harness status
line in the commit/PR this artifact ships with; run separately from this
experiment to avoid conflating simulation output with test output. New
tests added: `mobilize/tests/test_matched_experiment.py` (7 tests, all
passing standalone). No existing test file was modified.
