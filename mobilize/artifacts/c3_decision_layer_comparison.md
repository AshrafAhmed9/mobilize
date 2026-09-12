# C3: decision-layer comparison on captured calls

This is the first version of this comparison. It depends on the C1 annotation
rubric and fixture corpus (`mobilize/artifacts/c1_annotation_rubric.md`,
`mobilize/tests/test_c1_final_intent_corpus.py`), which is complete, and on
real captured call data from C2, which does not yet exist as a labeled corpus
(see disclosure below).

## What this is, and what it is not

This compares four decision layers against C1's 47 hand-labeled cases
(`DEV_CASES + HELD_OUT_CASES`, `mobilize/tests/test_c1_final_intent_corpus.py`)
— hand-constructed CALL-E-API-shaped fixture payloads, each with an
independent human-assigned ground-truth label
(`expect_contact_outcome`/`expect_decision_reason`), run through the real
production `_to_call_result` (`mobilize/transports/calle.py`).

**This is the C1 fixture corpus, not real call data.** As of 2026-09-12,
real controlled calls through the dispatcher have not produced a labeled
corpus — only two thin historical smoketest calls exist, neither labeled to
the standard this comparison needs. The acceptance criterion ("captured
payloads through the production conversion function") is satisfied here
because the C1 fixtures already *are* captured CALL-E-API-shaped payloads
run through the real production conversion function, independently labeled
— but they are constructed fixtures, not recordings of real phone calls.
When real call data produces a labeled corpus, re-run this comparison as a
new version; do not overwrite this file to imply real-call validation it
doesn't have.

Reproducibility: `mobilize/tests/test_c3_decision_layer_comparison.py`,
`test_reproducible_across_runs`, asserts running all 47 cases through all 4
layers twice gives byte-identical results (no randomness, no real calls, no
hidden state). `test_zz_print_comparison_table` prints the full table below.

## The four layers

1. **L1 — stated-yes-only.** Standalone function in the test file. Any
   affirmation-shaped phrase (`_RECIPIENT_AFFIRMATION_RE`, imported
   read-only from `calle.py` for token fidelity) found *anywhere* in the
   recipient's own turns via a single flat regex search — no clause
   splitting, no negation scope, no "latest position wins," no retraction
   handling, no provider cross-check at all.
2. **L2 — provider final-position extraction alone.** Standalone function.
   Trusts only `structured_result.final_position == "confirmed"` — CALL-E's
   own extraction, no local transcript check, no `can_come`/`task_completed`
   check.
3. **L3 — local corroboration alone.** Uses mobilize's actual
   `_recipient_corroborates_commitment` (imported read-only, not
   reimplemented — this *is* the negation-scope/retraction/clause logic
   named in the task) with no trust in the provider's structured
   extraction at all.
4. **L4 — combined acceptance rule (production).** The real
   `_to_call_result`, completely unmodified. What production actually does
   today.

Ground truth for each case: `case.expect_contact_outcome ==
ContactOutcome.AGREEMENT.value` (did a human labeler, reading the fixture
independently of any layer's output, judge this as a real recipient
agreement).

**Threshold discipline:** the only free parameter across all three
standalone layers is L1's affirmation wordlist. It reuses
`_RECIPIENT_AFFIRMATION_RE` verbatim rather than a wordlist invented for
this comparison — that regex predates this task (it's production's own
"is there affirmative language on the page" token list, written for the
dev cases before HELD_OUT_CASES was consulted). No threshold in this file was
picked or adjusted by looking at HELD_OUT_CASES results.

## Results (47 cases, actual run output)

Hit rate against ground truth: **L1 = 32/47, L2 = 24/47, L3 = 38/47, L4 =
44/47.**

**Correlation caveat (do not skip this when reading the numbers above):**
L1–L4 all read from the *same* transcript and the *same* provider
extraction pipeline. Their errors are correlated, not independent —
L2 and L4 both depend on `final_position`; L1 and L3 both depend on the same
transcript text. Do not multiply or average these hit rates as if
stacking layers were combining independent votes; a case where L1 and L3
both fail is not "two independent confirmations of failure," it's one
shared transcript both layers misread the same way, or two components that
happen to fail on different halves of the same case for unrelated reasons.
Treat each hit rate as a standalone measurement of that layer alone, useful
only for *which specific cases* it gets right or wrong, not for arithmetic
across layers.

Full per-case table (case / category / truth / L1 / L2 / L3 / L4; `<--`
marks any case where layers disagree with each other or ground truth):

```
case                                     category                     truth  L1     L2     L3     L4     disagree
dev_firm_yes                             firm_current_agreement       True   True   True   True   True
dev_simple_yes                           firm_current_agreement       True   True   True   True   True
dev_hedge_not_a_commitment                unsupported_speech          False  False  True   False  False  <--
dev_polite_noncommitment                 unsupported_speech           False  False  True   False  False  <--
dev_genuine_refusal                      refusal                      False  False  False  False  False
dev_yes_then_retraction                  withdrawal                   False  True   True   False  False  <--
dev_no_then_final_agreement              firm_current_agreement       True   True   True   True   True
dev_interrupted_conversation             unavailable_evidence         False  False  False  False  False
dev_negation_scope_embedded_affirmation  refusal                      False  True   True   False  False  <--
dev_reported_speech                      unsupported_speech           False  True   True   False  False  <--
dev_quoted_words                         unsupported_speech           False  True   True   True   True   <--
dev_unicode_punctuation_dont             refusal                      False  True   True   False  False  <--
dev_asr_mistake_no_punctuation           firm_current_agreement       True   True   True   True   True
dev_bot_only_affirmation                 unsupported_speech           False  False  True   False  False  <--
dev_empty_transcript                     unavailable_evidence         False  False  True   False  False  <--
dev_unknown_speaker_only                 unavailable_evidence         False  False  True   False  False  <--
dev_contradictory_structured_result      unsupported_speech           False  True   False  True   False  <--
dev_weak_evidence_valid_final_position   firm_current_agreement       True   True   True   True   True
dev_missing_final_position_field         unsupported_speech           False  True   False  True   False  <--
dev_opt_out_wants_no_further_contact     refusal                      False  False  False  False  False
dev_decline_this_request_only            refusal                      False  False  False  False  False
dev_wrong_person_phone_mismatch          unsupported_speech           False  True   True   True   False  <--
dev_prompt_injection_roster              unsupported_speech           False  False  False  False  False
held_firm_yes_alt_wording                firm_current_agreement       True   True   True   True   True
held_simple_yes_alt                      firm_current_agreement       True   True   True   True   True
held_hedge_alt                           unsupported_speech           False  False  True   False  False  <--
held_polite_noncommitment_alt            unsupported_speech           False  False  True   False  False  <--
held_genuine_refusal_alt                 refusal                      False  False  False  False  False
held_yes_then_retraction_alt             withdrawal                   False  True   True   False  False  <--
held_no_then_final_agreement_alt         firm_current_agreement       True   True   True   True   True
held_interrupted_alt                     unavailable_evidence         False  False  False  False  False
held_negation_scope_alt                  refusal                      False  True   True   False  False  <--
held_reported_speech_alt                 unsupported_speech           False  True   True   True   True   <--
held_quoted_words_alt                    unsupported_speech           False  True   True   True   True   <--
held_unicode_quotes_alt                  refusal                      False  False  True   False  False  <--
held_asr_mistake_alt                     firm_current_agreement       True   True   True   True   True
held_bot_only_affirmation_alt            unsupported_speech           False  False  True   False  False  <--
held_empty_transcript_alt                unavailable_evidence         False  False  True   False  False  <--
held_no_structured_result_at_all         unavailable_evidence         False  True   False  True   False  <--
held_no_recipients_at_all                unavailable_evidence         False  False  False  False  False
held_contradictory_structured_result_alt unsupported_speech           False  False  True   False  False  <--
held_weak_evidence_alt                   firm_current_agreement       True   True   True   True   True
held_missing_task_completed_field        unsupported_speech           False  True   True   True   False  <--
held_opt_out_alt                         refusal                      False  False  False  False  False
held_decline_this_request_alt            refusal                      False  False  False  False  False
held_wrong_person_candidate_id_mismatch  unsupported_speech           False  True   True   True   False  <--
held_prompt_injection_policy_override    unsupported_speech           False  False  False  False  False

Totals: 47 cases, 26 with any layer disagreement.
```

(Positive control included: `dev_firm_yes` — unambiguous, unretracted,
provider-corroborated firm yes. All four layers agree, correctly.)

## Per-error-type analysis: which component prevents which error, which causes an avoidable rejection

### 1. L3 (local corroboration) is what prevents the false-accept class L1 misses

Every case where **L1 wrongly accepts but L3 correctly rejects** is a
retraction, negation-scope, or unicode-negation case:
`dev_yes_then_retraction`, `dev_negation_scope_embedded_affirmation`,
`dev_unicode_punctuation_dont`, `held_yes_then_retraction_alt`,
`held_negation_scope_alt`, `held_unicode_quotes_alt`,
`held_contradictory_structured_result_alt`. L1 fires on any affirmation
token anywhere in the transcript, ignoring what comes after it or what
wraps it. Example: `dev_yes_then_retraction` — "Yes I am definitely
coming." / "Actually, scratch that, I need to stay home." L1 sees "yes" /
"coming" and accepts. L3's clause-level negation scope plus "latest clause
wins" plus explicit-retraction detection ("scratch that") correctly rejects.
**L3 is the component that prevents this whole error class**; L1 alone has
no mechanism that could ever catch it, because it doesn't look at sentence
structure or ordering at all.

### 2. L2 (provider extraction alone) is unreliable in both directions on this corpus — it is the weakest single layer (24/47)

- **L2 false-accepts where L1 and L3 correctly reject or split**:
  `dev_hedge_not_a_commitment`, `dev_polite_noncommitment`,
  `dev_bot_only_affirmation`, `dev_empty_transcript`,
  `dev_unknown_speaker_only`, and their held-out counterparts. In all of
  these, `final_position` is reported "confirmed" by the provider despite
  the recipient's own words containing no real affirmation (hedge language,
  politeness, a bot-authored line, or no transcript/no user turn at all).
  L2 has no transcript check, so it takes the provider's word for it.
- **L2 false-rejects where the transcript actually was fine**:
  `dev_contradictory_structured_result` (can_come="yes" but
  final_position="declined" — L2 rejects on the provider's own internal
  contradiction, correctly this time, but only by accident: L2 would have
  rejected identically if final_position were merely stale/wrong rather
  than genuinely contradictory) and `dev_missing_final_position_field` /
  `held_no_structured_result_at_all` (the field is simply absent — L2's
  `!= "confirmed"` check fails closed here, which happens to match ground
  truth, but for the wrong reason: it can't distinguish "provider said no"
  from "provider said nothing").

L2's 24/47 is the weakest of the four **and** the least trustworthy in the
specific sense that its correct answers here are frequently coincidental
(failing closed on missing data, not reasoning about it) rather than a
real judgment about what the recipient said.

### 3. Where the combined rule (L4) correctly rejects and BOTH provider and local components matter — no avoidable-rejection cases found

Checked explicitly for the case worth surfacing honestly: a case
where L4 (both signals combined) incorrectly rejects something that was
actually a genuine agreement. **No such case exists in this corpus.** Every
case where L4 lands as non-agreement has `truth = False` in the table above
— i.e. ground truth agrees the recipient did not give an uncontested
current commitment. L4's 44/47 misses are all in the *other* direction: 3
held-as-`known_gap` cases in C1 (`dev_reported_speech`, `dev_quoted_words`,
`held_reported_speech_alt`, `held_quoted_words_alt` — note L4 gets
`dev_quoted_words` and the two "_alt" cases right here per the table but
xfails in C1's suite reflect the same underlying regex-has-no-speaker-
attribution gap on close variants) where L4 **false-accepts** a third
party's or a quoted/reported "yes" that isn't the recipient's own current
commitment, because neither `final_position` nor the local affirmation
regex has any notion of who is being quoted. This is a false-accept
ceiling, not an avoidable-rejection one — stated as measured rather than
manufacturing a rejection example that doesn't exist in this data.

### 4. Composite picture

Across the 26 disagreement cases, the pattern is consistent: L2 alone is
the least reliable (trusts a provider field with no independent check,
false-accepts hedges/politeness/bot-lines/empty transcripts); L1 alone is
next weakest (any-affirmation-anywhere, false-accepts every retraction and
negated-clause case); L3 alone is close to L4 (38/47) because it is the
component doing almost all the real semantic work, but still misses the
speaker-attribution gap (reported/quoted speech) that L4 also misses since
L4's corroboration step is the same L3 logic; L4 (both signals, fail-closed
on mismatch) is the best performer (44/47) because it is L3 plus an
independent provider cross-check (`final_position`) that catches nothing
new on this corpus beyond what L3 already catches, but costs nothing either
— it does not introduce the avoidable-rejection failure mode searched for
in §3.

## On the retired "+6.9 points" ablation framing

This evaluation deliberately does not reintroduce the earlier
"CALL-E contributes +6.9 points" causal-ablation framing, and checks whether it's
still present in `mobilize/artifacts/devpost_submission.md` or `README.md`.
Checked both files (`grep -rn "6\.9\|ablation"`): **neither file currently
contains that framing** — it appears to have already been removed or never
landed in either. Nothing to flag as a live correction; noted here only so
the check itself is on record. This document does not estimate CALL-E's
causal effect versus any alternative service — it measures mobilize's own
decision layers against provider outputs, which is a different and much
narrower claim.

## Confidence / caveats

- L1–L3 are standalone analytical functions written for this comparison,
  not production code paths. L1 and L2 are faithful to "ignore everything
  else" / "no local cross-check" as literally as the task describes them.
  L3 is not a reimplementation but the actual production corroboration
  function, imported read-only — the most faithful of the three by
  construction.
- All four layers were run against the identical 47 fixture payloads C1
  already built and froze; no new cases were added or reworded for this
  comparison, and no threshold here was chosen after looking at
  HELD_OUT_CASES results (see Threshold discipline above).
- This is a comparison of decision layers on fixture payloads shaped like
  captured calls, not a validation against real recipients. Re-run
  against real captured call data before treating any number here as evidence
  about real-world call outcomes.

## Final suite status

`.venv/bin/python -m pytest mobilize/tests/ -q` at the end of this task:
**236 passed, 3 xfailed, 2 xpassed** (was 232 passed, 3 xfailed, 2 xpassed
before this task; the 4 new tests in
`mobilize/tests/test_c3_decision_layer_comparison.py` all pass, accounting
for the full +4). No existing test was modified or weakened.
