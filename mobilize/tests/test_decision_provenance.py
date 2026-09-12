"""A0/A1: proves each refusal/confirmation branch in _to_call_result records
the correct DecisionReason/ContactOutcome code (not just the correct
CallOutcome), that precedence between overlapping conditions matches the
existing branch order, and that old ledger entries -- written before these
fields existed -- replay with them as explicitly unknown (None), never
guessed at from the outcome alone. See mobilize/tests/test_outcome_trust_boundary.py
for the underlying outcome-correctness tests this file does not repeat.
"""

from __future__ import annotations

from mobilize.core.dispatcher import _deserialize, _serialize
from mobilize.core.types import CallOutcome, CallResult, ContactOutcome, DecisionReason, utcnow
from mobilize.tests.test_planner import make_candidate
from mobilize.transports.calle import _to_call_result


def _call(status="completed", recipient_status="completed", can_come="yes", task_completed=None,
          transcript_turns=None, evidence="leaving now", final_position="confirmed",
          candidate_id="c0", phone="+15550000000"):
    structured_result = {"can_come": can_come, "evidence_summary": evidence}
    if final_position is not None:
        structured_result["final_position"] = final_position
    return {
        "status": status,
        "task_completed": task_completed,
        "metadata": {"candidate_id": candidate_id},
        "recipients": [{
            "phones": [phone],
            "status": recipient_status,
            "structured_result": structured_result,
            "attempts": [{"transcript_turns": transcript_turns or []}],
        }],
    }


def _candidate():
    return make_candidate("c0")


# --- one positive/negative pair per refusal branch, plus the confirmed path ---

def test_binding_mismatch_reason():
    call = _call(candidate_id="c0", phone="+19998887777")  # phone won't match candidate
    result = _to_call_result("call_1", call, _candidate())
    assert result.decision_reason == DecisionReason.BINDING_MISMATCH.value
    assert result.contact_outcome == ContactOutcome.EXECUTION_FAILURE.value
    assert result.outcome == CallOutcome.FAILED


def test_call_status_failed_reason():
    call = _call(status="failed")
    result = _to_call_result("call_1", call, _candidate())
    assert result.decision_reason == DecisionReason.CALL_OR_RECIPIENT_FAILED.value
    assert result.contact_outcome == ContactOutcome.NO_CONTACT.value


def test_recipient_status_canceled_reason():
    call = _call(status="completed", recipient_status="canceled")
    result = _to_call_result("call_1", call, _candidate())
    assert result.decision_reason == DecisionReason.CALL_OR_RECIPIENT_FAILED.value
    assert result.contact_outcome == ContactOutcome.NO_CONTACT.value


def test_task_incomplete_reason():
    call = _call(can_come="yes", task_completed=False,
                  transcript_turns=[{"speaker": "user", "text": "leaving now"}])
    result = _to_call_result("call_1", call, _candidate())
    assert result.decision_reason == DecisionReason.TASK_INCOMPLETE.value
    assert result.contact_outcome == ContactOutcome.NO_CONTACT.value


def test_recipient_declined_reason():
    call = _call(can_come="no")
    result = _to_call_result("call_1", call, _candidate())
    assert result.decision_reason == DecisionReason.RECIPIENT_DECLINED.value
    assert result.contact_outcome == ContactOutcome.REFUSAL.value
    assert result.outcome == CallOutcome.NO


def test_absent_transcript_reason():
    call = _call(can_come="yes", transcript_turns=[])
    result = _to_call_result("call_1", call, _candidate())
    assert result.decision_reason == DecisionReason.ABSENT_TRANSCRIPT.value
    assert result.contact_outcome == ContactOutcome.MISSING_EVIDENCE.value


def test_task_completion_unconfirmed_reason():
    call = _call(can_come="yes", task_completed=None,
                  transcript_turns=[{"speaker": "user", "text": "leaving now"}])
    result = _to_call_result("call_1", call, _candidate())
    assert result.decision_reason == DecisionReason.TASK_COMPLETION_UNCONFIRMED.value
    assert result.contact_outcome == ContactOutcome.MISSING_EVIDENCE.value


def test_final_position_unclear_reason():
    call = _call(can_come="yes", task_completed=True, final_position="unclear",
                  transcript_turns=[{"speaker": "user", "text": "leaving now"}])
    result = _to_call_result("call_1", call, _candidate())
    assert result.decision_reason == DecisionReason.FINAL_POSITION_UNCLEAR.value
    assert result.contact_outcome == ContactOutcome.MISSING_EVIDENCE.value


def test_missing_recipient_corroboration_reason():
    call = _call(can_come="yes", task_completed=True, final_position="confirmed",
                  evidence="leaving now",
                  transcript_turns=[{"speaker": "user", "text": "Maybe, I am not sure."}])
    result = _to_call_result("call_1", call, _candidate())
    assert result.decision_reason == DecisionReason.MISSING_RECIPIENT_CORROBORATION.value
    assert result.contact_outcome == ContactOutcome.CONFLICTING_EVIDENCE.value


def test_ambiguous_provider_response_reason():
    call = _call(can_come="unknown")
    result = _to_call_result("call_1", call, _candidate())
    assert result.decision_reason == DecisionReason.AMBIGUOUS_PROVIDER_RESPONSE.value
    assert result.contact_outcome == ContactOutcome.MISSING_EVIDENCE.value


def test_confirmed_reason():
    call = _call(can_come="yes", task_completed=True, final_position="confirmed",
                  transcript_turns=[{"speaker": "bot", "text": "can you help?"},
                                     {"speaker": "user", "text": "yes, leaving now"}])
    result = _to_call_result("call_1", call, _candidate())
    assert result.decision_reason == DecisionReason.CONFIRMED.value
    assert result.contact_outcome == ContactOutcome.AGREEMENT.value
    assert result.outcome in (CallOutcome.FIRM_YES, CallOutcome.SOFT_YES)


# --- precedence: when multiple conditions could apply, the earlier branch
# in _to_call_result's existing if/elif chain must win, and the reason must
# match that same branch, not a later one that would also be true. ---

def test_precedence_binding_mismatch_beats_call_status_failed():
    """A mismatched phone AND a failed call status: binding validation runs
    first in _to_call_result, before status is even inspected."""
    call = _call(status="failed", candidate_id="c0", phone="+10000000000")
    result = _to_call_result("call_1", call, _candidate())
    assert result.decision_reason == DecisionReason.BINDING_MISMATCH.value


def test_precedence_call_failed_beats_task_incomplete():
    """A failed call status AND task_completed=False: status is checked
    first in the existing if/elif chain."""
    call = _call(status="failed", task_completed=False)
    result = _to_call_result("call_1", call, _candidate())
    assert result.decision_reason == DecisionReason.CALL_OR_RECIPIENT_FAILED.value


def test_precedence_absent_transcript_beats_task_completion_unconfirmed():
    """No transcript AND task_completed unconfirmed: the transcript check
    comes first inside the can_come=='yes' branch."""
    call = _call(can_come="yes", task_completed=None, transcript_turns=[])
    result = _to_call_result("call_1", call, _candidate())
    assert result.decision_reason == DecisionReason.ABSENT_TRANSCRIPT.value


def test_precedence_final_position_beats_corroboration_check():
    """final_position not confirmed AND the transcript wouldn't corroborate
    either: final_position is checked first inside the can_come=='yes' branch."""
    call = _call(can_come="yes", task_completed=True, final_position="unclear",
                  transcript_turns=[{"speaker": "user", "text": "Maybe, I am not sure."}])
    result = _to_call_result("call_1", call, _candidate())
    assert result.decision_reason == DecisionReason.FINAL_POSITION_UNCLEAR.value


# --- old-ledger replay: a serialized CallResult from before decision_reason/
# contact_outcome existed must deserialize with both as None, not crash and
# not guess a value from the outcome alone. ---

def test_old_ledger_entry_without_provenance_fields_deserializes_as_unknown():
    old_payload = {
        "call_id": "call_old",
        "candidate_id": "c0",
        "outcome": "firm_yes",
        "commitment_score": 0.9,
        "stated_yes": True,
        "evidence": "leaving now",
        # no stop_requested, decision_reason, or contact_outcome keys --
        # exactly what a ledger entry written before this feature existed
        # would contain.
    }
    result = _deserialize(old_payload)
    assert result.decision_reason is None
    assert result.contact_outcome is None
    assert result.stop_requested is False
    assert result.outcome == CallOutcome.FIRM_YES


def test_serialize_then_deserialize_round_trips_provenance_fields():
    result = CallResult(
        call_id="call_1", candidate_id="c0", outcome=CallOutcome.NO,
        commitment_score=0.0, stated_yes=False, evidence="",
        decision_reason=DecisionReason.RECIPIENT_DECLINED.value,
        contact_outcome=ContactOutcome.REFUSAL.value,
        completed_at=utcnow(),
    )
    round_tripped = _deserialize(_serialize(result))
    assert round_tripped.decision_reason == DecisionReason.RECIPIENT_DECLINED.value
    assert round_tripped.contact_outcome == ContactOutcome.REFUSAL.value
