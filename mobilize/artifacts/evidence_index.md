# Evidence index

A judge-readable map from each claim to a checkable file, test, or explicit
"pending" label. Nothing here is asserted without a path or a passing test
next to it. Working tree has uncommitted changes as of this writing; commit
below is the last committed revision, not necessarily what you're reading.

## 1. Real call -> normalized decision -> dispatch trace -> roster

**Pending.** No real calls have been placed for this submission as of this
writing (12 September 2026); that's outstanding work owned by Ashraf, not
completed evidence.

Two earlier smoke-test calls exist from day-1 development and are cited
honestly, with their limitations:

- `mobilize/artifacts/smoketest_1_result.json` — one real call, `firm_yes`,
  commitment score 0.71, full transcript included. Its own provenance note
  flags an unresolved gap: the recorded `call_id` is CALL-E's call-task ID,
  not a billing-record ID, and can't be reconciled against the provider's
  billing dashboard because the raw `recipients[].attempts[]` array was
  never captured.
- `mobilize/artifacts/smoketest_2_result.json` — one real call through the
  full `mobilize()` pipeline (dispatcher + ledger + real transport),
  `soft_yes` at 0.54, correctly refused (below the 0.55 confirmation
  threshold). Its provenance note documents that no transcript, call_id, or
  raw status was saved for this call, and corrects an earlier false claim in
  `devpost_submission.md` that the transcript "is committed in the repo."

See `mobilize/artifacts/devpost_submission.md` (search "Correction (12
September 2026 review)") for the full, corrected account.

## 2. Matched policy report, raw trial data, assumptions, reproduction

- Report: `mobilize/artifacts/matched_policy_experiment_b1.md`
- Raw output: `mobilize/artifacts/matched_policy_experiment_b1_raw_output.json`
- Measurement audit and corrected numbers: `mobilize/artifacts/benchmark_audit_b0.md`
- Reproduction command: `.venv/bin/python -m mobilize.sim.harness`
- Actual trial count: **200** trials (`mobilize/sim/harness.py:175`,
  `n_trials=200`, hardcoded, not a CLI flag — earlier claims of 300 trials
  were wrong and are corrected in `benchmark_audit_b0.md`).
- Commit tying results to source: `35ff46782dd785a5bdeb4e6fbfeb04b86ca39dff`
  (per `benchmark_audit_b0.md`).

## 3. Recovery demonstration

Test verified to exist and pass (run 12 September 2026):

```
.venv/bin/python -m pytest mobilize/tests/test_crash_safety.py::test_kill_minus_9_mid_dispatch_then_resume_no_duplicates_no_losses -v
```

Result: **1 passed.** The test `SIGKILL`s an actual subprocess mid-dispatch,
restarts it against the same ledger, and asserts zero duplicate dials and
zero lost confirmations.

Other anchors from `EXECUTION_PLAN.md` F2, all verified passing the same
run:

- `mobilize/tests/test_ambiguous_dispatch_reconciliation.py::test_ambiguous_dispatch_halts_further_waves_within_the_same_run`
- `mobilize/tests/test_outcome_trust_boundary.py::test_commitment_score_is_derived_from_recipient_words_not_structured_evidence_summary`
- `mobilize/tests/test_outcome_trust_boundary.py::test_missing_final_position_fails_closed`

All 4 passed in a single run.

## 4. Conversation evaluation and known limitations

Corpus test: `mobilize/tests/test_c1_final_intent_corpus.py`. Run 12
September 2026:

```
.venv/bin/python -m pytest mobilize/tests/test_c1_final_intent_corpus.py -q
```

Result: **43 passed, 3 xfailed, 2 xpassed.** The xfail/xpass cases are
known, documented gaps — not hidden failures.

Rubric and case design: `mobilize/artifacts/c1_annotation_rubric.md`.
Known limitations of the corpus and scoring approach are discussed in
`mobilize/artifacts/devpost_submission.md` and `mobilize/artifacts/calle_contract_c0.md`
(e.g. schema/provenance gaps noted in section 1 above apply to conversation
evidence generally, not just the two smoke-test calls).

## 5. Two domain workflows and practitioner findings

Both domains exist and are exercised by dedicated isolation tests:

- Blood-donor and shift-coverage sample registries:
  `mobilize/app/sample_data/sample_shift_registry.csv` and the donor
  equivalent, loaded via `load_registry_csv`.
- Cross-domain isolation proof: `mobilize/tests/test_d4_domain_isolation.py`
  (e.g. `test_shift_candidates_not_excluded_by_donor_recency_rule`,
  `test_donor_candidates_not_filtered_by_shift_skill_rule`,
  `test_shift_domain_filters_incompatible_shift_candidates`) — proves the
  56-day donor-recency rule and shift-skill rule each apply only in their
  own domain.
- Sample runs: `mobilize/artifacts/d4_sample_runs.md` /
  `mobilize/artifacts/d4_sample_runs.py`.

**Practitioner outreach: pending.** No intended-practitioner task
observation has been collected as of this writing. No practitioner
feedback has been gathered.

## 6. Attendance / calibration evidence

**Pending.** No real-world attendance or calibration data has been
observed yet; the matched-policy numbers in section 2 are simulation-only.
The mechanism to record real observed attendance exists and is tested
(`mobilize/core/registry.py::record_attendance`, `mobilize/tests/test_registry.py`),
but no actual mobilization has produced attendance data to run through it.

## 7. Public rehearsal, source revision, clean-install

- Public rehearsal app: `mobilize/app/public_mode.py` (E2). It only ever
  runs the deterministic fixture backend
  (`mobilize.sim.fixture_transport.run_fixture_scenario`), never imports
  the real CALL-E transport, and never reads any credential — structurally
  incapable of placing a real call regardless of client input.
- Design/guarantees write-up: `mobilize/artifacts/e2_public_mode.md`.
- Exact revision: working tree is **dirty** (uncommitted changes present)
  as of this writing. Last committed revision:
  `35ff46782dd785a5bdeb4e6fbfeb04b86ca39dff` (`git rev-parse HEAD`). Treat
  anything not yet committed — including this file — as provisional until
  committed.
- Clean-install instructions: `./run.sh` — creates the venv on first run,
  reuses it after, no manual activation or pip install required.

## 8. Independent rehearsal (informal)

- `mobilize/artifacts/f6_judge_rehearsal.md`: a friend with no prior
  exposure to the project ran it unguided from the public repo and
  succeeded without help, and reacted positively to the concept. This is
  real signal that setup and the core workflow don't require hand-holding.
- **What it is not:** the full five-task comprehension check
  (`validation_protocol.md` Part E) — whether an unguided user correctly
  distinguishes simulated from real, understands a specific refusal, or
  can reproduce a piece of evidence from source was not specifically
  tested. Labeled accordingly in the artifact; not claimed as a completed
  independent judge rehearsal.

## Full-suite baseline

```
.venv/bin/python -m pytest mobilize/tests/ -q
```

310 passed, 3 xfailed, 2 xpassed, confirmed at the time this index was
written (docs-only change; no source or test files touched).
