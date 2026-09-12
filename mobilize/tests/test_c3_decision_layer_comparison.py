"""C3: decision-layer comparison on C1's captured fixture corpus.

Version tag: C3-v1.

IMPORTANT SCOPE DISCLOSURE: this evaluation runs against C1's 47 hand-labeled
fixture cases (`DEV_CASES + HELD_OUT_CASES` in
`mobilize/tests/test_c1_final_intent_corpus.py`) -- hand-constructed
CALL-E-API-shaped payloads run through the real production `_to_call_result`,
cross-checked against independent human intent labels. It is NOT validated
against real phone calls. C2 (real-call capture) has not produced a labeled
corpus as of this run (2026-09-12) -- only two thin historical smoketest
calls exist, with no comparable ground-truth labeling. If/when C2 produces
real captured-call data, this evaluation must be re-run as a new version
(e.g. C3-v2) against that corpus; this file/version must not be silently
overwritten to claim real-call validation it does not have.

Four decision layers, all evaluated against the SAME 47 ground-truth labels
(`case.expect_contact_outcome == ContactOutcome.AGREEMENT.value`, C1's
`expect_contact_outcome`/`expect_decision_reason` fields):

  L1 stated-yes-only       -- standalone, below. Any affirmation-shaped
                               phrase anywhere in the recipient's own turns,
                               with NO negation scope, NO clause splitting,
                               NO "latest position wins", NO retraction
                               handling, NO provider cross-check. Reuses only
                               the raw affirmation-token regex
                               (`_RECIPIENT_AFFIRMATION_RE`, read-only import
                               from calle.py for token fidelity) applied with
                               a single flat `re.search` over concatenated
                               user speech -- deliberately the naive
                               baseline this task asks for.
  L2 provider-extraction-only -- standalone, below. Trusts CALL-E's own
                               `final_position == "confirmed"` field alone,
                               no local transcript cross-check at all.
  L3 local-corroboration-only -- standalone, below. Trusts ONLY mobilize's
                               own transcript corroboration logic
                               (`_recipient_corroborates_commitment`,
                               read-only import -- this IS "mobilize's own
                               transcript-scanning/negation-scope/retraction
                               logic" named in the task, not a reimplementation
                               of it), ignoring can_come/final_position/
                               task_completed entirely.
  L4 combined (production)  -- the real `_to_call_result`, unmodified. Accept
                               iff contact_outcome == "agreement".

Guardrail: this file imports read-only helpers from mobilize/transports/calle.py
(`_RECIPIENT_AFFIRMATION_RE`, `_recipient_corroborates_commitment`,
`_to_call_result`) but calle.py itself is NOT modified by this task, and none
of L1/L2/L3 are new production code paths -- they are analytical baselines
for comparison only, never called from dispatch/policy/commitment.

Threshold discipline: L1's only free choice is which regex counts as
"sounds like yes." That's `_RECIPIENT_AFFIRMATION_RE`, picked before looking
at HELD_OUT_CASES results, for one reason: it is the same token list
production already uses to decide there's affirmative language on the page.
Reusing it (rather than inventing a separate "L1 wordlist") means L1's
result is not shaped by a threshold chosen to make L1 look good or bad on
held-out cases -- it's fixed by DEV_CASES-era production code, not tuned
here at all. No other threshold is introduced anywhere in this file.
"""

from __future__ import annotations

import re

from mobilize.core.types import ContactOutcome
from mobilize.transports.calle import (
    _RECIPIENT_AFFIRMATION_RE,
    _recipient_corroborates_commitment,
    _to_call_result,
)
from mobilize.tests.test_c1_final_intent_corpus import ALL_CASES, DEV_CASES, Case


def layer1_stated_yes_only(case: Case) -> bool:
    """Naive baseline: any affirmation-shaped phrase anywhere in the
    recipient's own (speaker == "user") turns, no structure at all."""
    recipient = (case.call.get("recipients") or [{}])[0]
    attempts = recipient.get("attempts") or []
    text = " ".join(
        turn.get("text", "")
        for attempt in attempts
        for turn in (attempt.get("transcript_turns") or [])
        if turn.get("speaker") == "user"
    )
    return bool(_RECIPIENT_AFFIRMATION_RE.search(text))


def layer2_provider_extraction_only(case: Case) -> bool:
    """Trust CALL-E's own final_position field alone -- no transcript
    cross-check, no can_come check, no task_completed check."""
    recipient = (case.call.get("recipients") or [{}])[0]
    structured = recipient.get("structured_result") or {}
    return structured.get("final_position", "unclear") == "confirmed"


def layer3_local_corroboration_only(case: Case) -> bool:
    """Trust ONLY mobilize's own transcript corroboration logic -- ignores
    can_come/final_position/task_completed/call status entirely."""
    recipient = (case.call.get("recipients") or [{}])[0]
    attempts = recipient.get("attempts") or []
    transcript: list[dict] = []
    for attempt in attempts:
        transcript.extend(attempt.get("transcript_turns") or [])
    return _recipient_corroborates_commitment(transcript)


def layer4_combined_production(case: Case) -> bool:
    """The real production decision: _to_call_result, unmodified."""
    result = _to_call_result(f"c3_{case.name}", case.call, case.candidate)
    return result.contact_outcome == ContactOutcome.AGREEMENT.value


def ground_truth(case: Case) -> bool:
    return case.expect_contact_outcome == ContactOutcome.AGREEMENT.value


def run_all_layers():
    """Returns a list of per-case dicts: name, ground_truth, l1..l4."""
    rows = []
    for case in ALL_CASES:
        rows.append({
            "name": case.name,
            "set": "dev" if case in DEV_CASES else "held_out",
            "category": case.category,
            "truth": ground_truth(case),
            "l1": layer1_stated_yes_only(case),
            "l2": layer2_provider_extraction_only(case),
            "l3": layer3_local_corroboration_only(case),
            "l4": layer4_combined_production(case),
        })
    return rows


def test_reproducible_across_runs():
    """Same 47 fixture payloads through the same four layers must produce
    byte-identical per-case verdicts on repeated runs -- no hidden state,
    no randomness, no real calls involved."""
    first = run_all_layers()
    second = run_all_layers()
    assert first == second


def test_positive_control_all_layers_agree():
    """dev_firm_yes: unambiguous, unretracted, provider-corroborated firm
    yes ('Yes, absolutely, I'll definitely be there.'). Every layer,
    including the naive stated-yes-only baseline, must accept it -- if even
    this disagrees, something is broken in the harness, not a genuine
    decision-layer finding."""
    case = next(c for c in ALL_CASES if c.name == "dev_firm_yes")
    assert ground_truth(case) is True
    assert layer1_stated_yes_only(case) is True
    assert layer2_provider_extraction_only(case) is True
    assert layer3_local_corroboration_only(case) is True
    assert layer4_combined_production(case) is True


def test_layer4_matches_production_agreement_rate():
    """Sanity check: L4 in this file must exactly match the C1 corpus's own
    pass/fail semantics for contact_outcome -- i.e. L4 truly is production,
    not a drifted copy."""
    for case in ALL_CASES:
        result = _to_call_result(f"c3_check_{case.name}", case.call, case.candidate)
        assert (result.contact_outcome == ContactOutcome.AGREEMENT.value) == layer4_combined_production(case)


def test_zz_print_comparison_table(capsys):
    """Not an assertion beyond internal consistency -- prints the full
    4-layer x 47-case comparison table plus disagreement summary, which
    mobilize/artifacts/c3_decision_layer_comparison.md is written from.
    Named test_zz_ to run last."""
    rows = run_all_layers()
    print("\n\nC3-v1 decision-layer comparison (C1 fixture corpus, 47 cases):")
    print(f"{'case':40s} {'category':28s} {'truth':6s} {'L1':6s} {'L2':6s} {'L3':6s} {'L4':6s} disagree")
    disagreements = 0
    for r in rows:
        vals = [r["truth"], r["l1"], r["l2"], r["l3"], r["l4"]]
        disagree = len(set(vals)) > 1
        if disagree:
            disagreements += 1
        print(f"{r['name']:40s} {r['category']:28s} "
              f"{str(r['truth']):6s} {str(r['l1']):6s} {str(r['l2']):6s} {str(r['l3']):6s} {str(r['l4']):6s} "
              f"{'<--' if disagree else ''}")

    def hits(key):
        return sum(1 for r in rows if r[key] == r["truth"])

    n = len(rows)
    print(f"\nTotals: {n} cases, {disagreements} with any layer disagreement.")
    print(f"Agreement with ground truth (NOT independent votes -- see correlation caveat in the .md): "
          f"L1={hits('l1')}/{n}  L2={hits('l2')}/{n}  L3={hits('l3')}/{n}  L4={hits('l4')}/{n}")
    print("Correlation caveat: L1/L2/L3/L4 share the same underlying transcript and the same "
          "provider extraction pipeline -- their errors are correlated, not independent votes. "
          "Do not multiply these hit rates together as if stacking layers multiplies reliability.")
