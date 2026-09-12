"""Core data types shared across the mobilize engine.

Keep these plain and serializable (JSON-friendly dicts/dataclasses) so the
same types flow through the ledger, both transports, and the dashboard
without translation layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CallOutcome(str, Enum):
    FIRM_YES = "firm_yes"
    SOFT_YES = "soft_yes"
    NO = "no"
    INELIGIBLE = "ineligible"
    NO_ANSWER = "no_answer"
    FAILED = "failed"


class DecisionReason(str, Enum):
    """Stable machine codes for WHY `_to_call_result` (transports/calle.py)
    landed on a given CallOutcome. A0/A1: this is additive provenance next to
    the outcome, never a second place that decides the outcome -- each code
    is set at the exact existing branch that already made the call, not
    derived independently afterward.

    Historical ledger entries written before this field existed deserialize
    with `decision_reason=None` (see dispatcher._deserialize), which must be
    read as "unknown", never guessed at.
    """

    BINDING_MISMATCH = "binding_mismatch"
    CALL_OR_RECIPIENT_FAILED = "call_or_recipient_failed"
    TASK_INCOMPLETE = "task_incomplete"
    RECIPIENT_DECLINED = "recipient_declined"
    ABSENT_TRANSCRIPT = "absent_transcript"
    TASK_COMPLETION_UNCONFIRMED = "task_completion_unconfirmed"
    FINAL_POSITION_UNCLEAR = "final_position_unclear"
    MISSING_RECIPIENT_CORROBORATION = "missing_recipient_corroboration"
    AMBIGUOUS_PROVIDER_RESPONSE = "ambiguous_provider_response"
    CONFIRMED = "confirmed"  # a real firm_yes/soft_yes -- recorded too, not just refusals


# Human-readable text for each DecisionReason code. Kept next to the enum
# so a new code can't be added without also adding its explanation.
DECISION_REASON_TEXT: dict[str, str] = {
    DecisionReason.BINDING_MISMATCH.value: (
        "The result's candidate metadata or dialed phone did not match the "
        "candidate we dispatched to; discarded rather than counted."
    ),
    DecisionReason.CALL_OR_RECIPIENT_FAILED.value: (
        "The call or recipient leg ended in 'failed' or 'canceled' status."
    ),
    DecisionReason.TASK_INCOMPLETE.value: (
        "CALL-E explicitly reported task_completed=False for this call."
    ),
    DecisionReason.RECIPIENT_DECLINED.value: (
        "The provider's structured extraction reported can_come='no'."
    ),
    DecisionReason.ABSENT_TRANSCRIPT.value: (
        "can_come='yes' was reported but no transcript backs it -- treated "
        "as unsupported rather than a real confirmation."
    ),
    DecisionReason.TASK_COMPLETION_UNCONFIRMED.value: (
        "can_come='yes' but task_completed was not explicitly True."
    ),
    DecisionReason.FINAL_POSITION_UNCLEAR.value: (
        "can_come='yes' but the provider's own final_position field was not "
        "'confirmed' (missing, unclear, or contradicted)."
    ),
    DecisionReason.MISSING_RECIPIENT_CORROBORATION.value: (
        "can_come='yes' and final_position='confirmed', but the recipient's "
        "own transcribed words do not affirmatively corroborate it."
    ),
    DecisionReason.AMBIGUOUS_PROVIDER_RESPONSE.value: (
        "can_come was neither 'yes' nor 'no' (e.g. 'unknown') -- no clear "
        "evidence either way."
    ),
    DecisionReason.CONFIRMED.value: (
        "can_come='yes', task_completed=True, final_position='confirmed', and "
        "the recipient's own words corroborate it: a real firm_yes/soft_yes."
    ),
}


class ContactOutcome(str, Enum):
    """A0 outcome ladder: the recipient-facing fact a single call ended on,
    distinct from CallOutcome (which also encodes the commitment-strength
    split firm/soft) and distinct from `filled`/`confirmed` on MobilizeResult
    (which are roster-level, not per-call). Kept as a separate, smaller
    vocabulary so 'was there a real agreement' can be asked without
    reasoning about firm/soft thresholds or aggregate counts.
    """

    AGREEMENT = "agreement"  # firm or soft yes, evidence-corroborated
    CONDITIONAL = "conditional"  # not modeled by current CALL-E schema; reserved
    REFUSAL = "refusal"  # can_come == "no"
    NO_CONTACT = "no_contact"  # call/recipient failed, canceled, or never completed
    EXECUTION_FAILURE = "execution_failure"  # binding mismatch / our own dispatch error
    MISSING_EVIDENCE = "missing_evidence"  # yes claimed but transcript/final_position/task signal absent
    CONFLICTING_EVIDENCE = "conflicting_evidence"  # yes claimed but recipient's own words don't corroborate it


class StopReason(str, Enum):
    """A0 workflow-stop codes: why `mobilize()` (core/dispatcher.py) stopped
    dispatching further waves for a mobilization. Set once, at the end of
    the run, from the same conditions the wave loop already checks -- this
    does not change when the loop exits, only records why.
    """

    TARGET_MET = "target_met"
    DEADLINE = "deadline"
    BUDGET_EXHAUSTED = "budget_exhausted"
    NO_ELIGIBLE_CANDIDATES = "no_eligible_candidates"
    UNRESOLVED_DISPATCH_OR_CALL = "unresolved_dispatch_or_call"
    # No operator-pause control exists yet anywhere in this codebase (no
    # pause/resume UI or CLI verb) -- the code is defined so persistence and
    # events have a stable slot for it once a pause feature exists, but
    # `mobilize()` never emits it today. Left for a future D-lane product
    # feature, not implemented here.
    OPERATOR_PAUSE = "operator_pause"


@dataclass(frozen=True)
class Need:
    """A mobilization request: get `count` confirmations by `deadline_minutes`."""

    label: str
    count: int
    deadline_minutes: float
    location: str
    max_calls: int
    # D4: explicit, domain-specific inputs -- never inferred from donor
    # fields. Only meaningful when the registry is being read in the shift
    # domain; None (the default) for donor mobilizations.
    required_skill: str | None = None
    # D4: "dispatch cutoff" (deadline_minutes, above -- the last moment it
    # makes sense to START a new call for this need; enforced by
    # dispatcher.py's wave loop, unchanged) is a conceptually different
    # deadline from "latest useful arrival" (the last moment someone could
    # SHOW UP and still be useful). This field captures the latter as an
    # explicit, separate, operator-supplied input. It is advisory/display
    # only in this codebase today -- nothing in dispatcher.py's wave loop
    # reads it, so it does not gate dispatch. It exists so the dashboard
    # can flag a confirmed candidate's stated ETA (see CallResult.stated_eta)
    # as "past the point of being useful" without conflating that judgment
    # with when dispatch itself stopped.
    latest_useful_arrival_minutes: float | None = None


@dataclass(frozen=True)
class Candidate:
    """One member of the consented pool who may be called."""

    id: str
    phone: str
    name: str
    days_since_last_action: float
    distance_km: float
    historical_accept_rate: float  # prior: how often they say yes when asked
    historical_showup_rate: float  # prior: how often a stated yes becomes real action
    eligible: bool = True
    timezone: str = "UTC"  # IANA name, e.g. "Asia/Kolkata" -- governs calling-hour checks
    # Per-candidate, not fixed at the transport level: a CalleTransport
    # instance previously hardcoded one region/locale for every dispatch,
    # which is wrong the moment a registry has recipients in more than one
    # country -- exactly the case for this project's own Kolkata-based
    # sample registry against a transport defaulting to "US". None means
    # "no override, fall back to the transport's own default."
    region: str | None = None
    locale: str | None = None
    # D4: why `eligible` is False, when it is -- e.g. "missing required
    # skill" (shift) or "coordinator-set eligibility flag is False" /
    # "system default recency check failed" (donor). None when eligible is
    # True. Purely descriptive provenance for the dashboard/operator; never
    # read by dispatch or policy logic, which only ever look at `eligible`.
    ineligibility_reason: str | None = None

    def prior_score(self) -> float:
        """Higher is better: more likely to say yes AND follow through."""
        recency = min(1.0, self.days_since_last_action / 90.0)
        distance_penalty = 1.0 / (1.0 + self.distance_km / 10.0)
        return (
            0.4 * self.historical_accept_rate
            + 0.4 * self.historical_showup_rate
            + 0.1 * recency
            + 0.1 * distance_penalty
        )


@dataclass(frozen=True)
class CallResult:
    """Normalized result of one dispatched call, from either transport."""

    call_id: str
    candidate_id: str
    outcome: CallOutcome
    commitment_score: float  # 0..1, calibrated probability of actually showing up
    stated_yes: bool
    evidence: str
    stop_requested: bool = False  # recipient explicitly asked never to be contacted again
    transcript: list[dict] = field(default_factory=list)
    started_at: datetime = field(default_factory=utcnow)
    completed_at: datetime | None = None
    raw: dict = field(default_factory=dict)
    # A1: WHY _to_call_result landed on `outcome`, not just what it landed
    # on -- a DecisionReason code, set at the exact branch that decided the
    # outcome (transports/calle.py). None on results produced before this
    # field existed (old ledger entries) or, in principle, for any future
    # caller that doesn't set it -- always "unknown", never guessed.
    decision_reason: str | None = None
    # A0: this call's outcome ladder position (a ContactOutcome code),
    # set alongside decision_reason at the same branch. Independent of
    # commitment_score/COMMITMENT_THRESHOLD, which is an aggregation-time
    # concern (dispatcher.py), not a per-call fact.
    contact_outcome: str | None = None
    # D4: if the recipient stated an ETA during the call, retained together
    # with the evidence quote it was extracted from -- never one without
    # the other. NOTE: no transport in this codebase currently populates
    # these fields (extracting an ETA from CALL-E's structured result is
    # C-lane's contract work in transports/calle.py, out of D4's scope);
    # they exist here as forward-compatible schema so that when extraction
    # lands, the dashboard already has somewhere honest to show it and flag
    # it for operator review. Until populated, both are None, and nothing
    # in this codebase infers GPS position or confirms arrival from them --
    # a stated ETA is a claim from the call, not an observation.
    stated_eta: str | None = None
    stated_eta_evidence: str | None = None


@dataclass
class Wave:
    """One dispatched batch within a mobilization."""

    index: int
    candidate_ids: list[str]
    dispatched_at: datetime = field(default_factory=utcnow)
    results: list[CallResult] = field(default_factory=list)


@dataclass
class MobilizeResult:
    need: Need
    confirmed: list[CallResult]
    all_results: list[CallResult]
    waves: list[Wave]
    calls_used: int
    time_to_fill_seconds: float | None
    filled: bool
    over_recruitment_ratio: float
    # Candidates whose dispatch attempt raised an error that COULD mean
    # CALL-E already accepted the request before the exception surfaced
    # (a timeout, a connection reset) -- not confirmed as ever having been
    # called, but not safely known to have never been called either.
    # Never auto-retried; surfaced here for a human to check manually.
    ambiguous_candidate_ids: list[str] = field(default_factory=list)
    # A0: why the wave loop actually stopped (a StopReason code), set once
    # by core/dispatcher.py from the same conditions the loop already
    # checks. None only if a caller predates this field or constructs a
    # MobilizeResult without going through mobilize() (e.g. some test doubles).
    stop_reason: str | None = None
    # A0: the reviewed counts table (see OutcomeCounts below), attached as a
    # plain dict via `OutcomeCounts.as_dict()` so old callers/serializers
    # that don't know about OutcomeCounts still get a JSON-friendly value.
    # None for results built before this field existed.
    counts: dict | None = None


@dataclass(frozen=True)
class OutcomeCounts:
    """A0: the reviewed counts table for one mobilization, keyed by
    candidate/operation IDs rather than derived by ad hoc subtraction.

    Each field names an explicit denominator. Some fields PARTITION the
    roster (every candidate is in exactly one of registered's subsets at a
    given stage); others describe a SUBSET that overlaps another (e.g.
    `commitments` and `excess_commitments` both draw from `completed`).
    Comments below say which is which -- this table is what both the
    dashboard and tests should read, instead of re-deriving counts from
    raw result lists in more than one place.
    """

    # --- Roster partition: every registered candidate is in exactly one
    # of {governance_blocked, eligible-but-never-attempted, attempted}. ---
    registered: int  # size of the full input pool, before any filtering
    eligible: int  # registered AND not governance-blocked (do_not_call/hours/cooldown/fatigue)
    governance_blocked: int  # registered AND blocked by policy.is_callable -- registered - eligible

    # --- Of `eligible`, the subset actually dispatched this run/resume. ---
    attempted: int  # a dispatch was actually attempted (dispatch_intent logged)
    definitely_not_dispatched: int  # eligible AND never attempted (eligible - attempted)

    # --- Of `attempted`, how the dispatch itself resolved. Overlapping
    # subsets of `attempted`, not a further partition of the roster. ---
    accepted_by_provider: int  # transport.dispatch() returned a call_id
    unresolved: int  # dispatch_intent logged with no resolved call_id or result (ledger.unresolved())

    # --- Of `accepted_by_provider`, how the call resolved. ---
    completed: int  # a terminal CallResult was recorded (poll returned non-None)

    # --- Of `completed`, evidence-supported commitment subsets (see
    # ContactOutcome.AGREEMENT + commitment_score >= COMMITMENT_THRESHOLD).
    # `commitments` is capped conceptually at `need.count` for "filled";
    # anything beyond that is `excess_commitments` -- both are subsets of
    # `completed`, not additional people. ---
    commitments: int  # min(accepted confirmations, need.count) -- what actually filled the need
    excess_commitments: int  # accepted confirmations beyond need.count -- NOT hidden to make counts look exact

    def as_dict(self) -> dict:
        """Plain, JSON-friendly dict -- what MobilizeResult.counts stores,
        so old callers/serializers that don't know about OutcomeCounts still
        get a usable value."""
        from dataclasses import asdict

        return asdict(self)


def build_outcome_counts(
    *,
    registered: int,
    governance_blocked: int,
    attempted: int,
    accepted_by_provider: int,
    unresolved: int,
    completed_results: list[CallResult],
    need_count: int,
) -> OutcomeCounts:
    """Build the reviewed counts table from primitive tallies plus the list
    of completed CallResults. Confirmations are computed here, once, from
    the same CONFIRMED_OUTCOMES/COMMITMENT_THRESHOLD definition dispatcher.py
    uses for `confirmed` -- duplicated as constants (not by importing
    dispatcher, to avoid a core-module import cycle), so keep both in sync
    if the threshold ever changes.
    """
    from mobilize.core.types import CallOutcome as _CallOutcome  # local: no cycle at module load

    confirmed_outcomes = {_CallOutcome.FIRM_YES, _CallOutcome.SOFT_YES}
    commitment_threshold = 0.55  # must match dispatcher.COMMITMENT_THRESHOLD

    confirmations = [
        r for r in completed_results
        if r.outcome in confirmed_outcomes and r.commitment_score >= commitment_threshold
    ]
    commitments = min(len(confirmations), need_count)
    excess_commitments = max(0, len(confirmations) - need_count)

    eligible = registered - governance_blocked
    return OutcomeCounts(
        registered=registered,
        eligible=eligible,
        governance_blocked=governance_blocked,
        attempted=attempted,
        definitely_not_dispatched=max(0, eligible - attempted),
        accepted_by_provider=accepted_by_provider,
        unresolved=unresolved,
        completed=len(completed_results),
        commitments=commitments,
        excess_commitments=excess_commitments,
    )


# --- A0 worked examples ------------------------------------------------
#
# SUCCESS: Need(count=3, max_calls=10). 10 registered, 1 governance-blocked
# (cooldown_active), 9 eligible, 5 attempted, 5 accepted_by_provider, 0
# unresolved, 5 completed, 4 of those are AGREEMENT with commitment_score
# above threshold. commitments = min(4, 3) = 3, excess_commitments = 1.
# stop_reason = TARGET_MET. filled = True (unchanged, old field). The extra
# accepted person is visible in excess_commitments, not discarded.
#
# SHORTAGE: Need(count=5, max_calls=6). 6 registered, 0 blocked, 6 eligible,
# 6 attempted, 6 accepted_by_provider, 0 unresolved, 6 completed, only 2
# AGREEMENT above threshold. commitments = 2, excess_commitments = 0.
# definitely_not_dispatched = 0 (everyone eligible was tried).
# stop_reason = BUDGET_EXHAUSTED. filled = False. Nothing here claims a
# fill that didn't happen -- confirmed has 2 entries, not 5.
#
# AMBIGUITY: Need(count=2, max_calls=8). One candidate's dispatch raised a
# connection-reset exception (transport.dispatch() threw a non-ValueError):
# logged as `attempted` (dispatch_intent) but never `accepted_by_provider`,
# so it lands in `unresolved` and in MobilizeResult.ambiguous_candidate_ids.
# The wave loop refuses to start a new wave while any ambiguous ID is
# outstanding (see dispatcher.py's loop guard) -- stop_reason =
# UNRESOLVED_DISPATCH_OR_CALL, even if enough OTHER confirmations came in
# to otherwise look "done". This is deliberate: an unresolved call may
# already be live and must be reconciled by a human before this
# mobilization is treated as finished.
