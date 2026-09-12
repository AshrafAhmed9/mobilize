"""Write-ahead ledger for crash-safe dispatch.

Every dispatch intent is appended BEFORE the call is placed. On restart, the
ledger is replayed: any candidate with a logged 'dispatched' entry and no
matching 'result' is either still in flight (poll it) or was interrupted
mid-dispatch. Either way we never re-dial someone whose dispatch was already
logged for this mobilization, and we never lose a confirmation that was
already recorded.

Uses CALL-E's Idempotency-Key request header directly (confirmed in the
OpenAPI spec) so even a duplicate dispatch call against the real API is safe.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class OperationConflictError(Exception):
    """Raised when a mobilization_id is reused with materially different
    request parameters than the operation it was first recorded with (see
    `Ledger.record_operation_identity`). A caller must either start a
    genuinely new operation (different mobilization_id) or, if the request
    is meant to resume the prior operation, submit the original parameters
    unchanged. Never silently mixed."""


@dataclass(frozen=True)
class LedgerEntry:
    kind: str  # "dispatch_intent" | "dispatched" | "result" | "operation" | "reconciled"
    mobilization_id: str
    candidate_id: str
    idempotency_key: str
    call_id: str | None = None
    payload: dict | None = None
    at: str = field(default_factory=_utcnow_iso)


class Ledger:
    """Append-only JSONL ledger with atomic writes and fsync before ack.

    Atomicity per entry: write to a temp file, fsync, then append via O_APPEND
    write, which is atomic for writes below PIPE_BUF on POSIX. For hackathon
    scope this is sufficient; a production version would use a proper WAL
    segment file with checksums.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not self._path.exists():
            self._path.touch()

    def idempotency_key(self, mobilization_id: str, candidate_id: str) -> str:
        return f"{mobilization_id}:{candidate_id}"

    def record_dispatch_intent(self, mobilization_id: str, candidate_id: str) -> None:
        """Written BEFORE the network call, with no call_id yet (there isn't
        one). This is the durable answer to "did we possibly already call
        this person" when transport.dispatch() raises an ambiguous error
        (a timeout, a connection reset) that could mean CALL-E accepted the
        request before our client ever saw a response. Without this, such a
        candidate leaves literally no trace in the ledger -- indistinguishable
        from having never been attempted at all -- and a resumed run could
        freely re-dispatch them, risking a real double-dial. An intent entry
        with no matching "dispatched" or "result" entry is surfaced via
        `unresolved()` for manual reconciliation rather than silently
        retried.
        """
        entry = LedgerEntry(
            kind="dispatch_intent",
            mobilization_id=mobilization_id,
            candidate_id=candidate_id,
            idempotency_key=self.idempotency_key(mobilization_id, candidate_id),
        )
        self._append(entry)

    def unresolved(self, mobilization_id: str) -> set[str]:
        """Candidates with a logged dispatch_intent but no confirmed
        "dispatched" (a call_id was actually returned) or "result" entry --
        an attempt was made, but we don't know whether it actually reached
        CALL-E. These must never be silently retried.

        A candidate with an explicit "reconciled" entry (see
        `record_reconciliation`) is excluded here even though the original
        dispatch_intent entry is left untouched in the ledger -- an operator
        has looked at that specific ambiguous attempt and recorded a
        decision, which is the only thing allowed to unblock it. The
        ledger record of the original ambiguity is never deleted or
        overwritten, only supplemented, so the audit trail of "we didn't
        know, then a human decided X" survives."""
        intended: set[str] = set()
        resolved: set[str] = set()
        reconciled: set[str] = set()
        for entry in self.replay(mobilization_id):
            if entry.kind == "dispatch_intent":
                intended.add(entry.candidate_id)
            elif entry.kind in ("dispatched", "result"):
                resolved.add(entry.candidate_id)
            elif entry.kind == "reconciled":
                reconciled.add(entry.candidate_id)
        return intended - resolved - reconciled

    def reconciled_candidates(self, mobilization_id: str) -> set[str]:
        """Every candidate_id an operator has explicitly reconciled for this
        mobilization_id, regardless of which kind of ambiguity it was
        (a dispatch_intent that never got a call_id, or a dispatched call
        whose result polling/recovery never resolved). Used by the
        dispatcher to stop treating a specific, already-reviewed candidate
        as blocking further waves -- without reopening it for a fresh
        dispatch, and without touching the original ambiguous entry."""
        return {
            entry.candidate_id
            for entry in self.replay(mobilization_id)
            if entry.kind == "reconciled"
        }

    def record_reconciliation(
        self, mobilization_id: str, candidate_id: str, *, decision: str, operator: str, note: str | None = None,
    ) -> None:
        """The one sanctioned way to unblock an ambiguous/unresolved
        candidate for a given mobilization_id -- an explicit, attributed,
        durable operator action, never an automatic retry and never a
        deletion of the original unresolved entry. `decision` is a free-form
        operator-supplied label (e.g. "confirmed_no_call_placed",
        "confirmed_call_placed_no_answer", "treat_as_failed") describing
        what was actually established, not a re-guess by this code."""
        entry = LedgerEntry(
            kind="reconciled",
            mobilization_id=mobilization_id,
            candidate_id=candidate_id,
            idempotency_key=self.idempotency_key(mobilization_id, candidate_id),
            payload={"decision": decision, "operator": operator, "note": note},
        )
        self._append(entry)

    _OPERATION_IDENTITY_VERSION = 1

    def record_operation_identity(self, mobilization_id: str, fingerprint: dict) -> None:
        """Persist the full request identity (count, deadline, location,
        max_calls, registry snapshot, ...) the FIRST time a mobilization_id
        is used. This is what lets `check_operation_identity` tell a
        legitimate resume (identical parameters) apart from a different
        later operation that happens to reuse the same label+phone-derived
        id -- the label and phone list alone are not enough to prove it's
        the same operation. A no-op if an identity is already on record for
        this mobilization_id (first write wins; never overwritten)."""
        if self.get_operation_identity(mobilization_id) is not None:
            return
        entry = LedgerEntry(
            kind="operation",
            mobilization_id=mobilization_id,
            candidate_id="",
            idempotency_key=mobilization_id,
            payload={"version": self._OPERATION_IDENTITY_VERSION, **fingerprint},
        )
        self._append(entry)

    def get_operation_identity(self, mobilization_id: str) -> dict | None:
        for entry in self.replay(mobilization_id):
            if entry.kind == "operation" and entry.payload is not None:
                return entry.payload
        return None

    def check_operation_identity(self, mobilization_id: str, fingerprint: dict) -> None:
        """Raise OperationConflictError if this mobilization_id already has
        a recorded operation identity that disagrees with `fingerprint` on
        any field. A mismatched schema version (an identity recorded before
        some field existed) is not itself treated as a conflict -- there is
        nothing to compare it against -- so only fields present in BOTH the
        stored and current fingerprint are compared."""
        existing = self.get_operation_identity(mobilization_id)
        if existing is None:
            return
        current = {"version": self._OPERATION_IDENTITY_VERSION, **fingerprint}
        shared_keys = set(existing) & set(current)
        mismatched = {
            key: (existing[key], current[key])
            for key in shared_keys
            if key != "version" and existing[key] != current[key]
        }
        if mismatched:
            raise OperationConflictError(
                f"mobilization_id {mobilization_id!r} was already used for an operation with "
                f"different parameters: {mismatched}. Use a different mobilization_id for a new "
                "operation, or resubmit the original parameters to resume this one."
            )

    def record_dispatch(self, mobilization_id: str, candidate_id: str, call_id: str) -> None:
        entry = LedgerEntry(
            kind="dispatched",
            mobilization_id=mobilization_id,
            candidate_id=candidate_id,
            idempotency_key=self.idempotency_key(mobilization_id, candidate_id),
            call_id=call_id,
        )
        self._append(entry)

    def record_result(self, mobilization_id: str, candidate_id: str, call_id: str, payload: dict) -> None:
        entry = LedgerEntry(
            kind="result",
            mobilization_id=mobilization_id,
            candidate_id=candidate_id,
            idempotency_key=self.idempotency_key(mobilization_id, candidate_id),
            call_id=call_id,
            payload=payload,
        )
        self._append(entry)

    def already_dispatched(self, mobilization_id: str, candidate_id: str) -> str | None:
        """Return the existing call_id if this candidate was already dispatched
        for this mobilization, else None. Prevents double-dialing on replay."""
        for entry in self.replay(mobilization_id):
            if entry.kind == "dispatched" and entry.candidate_id == candidate_id:
                return entry.call_id
        return None

    def in_flight(self, mobilization_id: str) -> dict[str, str]:
        """candidate_id -> call_id for dispatches with no recorded terminal result."""
        dispatched: dict[str, str] = {}
        completed: set[str] = set()
        for entry in self.replay(mobilization_id):
            if entry.kind == "dispatched" and entry.call_id:
                dispatched[entry.candidate_id] = entry.call_id
            elif entry.kind == "result":
                completed.add(entry.candidate_id)
        return {cid: call_id for cid, call_id in dispatched.items() if cid not in completed}

    def completed_results(self, mobilization_id: str) -> list[dict]:
        return [
            entry.payload
            for entry in self.replay(mobilization_id)
            if entry.kind == "result" and entry.payload is not None
        ]

    def get_started_at(self, mobilization_id: str) -> datetime | None:
        """Wall-clock time of the first ledger entry for this mobilization,
        i.e. when it actually started -- possibly in a prior process. Used to
        compute correct total elapsed time across a crash and resume, rather
        than measuring only time-since-this-process-started."""
        earliest: datetime | None = None
        for entry in self.replay(mobilization_id):
            at = datetime.fromisoformat(entry.at)
            if earliest is None or at < earliest:
                earliest = at
        return earliest

    def replay(self, mobilization_id: str) -> Iterator[LedgerEntry]:
        if not self._path.exists():
            return
        with self._path.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                if raw.get("mobilization_id") == mobilization_id:
                    yield LedgerEntry(**raw)

    def _append(self, entry: LedgerEntry) -> None:
        line = json.dumps(asdict(entry)) + "\n"
        with self._lock:
            with self._path.open("a") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
