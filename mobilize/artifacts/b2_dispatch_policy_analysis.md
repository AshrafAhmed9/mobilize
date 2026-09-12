# Dispatch policy analysis: retain and document

No changes to `mobilize/core/planner.py`, `mobilize/core/dispatcher.py`,
or `mobilize/core/commitment.py` in this task. Additive-only work: one new test
file (`mobilize/tests/test_b2_planner_invariants.py`, 9 tests, all passing)
that pins the current planner's behavior with exact hand-worked examples and
monotonicity checks. No production code changed.

## Decision

**Retain the current heuristic planner. Do not add a probabilistic wave-sizer
or a separate sequential-dispatch code path.** This is a documented,
evidence-based decision, not a default or a skipped task.

## What the current planner actually does

`mobilize/core/planner.py::plan_wave` ranks the eligible pool by
`Candidate.prior_score()` (a fixed weighted blend: 0.4 historical accept
rate + 0.4 historical show-up rate + 0.1 recency + 0.1 distance, all in
`mobilize/core/types.py`), then greedily takes the smallest ranked prefix
whose summed `prior_score()` reaches `need.count * safety_margin`
(`safety_margin` fixed at 1.3 in production).

This is **neither a calibrated fill probability nor a complete
overshoot optimizer**:

- `prior_score()` is a hand-weighted heuristic blend, not a fitted or
  calibrated probability of pickup, commitment, or arrival. Treating its
  sum as an "expected confirmation count" is only correct if each
  candidate's true show-up probability actually equals their
  `prior_score()` and outcomes are independent — neither is verified
  against real data (that's B3's job, and B3 hasn't happened).
- The 1.3 safety margin is a fixed constant, not derived from any
  declared target probability or overshoot tolerance. It doesn't adapt to
  urgency, deadline pressure, or how many calls are already in flight.
- It is a correct greedy solution to one narrow sub-problem (maximize
  expected count under a cardinality constraint, given independent trials
  with known success probabilities) — but "known success probabilities"
  is doing a lot of unverified work in that sentence.

README.md's own description of this (`### 3. Over-recruitment as
constrained optimization`, lines ~152-157) is reasonably careful — it says
"priors" and "expected," and doesn't claim calibration for the planner
itself (calibration is correctly scoped to `commitment.py`'s stated-yes
scoring, a separate module). One place overclaims informally: line 513,
in the "what was cut" section, calls it "the planner's optimizer" in
passing. Flagging as a follow-up for README.md rather than editing it
here, to keep this change additive-only. It's a minor wording issue (the "optimizer"
framing is defensible for the narrow sub-problem it solves), not a
substantive correctness claim, but "optimizer" without qualification
invites exactly the overclaim this task exists to catch.

## Why no new policy is being added

**1. B1 never tested `need.count == 1`, so there's no evidence for the
exact case the plan calls out.** The plan explicitly names "one-slot or
zero-overshoot situations" as a case where "sequential is a legitimate
design." B1's matched-policy experiment (`matched_policy_experiment_b1.md`)
swept `need_count` in `{2, 5, 10}` only. Building a distinct code path for
`need_count == 1` on the strength of an experiment that never ran that
case would be exactly the thing this task's instructions warn against —
shipping a variant without evidence it helps the case it targets.

**2. The evidence B1 does have doesn't show a clean win for sequential,
even generalized.** Held-out bootstrap CI for `ranked_greedy_real` minus
`sequential` on `calls_used_diff`: point estimate 0.78, CI [0.63, 0.96] —
batched waves place more calls than sequential for equivalent outcomes.
But `precision_diff` CI is [-0.008, 0.012], straddling zero — no
precision difference. Sequential's real cost is `mean_dispatch_rounds`
≈ `mean_calls_used` (one network round-trip per call, vs. ~3.6 rounds for
batched), i.e. dispatch latency, which B1 explicitly does not model as
wall-clock. So "sequential uses fewer calls" is real but "sequential is
strictly better" is not established — it trades call count for round-trip
latency, and nothing in the current codebase measures whether that
latency matters more than the calls it saves for a given need's deadline.
This is precisely B1's own conclusion: no policy dominates.

**3. A structural scoping constraint, not just a data gap: an
additive planner-only change can't actually reach production without
touching `dispatcher.py`'s wave loop.** `plan_wave` is called from
`dispatcher.py`'s wave loop, which this task is explicitly forbidden from
touching (hard guardrail in the task brief: "Do NOT touch
`mobilize/core/dispatcher.py`'s core wave loop"). An opt-in flag added to
`plan_wave` (e.g. "if `need_count == 1`, return top-1 only") would be
inert dead code unless `dispatcher.py` is changed to call it — code that
exists but is never exercised by production is worse than no code: it's
a maintenance liability and a place for the two paths to silently drift
apart. Wiring it in is legitimately a separate, larger, `dispatcher.py`-
touching change that needs its own review and its own evidence, not
something to smuggle in under a "planner-only, additive" task.

**4. Probabilistic wave sizing (Poisson-binomial-style, separating
pickup/commitment/arrival probabilities) was considered and explicitly
not built**, per the plan's own permission ("if no variant improves the
tradeoff, retain the simpler implementation and publish why"). Reasons:
  - It requires per-stage probability estimates (pickup rate, commitment-
    given-pickup rate, arrival-given-commitment rate) from "appropriate
    observed denominators" — those denominators don't exist yet. The only
    historical signal in the codebase is `historical_accept_rate` and
    `historical_showup_rate`, already the two inputs `prior_score()` uses;
    there's no richer stage-by-stage breakdown to estimate from without
    B3's real observation ledger.
  - The independence assumption a Poisson-binomial calculation needs is
    exactly as unverified for a 3-stage model as it is for the current
    1-stage heuristic — correlated failure modes (shared transport
    outage, weather, time-of-day) aren't measured anywhere in this
    codebase, so a fancier model would inherit the same unverified
    independence assumption while looking more authoritative. That's a
    worse failure mode, not a better one: false precision.
  - Building it against simulated data only (no real calibration data
    exists — B3 hasn't happened) would produce a probability that reads
    as "calibrated" to a judge or coordinator but isn't, which is the
    exact overclaim this task exists to prevent.

## Invariant review

Properties the planner (`rank_candidates` / `plan_wave`) must never
violate, each backed by a test in
`mobilize/tests/test_b2_planner_invariants.py` (new) and
`mobilize/tests/test_planner.py` (pre-existing):

| # | Invariant | Test |
|---|---|---|
| 1 | `plan_wave`'s chosen candidates are always a prefix of `rank_candidates(pool)` in order — never skips a higher-ranked candidate for a lower-ranked one | `test_invariant_chosen_is_a_prefix_of_ranked_order` |
| 2 | When the target is unreachable, the full (eligible) pool is returned and `expected_confirmations` is reported honestly below target — never padded, never silently truncated to hide the shortfall | `test_hand_worked_exact_target_unreachable_returns_full_pool_short_of_target` |
| 3 | When the target is reachable, `plan_wave` stops at the smallest sufficient prefix — never over-picks past the point the target is met | `test_hand_worked_exact_target_reached_stops_at_smallest_sufficient_prefix`, `test_plan_wave_stops_once_expected_meets_target` (existing) |
| 4 | Wave size is non-decreasing in `need_count` (larger needs never get smaller waves) | `test_monotonic_wave_size_nondecreasing_in_need_count` |
| 5 | Wave size is non-decreasing in `safety_margin` (more cushion never means fewer calls) | `test_monotonic_wave_size_nondecreasing_in_safety_margin` |
| 6 | Wave size is non-increasing in candidate quality (stronger priors across the pool never force a bigger wave for the same target) | `test_monotonic_wave_size_nonincreasing_in_candidate_quality` |
| 7 | `max_wave_size` is a hard cap that always wins over the target — the planner never exceeds a declared call budget to chase the safety margin | `test_max_wave_size_hard_cap_wins_over_target`, `test_plan_wave_respects_max_wave_size` (existing) |
| 8 | Ineligible candidates are never chosen, regardless of prior strength | `test_ineligible_never_chosen_even_with_best_priors`, `test_rank_candidates_excludes_ineligible` (existing) |
| 9 | Empty pool produces an empty, zero-expectation plan — never an error, never a fabricated nonzero expectation | `test_empty_pool_is_empty_plan_not_an_error`, `test_plan_wave_empty_pool_returns_empty_plan` (existing) |

## Decision explanation a coordinator would see

None of this task's output changes what a coordinator sees today — the
planner's behavior is unchanged. For the record, what a coordinator can
already infer from `WavePlan` (`mobilize/core/planner.py`) about why a
wave was sized the way it was: `expected_confirmations` shows the summed
`prior_score()` the wave is banking on, comparable directly against
`need.count * 1.3` to see whether the wave reached its target or was cut
short by pool exhaustion or `max_wave_size`. There is no natural-language
explanation surfaced anywhere today (e.g. "called N people because
priors were weak and margin required 1.3x"); adding one would be a small,
genuinely additive UI/logging change with no policy risk, and is a
reasonable follow-up for dashboard/CLI output — not
in scope here since it touches no dispatch logic.

## Sensitivity analysis

Not applicable — no probabilistic component was added. The monotonicity
tests above are the sensitivity analysis for the *existing* heuristic:
they confirm `plan_wave`'s two free parameters (`need_count`,
`safety_margin`) move the wave size in the expected direction and that
candidate quality moves it in the opposite expected direction, across the
input space, not just at one hand-picked point.

## No real-world probability guarantee

Nothing in this artifact, `planner.py`, or the new tests constitutes or
implies a real-world probability guarantee. `prior_score()` remains an
unvalidated heuristic blend of historical rates; B3 (real outcome
calibration with consenting participants) has not happened, and until it
does, no claim of calibrated fill probability is being made anywhere in
this codebase's dispatch policy.

## Test suite status

New file: `mobilize/tests/test_b2_planner_invariants.py`, 9 tests, all
passing standalone (`.venv/bin/python -m pytest
mobilize/tests/test_b2_planner_invariants.py -q` → `9 passed`). No
existing test file modified. Full-suite and harness reproduction results
are in this artifact's accompanying report, not duplicated here to avoid
two sources of truth for the same numbers.
