# F5 — Why use this instead of a sequential caller or a standard quota dialer?

## Question, workload, and thresholds (recorded before reading results below)

Workload assumed: a coordinator needs `need.count` confirmed people to show up
by a hard deadline, from a pool where most people either never pick up or say
something short of a real commitment. Acceptable overshoot: a few extra
confirmed/arrived people, not 10+. Deadline: hours, not days — the whole
premise of "mobilize" is a shortage discovered late. Budget: calls are cheap
relative to a missed headcount, but not free — a policy that silently 4x's
call volume for no measured gain is a real cost. Practical improvement
threshold: a policy is worth using over a naive baseline if it either (a)
fixes a target it would otherwise miss, or (b) meaningfully improves confirmed-
roster precision without a large, unexplained blowup in calls or wall-clock
time. No practitioner numbers were available for this project, so these
thresholds are labeled assumptions, not measured requirements.

Two comparisons follow: a standard quota dialer (call everyone / stop at
first N stated yeses) and a sequential one-at-a-time caller. Both are backed
by data already in this repo — no new benchmark was invented for either.

## 1. Standard quota dialer: `call_all` and `stated_yes_only`

Source: `mobilize/artifacts/benchmark_audit_b0.md` (200-trial harness,
reproduced via `.venv/bin/python -m mobilize.sim.harness`) and the matched,
harder held-out split in `mobilize/artifacts/matched_policy_experiment_b1.md`
(120 paired trials, same ground truth per trial across policies).

Old harness (200 trials, generous fixed budget):

| policy | fill_rate | confirmation_accuracy | mean_calls_used | mean_over_recruitment_ratio |
|---|---|---|---|---|
| calibrated (ours) | 0.995 | 0.937 | 10.62 | 3.54 |
| stated_yes_only | 1.0 | 0.863 | 7.04 | 2.35 |
| call_all | 1.0 | 0.800 | 40 | 13.33 |

Matched held-out split (harder budget, three pool sizes, paired bootstrap
CIs, n=120):

- **vs. `call_all`**: `ranked_greedy_real` uses 5.8–8.9 *fewer* calls (CI
  entirely negative) for a precision difference that straddles zero — no
  measurable precision win, just far less call volume, and `call_all` posts
  a much larger `excess_positive_commitments_mean` (1.225 vs 0.175) and
  `excess_actual_arrivals_mean` (0.825 vs 0.108). Calling everyone is a
  budget-burning way to get the same or worse roster quality.
- **vs. `stated_yes_only`**: the picture is genuinely mixed, and this is the
  honest tradeoff, not a clean win. `stated_yes_only` uses 7.1–9.6 *fewer*
  calls (CI entirely positive) and hits a higher `counted_target_attainment`
  (0.967 vs 0.65 on held-out) — trusting every stated yes fills the quota
  more often and cheaper. What it gives up is precision: accepted-roster
  precision is 0.030–0.123 *lower* (CI entirely negative), i.e. more of the
  people it counts as confirmed never show. That gap — a policy that looks
  like it "filled the quota" while quietly seating no-shows — is the exact
  failure mode this project is built to catch. Old-harness confirmation
  accuracy shows the same pattern: 86.3% (stated-yes) vs 93.7% (calibrated).

**Honest bottom line on the quota-dialer comparison**: mobilize's calibration
buys ~8 percentage points of confirmed-roster precision at a real cost of
roughly 7–9 more calls per trial and a lower raw fill rate under a tight
budget. If the coordinator only cares about "did I technically hit my
number," `stated_yes_only` (or a standard quota dialer that stops at the
first N stated yeses) is cheaper and fills more often. If the coordinator
cares whether the people it counted actually show up, calibration is worth
the extra calls. Neither policy dominates the other on every axis — the
project's advantage is conditional, not universal.

## 2. Sequential one-at-a-time caller

Source: `mobilize/artifacts/matched_policy_experiment_b1.md`, the `sequential`
policy — a real reimplementation (not hypothetical) using the production
`rank_candidates()` ordering and the exact same acceptance rule imported from
`mobilize/core/dispatcher.py`, differential-tested against the real
dispatcher in `test_sequential_matches_real_dispatcher_when_one_wave_suffices`.
This already exists in the repo; no new sequential policy or script was
needed or added.

On calls and precision alone, sequential is *not* worse than the batched
production dispatcher — held-out CIs show production places 0.63–0.96 *more*
calls than sequential for statistically indistinguishable precision (CI on
the precision difference straddles zero). If call count and accuracy were
the only axes, sequential would look like the better choice.

The reason mobilize doesn't run sequentially is throughput, not accuracy, and
that has to be argued structurally rather than from a synthetic benchmark,
because "sequential" here means *one live conversation at a time* — a
constraint the matched-trial harness doesn't model in wall-clock terms. The
one number the harness does surface that gestures at this:
`mean_dispatch_rounds` for `sequential` is 23.15 on held-out — essentially
equal to its own `mean_calls_used` (23.15), because a strictly one-at-a-time
caller needs one full round trip (dial, wait for pickup or voicemail,
converse, hang up, move to next) per call. The batched production dispatcher
needs only ~3.58 rounds for the same 23.9 calls, because a round dispatches a
whole wave of calls concurrently and only waits once per wave. That gap —
23 sequential round-trips vs. ~4 concurrent rounds — is not a modeled latency
number in this harness (`poll_latency_s` is not wall-clock realistic here),
so it should be read as a structural argument, not a timed benchmark: a
system that can only hold one conversation at a time needs roughly
(pool contacted) round-trips to finish, while a system that dispatches waves
concurrently needs roughly (pool contacted / wave size) round-trips. Against
a hard deadline measured in hours, with a pool in the dozens or hundreds,
that's the difference between finishing in time and not, independent of
which policy is more call-efficient or more precise. A human sequential
caller is the same constraint in a stronger form — they can't even queue a
second attempt while the first hasn't hung up.

This is a structural/throughput argument, deliberately not backed by a
fabricated "human caller made N calls in T minutes" statistic — no such
measurement exists in this repo, and inventing one would violate F5's
requirement not to benchmark an imagined competitor.

## 3. Documented external competitors (from prior contest research, not new benchmarks)

From `/Users/ashraf/.claude/plans/devpost-join-a-hackathon-zippy-panda.md`
(competitor scan performed during hackathon research, not a lab benchmark of
mobilize):

- **#218 `standby`** — a *deliberately sequential* shift-fill cascade that
  explicitly argues, by name, that parallelism is the wrong design for
  filling a single slot. That's a real, documented counter-position, and the
  argument above (throughput under a deadline, over a pool bigger than one
  slot) is the direct answer to it — for a single slot with no deadline
  pressure, sequential's call-efficiency edge shown in B1 may well be the
  better tradeoff; mobilize's parallel design earns its keep specifically
  when the pool is larger than one and the deadline is tight.
- **#296 `raktdaan`** — a real-world blood-donor recall program with
  *measured* field data (62,762 manual calls, 43% reached, 75.8% stated yes,
  9.18% actual donation/show-up). This is the closest thing to an external
  ground truth for the stated-yes-vs-actual-showup gap this project targets:
  a 75.8% stated-yes rate collapsing to a 9.18% real show-up rate is a much
  larger gap than anything in mobilize's synthetic harness, and it's real
  program data, not a simulation — it supports the *problem* (naive stated-
  yes trust badly overstates turnout) but says nothing about mobilize's own
  measured accuracy, since raktdaan is a manual, non-mobilize program.
- **#298 `blood-bank-dispatch`** — parallel fan-out like mobilize, but it
  enquires of institutions with a fixed question set and no stopping rule;
  it isn't a recruitment/commitment-calibration system and there's no
  comparable metric to benchmark against here.

None of these three provide a runnable benchmark comparable to the B0/B1
numbers above — they're cited only where they bear directly on the problem
framing or on a named competing design choice, per F5's instruction not to
treat documented availability as proof of measured behavior.

## Summary

- Against a standard quota dialer that calls everyone: clear win — same or
  better precision at roughly a quarter of the calls.
- Against a standard quota dialer that stops at the first N stated yeses:
  genuine tradeoff — mobilize trades ~7-9 more calls and a lower raw fill
  rate under a tight budget for ~8 points of confirmed-roster precision.
  Whether that trade is worth it depends on whether the coordinator is
  penalized for no-shows or only for missing headcount — not stated here as
  a universal win.
- Against a sequential one-at-a-time caller: not a precision or call-count
  argument (sequential is at least as good on both in the matched trials) —
  the case is structural throughput under a hard deadline and a pool larger
  than one slot, where concurrent wave dispatch needs a small, roughly
  constant number of round-trips instead of one round-trip per contact.
- No fabricated competitor benchmark was used anywhere above; every number
  traces to `benchmark_audit_b0.md`, `matched_policy_experiment_b1.md`, or
  the named external plan-file citations.
