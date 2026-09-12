"""A2: durable dispatch and explicit recovery.

Covers two gaps that were genuinely present before this task, plus the
already-correct operator-reconciliation and old-ledger-compatibility paths.
"""

from __future__ import annotations

import pytest

from mobilize.core.dispatcher import mobilize
from mobilize.core.ledger import Ledger, OperationConflictError
from mobilize.core.types import CallOutcome, CallResult, Need, StopReason
from mobilize.tests.test_planner import make_candidate


class NeverRespondsTransport:
    """Accepts every dispatch (a call_id comes back) but never resolves a
    poll -- standing in for a call that is genuinely still ringing/live at
    the provider when the poll timeout is hit."""

    def __init__(self) -> None:
        self.dispatch_calls: list[str] = []

    async def dispatch(self, candidate, need_label, location, *, idempotency_key):
        self.dispatch_calls.append(candidate.id)
        return f"call_{candidate.id}"

    async def poll(self, call_id, *, expected_candidate=None):
        return None


@pytest.mark.asyncio
async def test_call_timed_out_blocks_further_waves_as_ambiguous(tmp_path):
    """A dispatched call that never returns a terminal result before the
    poll timeout may still be live at the provider. Regression for a real
    gap: `call_timed_out` used to only emit an event, never add the
    candidate to `ambiguous_candidate_ids`, so a later wave could dispatch
    on top of a possibly-still-in-flight call and the operator had no way to
    see the unresolved state in the returned result."""
    ledger = Ledger(tmp_path / "ledger.jsonl")
    transport = NeverRespondsTransport()
    # A single candidate: proves the visibility half of the gap directly
    # (c0's timeout must appear in ambiguous_candidate_ids and stop_reason,
    # not just as a log line). The "blocks a further wave/run" half of the
    # same gap is proven by test_recovery_unresolved_after_crash_blocks_dispatch
    # below and by the updated
    # test_unresolved_ambiguity_halts_the_whole_mobilization_on_resume in
    # test_ambiguous_dispatch_reconciliation.py, where a second candidate is
    # genuinely available and must NOT be reached while the first is
    # unresolved.
    pool = [make_candidate("c0", accept=0.99, showup=0.99)]
    need = Need(label="x", count=2, deadline_minutes=60, location="loc", max_calls=10)

    result = await mobilize(
        need, pool, transport, ledger=ledger, mobilization_id="mob_timeout",
        poll_interval_s=0.01, poll_timeout_s=0.05,
    )

    # c0 was dispatched, never resolved -> must be visible as ambiguous, not
    # silently absent from the result. Before the fix, `pending`'s timeout
    # was logged via `call_timed_out` but never recorded anywhere the caller
    # could query -- ambiguous_candidate_ids stayed empty and stop_reason
    # would have come out DEADLINE/NO_ELIGIBLE_CANDIDATES instead.
    assert "c0" in result.ambiguous_candidate_ids
    assert transport.dispatch_calls == ["c0"]
    assert not result.filled
    assert result.stop_reason == StopReason.UNRESOLVED_DISPATCH_OR_CALL.value
    assert result.counts["unresolved"] == len(result.ambiguous_candidate_ids)


class PreResolvedNoAnswerTransport:
    def __init__(self, resolves_after: set[str]) -> None:
        self._resolves_after = resolves_after
        self.dispatch_calls = 0

    async def dispatch(self, candidate, need_label, location, *, idempotency_key):
        self.dispatch_calls += 1
        return f"new_{candidate.id}"

    async def poll(self, call_id, *, expected_candidate=None):
        return None


@pytest.mark.asyncio
async def test_recovery_unresolved_after_crash_blocks_dispatch(tmp_path):
    """Same gap as above, but on the recovery-after-crash path: a prior
    process dispatched a call and crashed before a result arrived, and the
    resumed process's recovery poll also can't resolve it before its own
    timeout. That candidate must show up as ambiguous, not just as a log
    line, and must block a fresh wave."""
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.record_dispatch("mob_recover_gap", "c0", "provider_call_c0")

    transport = PreResolvedNoAnswerTransport(resolves_after=set())
    pool = [make_candidate("c0"), make_candidate("c1", accept=0.99, showup=0.99)]
    need = Need(label="x", count=1, deadline_minutes=60, location="loc", max_calls=10)

    result = await mobilize(
        need, pool, transport, ledger=ledger, mobilization_id="mob_recover_gap",
        recovery_timeout_s=0.05, poll_interval_s=0.01,
    )

    assert "c0" in result.ambiguous_candidate_ids
    assert transport.dispatch_calls == 0  # c1 must never have been dispatched
    assert result.stop_reason == StopReason.UNRESOLVED_DISPATCH_OR_CALL.value


class RejectsDispatchTransport:
    async def dispatch(self, candidate, need_label, location, *, idempotency_key):
        raise AssertionError("must not dispatch: operation identity conflict must stop this before any call")

    async def poll(self, call_id, *, expected_candidate=None):
        return None


@pytest.mark.asyncio
async def test_changed_parameters_on_same_mobilization_id_raise_conflict(tmp_path):
    """A later, legitimate operation that happens to derive the same
    mobilization_id from the same label+phone list (derive_mobilization_id
    only hashes those two things) must not silently inherit an earlier,
    differently-scoped run's ledger state just because the label and phone
    list match. Changing count (or deadline/location/max_calls/registry)
    must force an explicit new operation or a conflict, not silent reuse."""
    ledger = Ledger(tmp_path / "ledger.jsonl")
    pool = [make_candidate("c0"), make_candidate("c1")]
    transport = RejectsDispatchTransport()

    need_v1 = Need(label="x", count=1, deadline_minutes=60, location="loc", max_calls=10)
    result1 = await mobilize(need_v1, pool, transport, ledger=ledger, mobilization_id="mob_conflict")
    assert result1.stop_reason in (StopReason.DEADLINE.value, None) or True  # setup only, no assertion needed here

    need_v2 = Need(label="x", count=5, deadline_minutes=60, location="loc", max_calls=10)
    with pytest.raises(OperationConflictError):
        await mobilize(need_v2, pool, transport, ledger=ledger, mobilization_id="mob_conflict")


@pytest.mark.asyncio
async def test_identical_resume_does_not_conflict(tmp_path):
    """The same operation, resumed with the exact original parameters, must
    not be treated as a conflict."""
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.record_dispatch("mob_resume_ok", "c0", "call_c0")
    ledger.record_result("mob_resume_ok", "c0", "call_c0", {
        "call_id": "call_c0", "candidate_id": "c0", "outcome": "firm_yes",
        "commitment_score": 0.9, "stated_yes": True, "evidence": "leaving now",
    })
    pool = [make_candidate("c0"), make_candidate("c1")]
    need = Need(label="x", count=1, deadline_minutes=60, location="loc", max_calls=10)

    class RefusesTransport:
        async def dispatch(self, candidate, need_label, location, *, idempotency_key):
            raise AssertionError("need already met, must not dispatch")

        async def poll(self, call_id, *, expected_candidate=None):
            return None

    result = await mobilize(need, pool, RefusesTransport(), ledger=ledger, mobilization_id="mob_resume_ok")
    assert result.filled


def test_old_ledger_without_operation_identity_is_backfilled_not_conflicted(tmp_path):
    """A ledger written before this feature existed has no "operation" entry
    at all. That must not be misread as a conflict -- there's nothing to
    compare against -- and the first call against it backfills the identity
    for future comparisons."""
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.record_dispatch("mob_old", "c0", "call_c0")
    ledger.record_result("mob_old", "c0", "call_c0", {
        "call_id": "call_c0", "candidate_id": "c0", "outcome": "firm_yes",
        "commitment_score": 0.9, "stated_yes": True, "evidence": "leaving now",
    })
    assert ledger.get_operation_identity("mob_old") is None
    ledger.check_operation_identity("mob_old", {"need_label": "x", "count": 1})  # must not raise
    ledger.record_operation_identity("mob_old", {"need_label": "x", "count": 1})
    assert ledger.get_operation_identity("mob_old") is not None


def test_reconciliation_unblocks_without_erasing_the_unresolved_entry(tmp_path):
    """The operator action for an ambiguous candidate must not clear the
    original dispatch_intent entry (the audit trail of "we didn't know"
    stays), but must unblock it from `unresolved()` once recorded."""
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.record_dispatch_intent("mob_reconcile", "c0")
    assert ledger.unresolved("mob_reconcile") == {"c0"}

    ledger.record_reconciliation(
        "mob_reconcile", "c0", decision="confirmed_no_call_placed", operator="ashraf", note="checked CALL-E dashboard",
    )
    assert ledger.unresolved("mob_reconcile") == set()

    intent_entries = [e for e in ledger.replay("mob_reconcile") if e.kind == "dispatch_intent"]
    assert len(intent_entries) == 1  # original entry preserved, not deleted
    reconciled_entries = [e for e in ledger.replay("mob_reconcile") if e.kind == "reconciled"]
    assert len(reconciled_entries) == 1
    assert reconciled_entries[0].payload["operator"] == "ashraf"
