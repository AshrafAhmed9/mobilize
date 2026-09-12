# D4 — two reproducible runs through the same engine

Both runs go through `core/dispatcher.mobilize()` unchanged, against the
free simulator (`_RegistryBackedSimulatedTransport`, the same class the
dashboard's rehearsal mode uses) -- no CALL-E credits spent, fully
reproducible locally.

## Run it

```
.venv/bin/python -m mobilize.artifacts.d4_sample_runs
```

Source: `mobilize/artifacts/d4_sample_runs.py`.

## What it proves

**Donor scenario** (`mobilize/app/sample_data/sample_registry.csv`,
`Registry.candidates(RegistryDomain.DONOR)`): 15 donors, all eligible under
the recency-fallback rule (none has a coordinator-set `donor_eligible` flag
in this sample, so each falls back to the 56-day heuristic and is labeled
`system_default_unreviewed`, not a clinical decision). Need for 3
confirmations fills.

**Shift scenario** (`mobilize/app/sample_data/sample_shift_registry.csv`,
`Registry.candidates(RegistryDomain.SHIFT, required_skill="RN",
shift_start=..., shift_end=...)`): 6 shift candidates, only 2 (`s001`,
`s003`) carry the RN skill and an availability window overlapping the
shift. The other 4 are explicitly flagged ineligible with a reason (missing
skill, or missing/non-overlapping availability), never silently dispatched
and never silently counted as eligible. `Need(count=5, ...)` deliberately
exceeds the 2 qualified candidates -- a shortage case: `filled=False`,
`confirmed=0`, and the ineligible candidates are visible in the output
rather than hidden.

Sample output (captured 2026-09-12, `.venv/bin/python -m
mobilize.artifacts.d4_sample_runs`):

```
=== DONOR ===
registry size: 15 | eligible: 15 | ineligible: 0
filled: True | confirmed: 3 | calls_used: 5
stop_reason: target_met

=== SHIFT (shortage case) ===
registry size: 6 | eligible: 2 | ineligible: 4
  ineligible: EMT Rahul Verma -- Missing required skill: 'RN'.
  ineligible: Tech Suresh Nair -- Missing required skill: 'RN'.
  ineligible: EMT Priyal Shah -- Missing required skill: 'RN'.
  ineligible: Nurse Manoj Pillai -- Missing required skill: 'RN'.
filled: False | confirmed: 0 | calls_used: 2
stop_reason: None
```

The confirmed-people counts in the donor run are stochastic (the simulated
transport uses an unseeded RNG per run, same as the dashboard's rehearsal
mode) -- re-running will fill with a plausible but not byte-identical
count; what's reproducible and asserted by test is the *eligibility split*
(which candidates are eligible/ineligible and why), not the exact call
outcomes.

## Known pre-existing dispatcher quirk (not a D4 regression)

In the shift shortage run, `stop_reason` comes back `None` instead of the
expected `NO_ELIGIBLE_CANDIDATES` once both eligible candidates are
exhausted. This is `core/dispatcher.py`'s own stop-reason precedence logic
(`mobilize/core/dispatcher.py` ~line 512-524), which D4 is not scoped to
touch (dispatcher.py is out of Lane D's file scope). It does not affect
`filled`/`confirmed`/eligibility correctness, only the recorded reason
code, and existed independent of any D4 change. Flagging for whichever
lane owns dispatcher.py.

## Domain isolation regression test

`mobilize/tests/test_d4_domain_isolation.py` proves, directly against
`Registry`/`Person` (not just via these sample runs):

- `test_shift_candidates_not_excluded_by_donor_recency_rule`
- `test_donor_candidates_not_filtered_by_shift_skill_rule`
- `test_shift_domain_filters_incompatible_shift_candidates`
- `test_missing_availability_fails_closed_for_shift`
- `test_donor_coordinator_flag_overrides_recency_both_ways`
- `test_donor_eligibility_falls_back_to_recency_when_unreviewed_and_says_so`

Run: `.venv/bin/python -m pytest mobilize/tests/test_d4_domain_isolation.py -q`
