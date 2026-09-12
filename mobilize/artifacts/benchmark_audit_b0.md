# B0 — Benchmark audit and measurement dictionary

## What was wrong

README.md, `mobilize/artifacts/devpost_submission.md`, and
`mobilize/artifacts/demo_video_script.md` all claimed **300 trials** and
**94.6% / 87.7% / 80.2%** confirmation accuracy for calibrated / naive /
call-everyone. The harness (`mobilize/sim/harness.py:175`) is hardcoded to
`n_trials=200` with no CLI flag — it has never run 300 trials. The
percentages were also stale: re-running the actual 200-trial harness
produces 94.0% / 86.3% / 80.0%, not 94.6% / 87.7% / 80.2%. Both errors are
now corrected everywhere those numbers appeared as claims (not in
`PLAN_REVIEW.md` / `EXECUTION_PLAN.md`, which reference the bug itself as
history and should keep the old wrong numbers as evidence of what was
fixed).

## Reproduction

- Command: `.venv/bin/python -m mobilize.sim.harness`
- Environment: `.venv/bin/python` = Python 3.14.6
- Commit: `35ff46782dd785a5bdeb4e6fbfeb04b86ca39dff`
- Trial count: 200 (`mobilize/sim/harness.py:175`, `n_trials=200`, not
  configurable via CLI)
- Seed scheme: `seed = 1000 + trial` for `trial in range(200)` (seeds
  1000–1199), used both to generate the synthetic donor population
  (`generate_population`) and to drive `SimulatedTransport` for the
  calibrated run. The naive baselines (`stated_yes_only`, `call_all`) reuse
  the same seed to build their own `random.Random(seed)` call-outcome
  stream — same seed, but a materially different RNG draw sequence than the
  calibrated policy's `SimulatedTransport`, since `SimulatedTransport` also
  consumes draws for simulated latency. Same seed does not mean same
  per-candidate outcome across policies; it only makes each policy's run
  independently reproducible on its own.
- Run twice (once via a backgrounded shell, once via direct foreground run)
  on the same commit — outputs were byte-identical, confirming determinism.

## Verbatim output

**Updated 12 September 2026**: a real validation call (`mobilize/artifacts/real_call_validation/call_1_raw.json`)
exposed a genuine commitment-scoring gap — a concrete numeric ETA ("I'll be
there in 10 minutes") matched no `FIRM_MARKERS` pattern and scored neutral
(0.5), just under the confirmation threshold, despite CALL-E's own extraction
confidently saying `final_position=confirmed`. Fixed in `mobilize/core/commitment.py`
by adding an ETA-pattern firm marker. Full suite still green (310/3/2); harness
re-run below reflects the fix. `confirmation_accuracy` moved 0.940 → 0.937 and
`mean_calls_used` moved 11.37 → 10.62 as a direct, explainable consequence
(more real firm commitments are now recognized as such, needing fewer calls,
with a small precision tradeoff) — not an unexplained drift. See
`mobilize/artifacts/validation_results.md` for the full real-call writeup.

```json
{
  "calibrated": {
    "fill_rate": 0.995,
    "confirmation_accuracy": 0.937,
    "mean_calls_used": 10.62,
    "mean_over_recruitment_ratio": 3.54,
    "n_trials": 200
  },
  "stated_yes_only": {
    "fill_rate": 1.0,
    "confirmation_accuracy": 0.863,
    "mean_calls_used": 7.04,
    "mean_over_recruitment_ratio": 2.35,
    "n_trials": 200
  },
  "call_all": {
    "fill_rate": 1.0,
    "confirmation_accuracy": 0.8,
    "mean_calls_used": 40,
    "mean_over_recruitment_ratio": 13.33,
    "n_trials": 200
  }
}
```

## Measurement dictionary

- **`fill_rate`** — fraction of trials (out of 200) where the policy hit
  `need.count` confirmed donors within `max_calls`. Not accuracy; a policy
  can fill 100% of the time while filling with people who never show.

- **`confirmation_accuracy`** — for each trial, `true_showups /
  confirmed_count` (ground-truth show-ups among the donors the policy
  believed were confirmed), then **averaged across trials** (macro
  average), not pooled across all confirmed donors in all trials. Trials
  with `confirmed_count == 0` are **excluded** from the average entirely
  (`mobilize/sim/harness.py:157`) rather than counted as 0% or skewing the
  denominator — with 200 trials and a 99.5%/100%/100% fill rate here, this
  exclusion affects at most a handful of calibrated-policy trials and none
  of the naive baselines.

- **`mean_calls_used`** — mean, across trials, of calls actually placed
  (dispatched) before the policy stopped.

- **`over_recruitment_ratio`** / **`mean_over_recruitment_ratio`** — despite
  the name, this is **calls placed per person needed**
  (`calls_used / need.count`), not "excess people recruited beyond the
  need." For `need.count = 3`, a calibrated ratio of 3.79 means ~11.4 calls
  were placed per 3-person need — it says nothing about how many people
  beyond 3 actually got confirmed or showed up. The docs above now describe
  it this way; renaming the field itself is out of scope for B0 (touches
  `RunOutcome` and call sites in `mobilize/sim/harness.py`, owner B territory
  but a behavior-adjacent rename, not a doc fix — flagged as a follow-up,
  not implemented here).

- **`n_trials`** — trial count actually run per policy (200), echoed back
  in the summary so a consumer of the JSON doesn't have to trust the docs.

- **Confirmed vs. actual arrivals vs. contacts** — `calls_used` is contacts
  attempted; `confirmed_count` is what the policy *believes* is confirmed
  (stated yes, or calibrated commitment above threshold); `true_showups` is
  the simulator's hidden ground truth for how many of those confirmed
  donors would actually arrive. `confirmation_accuracy` is `true_showups /
  confirmed_count`, not `true_showups / calls_used`.

## Limitations (visible beside the numbers, not buried)

- These are simulated results against a synthetic population with a known,
  hidden show-up probability (`mobilize/sim/population.py`). They validate
  the calibration *policy's* logic against a scoring oracle — they are not
  a claim about real human behavior beyond what the two real-call
  smoketests in `mobilize/artifacts/` separately corroborate.
- `SimulatedTransport`'s elapsed/latency timings are synthetic draws, not
  measured real-world calling durations.
- No headline number here is cherry-picked as "the largest percentage" —
  all three policies' full metric rows are reported together in every
  place these numbers appear.

## Suggestion (not implemented)

A `--trials` CLI flag on `mobilize/sim/harness.py` (owner B, `sim/**`)
would make future re-audits and different sample-size sensitivity checks
easier without touching the default reproducibility path. Not added here
because it's a scope decision beyond a pure documentation fix, and
`n_trials=200` as the hardcoded default is preserved exactly as-is.
