"""The registry: the actual people a coordinator can call, loaded from a
CSV they already have, persisted between sessions, and improved by outcomes.

This is what turns `mobilize` from an engine into something a coordinator
can use. The engine works on `Candidate` objects; a real donor coordinator
has a spreadsheet. This module is the bridge, and it is deliberately
forgiving about what that spreadsheet looks like:

    name, phone, timezone                       <- the only required columns
    last_donation, distance_km,                 <- optional, improves ranking
    accept_rate, showup_rate                    <- optional, learned over time

The learned rates are the point. A coordinator loading a fresh list has no
history, so everyone starts at a neutral prior. After each mobilization,
`record_outcomes` updates each person's accept and show-up rates from what
actually happened -- so the ranking gets better the more you use it, without
anyone having to maintain a spreadsheet column by hand.
"""

from __future__ import annotations

import csv
import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path

from mobilize.core.types import Candidate
from mobilize.core.validation import stable_id_from_phone, validate_e164, validate_timezone

# A person with no history is assumed neither promising nor hopeless. These
# are deliberately middling so that a fresh registry ranks mostly by
# eligibility and distance until real outcomes accumulate.
DEFAULT_ACCEPT_RATE = 0.5
DEFAULT_SHOWUP_RATE = 0.5
DEFAULT_DISTANCE_KM = 10.0

# How fast learned rates move toward the latest outcome. 0.25 means one
# result shifts the rate a quarter of the way -- responsive enough to be
# useful within a few mobilizations, damped enough that a single unlucky
# no-answer doesn't bury someone permanently.
LEARNING_RATE = 0.25

REQUIRED_COLUMNS = {"name", "phone", "timezone"}


class RegistryDomain(str, Enum):
    """Which scenario this registry is being read as. The engine (Candidate,
    dispatcher, policy) is domain-agnostic -- it only ever sees `eligible`.
    This enum exists so `Registry.candidates()` knows WHICH eligibility rule
    to compute that bool from, instead of the dashboard silently applying
    the blood-donor recency rule to every domain (the D4 bug: a shift
    worker filtered out by a 56-day whole-blood-donation interval that has
    nothing to do with them)."""

    DONOR = "donor"
    SHIFT = "shift"


@dataclass
class Person:
    """One member of the registry, as a coordinator thinks of them."""

    id: str
    name: str
    phone: str
    timezone: str
    last_donation: date | None = None
    distance_km: float = DEFAULT_DISTANCE_KM
    accept_rate: float = DEFAULT_ACCEPT_RATE
    showup_rate: float = DEFAULT_SHOWUP_RATE
    times_called: int = 0
    notes: str = ""
    region: str | None = None  # optional CSV column; falls back to the transport's default
    locale: str | None = None

    # --- Donor-domain eligibility -----------------------------------
    # Whether this person may currently be asked to donate. This is
    # deliberately NOT a clinical decision made by mobilize -- 56 days is
    # a real whole-blood-donation interval that belongs to donation-center
    # staff, not to this app. `donor_eligible` is a flag: either supplied
    # by a coordinator/CSV (a real human decision, source="coordinator")
    # or, absent that, a same-shaped placeholder this app computes from
    # recency so a fresh demo registry still has *something* to rank by
    # (source="system_default_unreviewed"). The source is always shown
    # next to the flag -- never presented as a verified clinical call.
    donor_eligible: bool = True
    donor_eligible_source: str = "system_default_unreviewed"  # or "coordinator"
    donor_eligible_set_at: str | None = None  # ISO timestamp, only when source == "coordinator"

    # --- Shift-domain eligibility ------------------------------------
    # Explicit inputs, not inferred from any donor field. A shift
    # candidate is qualified only if they carry the need's required skill
    # (when one is specified) and their stated availability window
    # overlaps the shift's window. Missing availability data means "not
    # verifiably available" -- fails closed, not silently included.
    skills: list[str] = field(default_factory=list)
    shift_available_from: datetime | None = None
    shift_available_until: datetime | None = None

    # Where showup_rate's current value actually came from -- "default" (no
    # data at all), "imported" (a coordinator's CSV shipped a prior showup_rate
    # column, which is someone else's history, not something this product has
    # verified), or "observed" (derived from record_attendance ground truth
    # collected by this product). Never conflated: an imported prior is
    # replaced by observed data once real observations exist, but the two are
    # never averaged together, and observed data always says so honestly.
    showup_rate_source: str = "default"
    # Count of distinct finalized (arrived/did_not_arrive) attendance
    # observations currently backing showup_rate. Zero means showup_rate is
    # still just a prior (imported or default), not something this product
    # has verified -- always show this alongside the rate.
    attendance_sample_count: int = 0
    # The model's own predicted commitment score, nudged from accepted calls'
    # commitment_score. This is a *prediction*, kept deliberately separate
    # from showup_rate (the observed-attendance estimate) so the two are
    # never mistaken for each other. Never fed into dispatch ranking.
    predicted_commitment_estimate: float | None = None

    def days_since_last_donation(self, today: date | None = None) -> float:
        if self.last_donation is None:
            # Never recorded a donation with us -- treat as eligible rather
            # than blocking them, but don't claim a specific recency.
            return 999.0
        today = today or datetime.now(timezone.utc).date()
        return float((today - self.last_donation).days)

    def is_eligible(self, min_days_between_donations: int, today: date | None = None) -> bool:
        """Recency-only signal (kept for the pre-existing callers/tests):
        has enough time passed since their last recorded donation. This is
        NOT the same thing as `donor_eligibility()` below -- recency is one
        input a coordinator's flag may consider, but the flag (and its
        source) is what actually gates dispatch."""
        return self.days_since_last_donation(today) >= min_days_between_donations

    def donor_eligibility(
        self, min_days_between_donations: int = 56, today: date | None = None
    ) -> tuple[bool, str | None]:
        """The donor-domain eligibility decision mobilize actually uses.
        mobilize never makes the clinical call itself -- if a coordinator
        (or an imported CSV column) explicitly set `donor_eligible`, that
        flag is respected as-is, full stop. Only when no such flag exists
        does this app fall back to the recency heuristic, and it labels
        that fallback as unreviewed rather than pretending it is a
        clinical judgment.
        """
        if self.donor_eligible_source == "coordinator":
            eligible = self.donor_eligible
        else:
            eligible = self.is_eligible(min_days_between_donations, today)
        reason = None if eligible else (
            "Coordinator-set eligibility flag is False."
            if self.donor_eligible_source == "coordinator"
            else f"No coordinator eligibility flag on file; system default recency check "
                 f"(<{min_days_between_donations} days since last recorded donation) failed. "
                 f"Not a clinical decision -- verify with collection staff."
        )
        return eligible, reason

    def shift_eligibility(
        self,
        required_skill: str | None,
        shift_start: datetime | None,
        shift_end: datetime | None,
    ) -> tuple[bool, str | None]:
        """The shift-domain eligibility decision: explicit skill and
        explicit availability-window overlap only, never inferred from any
        donor field. Missing availability data fails closed."""
        if required_skill and required_skill not in self.skills:
            return False, f"Missing required skill: {required_skill!r}."
        if shift_start is not None or shift_end is not None:
            if self.shift_available_from is None or self.shift_available_until is None:
                return False, "No recorded availability window; cannot verify overlap with this shift."
            window_start = shift_start or self.shift_available_from
            window_end = shift_end or self.shift_available_until
            overlaps = self.shift_available_from < window_end and self.shift_available_until > window_start
            if not overlaps:
                return False, "Stated availability window does not overlap this shift's window."
        return True, None

    def to_candidate(
        self,
        domain: "RegistryDomain | str" = "donor",
        *,
        min_days_between_donations: int = 56,
        required_skill: str | None = None,
        shift_start: datetime | None = None,
        shift_end: datetime | None = None,
        today: date | None = None,
    ) -> Candidate:
        domain_value = domain.value if isinstance(domain, RegistryDomain) else str(domain)
        if domain_value == RegistryDomain.SHIFT.value:
            eligible, reason = self.shift_eligibility(required_skill, shift_start, shift_end)
            days_since_last_action = 999.0  # donor recency has no meaning for shift workers
        else:
            eligible, reason = self.donor_eligibility(min_days_between_donations, today)
            days_since_last_action = self.days_since_last_donation(today)
        return Candidate(
            id=self.id,
            phone=self.phone,
            name=self.name,
            days_since_last_action=days_since_last_action,
            distance_km=self.distance_km,
            historical_accept_rate=self.accept_rate,
            historical_showup_rate=self.showup_rate,
            eligible=eligible,
            ineligibility_reason=reason,
            timezone=self.timezone,
            region=self.region,
            locale=self.locale,
        )


@dataclass
class Registry:
    people: dict[str, Person] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.people)

    def all(self) -> list[Person]:
        return list(self.people.values())

    def candidates(
        self,
        domain: "RegistryDomain | str" = RegistryDomain.DONOR,
        *,
        min_days_between_donations: int = 56,
        required_skill: str | None = None,
        shift_start: datetime | None = None,
        shift_end: datetime | None = None,
        today: date | None = None,
    ) -> list[Candidate]:
        """Domain-aware: which eligibility rule applies is selected by
        `domain`, never mixed. Donor callers get the donor rule
        (coordinator flag, falling back to recency); shift callers get the
        shift rule (skill + availability overlap) -- the 56-day donor
        interval is never evaluated against a shift candidate and vice
        versa."""
        return [
            p.to_candidate(
                domain,
                min_days_between_donations=min_days_between_donations,
                required_skill=required_skill,
                shift_start=shift_start,
                shift_end=shift_end,
                today=today,
            )
            for p in self.people.values()
        ]

    def get(self, person_id: str) -> Person | None:
        return self.people.get(person_id)


class RegistryError(ValueError):
    """Raised for CSV problems a coordinator can actually act on."""


def load_registry_csv(path: str | Path) -> Registry:
    """Load a coordinator's own CSV. Errors name the row and the problem, so
    a non-technical user can fix their spreadsheet rather than read a
    traceback."""
    path = Path(path)
    if not path.exists():
        raise RegistryError(f"No such file: {path}")

    registry = Registry()
    seen_phones: dict[str, int] = {}  # phone -> first row_number seen
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise RegistryError("The CSV appears to be empty.")

        headers = {h.strip().lower() for h in reader.fieldnames if h}
        missing = REQUIRED_COLUMNS - headers
        if missing:
            raise RegistryError(
                f"Missing required column(s): {', '.join(sorted(missing))}. "
                f"Found: {', '.join(sorted(headers))}. "
                f"A registry CSV needs at least: name, phone, timezone."
            )

        for row_number, raw in enumerate(reader, start=2):  # row 1 is the header
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            if not any(row.values()):
                continue  # tolerate blank lines

            name = row.get("name", "")
            phone = row.get("phone", "")
            tz = row.get("timezone", "")
            if not name or not phone or not tz:
                raise RegistryError(
                    f"Row {row_number}: name, phone, and timezone are all required "
                    f"(got name={name!r}, phone={phone!r}, timezone={tz!r})."
                )
            # Validated here, not just at dispatch time -- a malformed
            # number surfacing deep inside a live wave dispatch is both a
            # worse experience for the coordinator (a cryptic mid-run
            # failure instead of a clear upload error) and a wider blast
            # radius (see the dispatcher's per-candidate error isolation:
            # even with that fix, a bad number is still a candidate that
            # can never actually be called, so better to catch it now).
            try:
                validate_e164(phone)
            except ValueError:
                raise RegistryError(
                    f"Row {row_number}: phone must be E.164 (e.g. +15550101234), got {phone!r}."
                ) from None
            try:
                validate_timezone(tz)
            except ValueError:
                raise RegistryError(
                    f"Row {row_number}: timezone must be a real IANA name (e.g. Asia/Kolkata, "
                    f"America/New_York), got {tz!r}. An invalid value would otherwise silently "
                    f"fall back to UTC, and calling-hour governance would evaluate this person "
                    f"against the wrong clock without any error."
                ) from None

            # Checked independently of the id check below: two rows can
            # have different explicit ids (or different auto-generated
            # ones) and still be the same real phone number -- a copy-paste
            # duplicate, most often. Without this, both get dialed.
            if phone in seen_phones:
                raise RegistryError(
                    f"Row {row_number}: phone {phone!r} was already used in row "
                    f"{seen_phones[phone]}. Duplicate phone numbers get dispatched to "
                    f"independently and would dial the same person twice."
                )
            seen_phones[phone] = row_number

            # The auto-generated fallback id is derived from the phone
            # number, not row position -- a positional id like "p0003"
            # would silently point at a DIFFERENT person if this CSV is
            # ever re-uploaded with rows in a different order, and
            # everything keyed by candidate.id (do-not-call, cooldown,
            # ledger idempotency) would misattribute that person's history.
            person_id = row.get("id") or stable_id_from_phone(phone)
            if person_id in registry.people:
                raise RegistryError(
                    f"Row {row_number}: duplicate id {person_id!r} -- already used by "
                    f"{registry.people[person_id].name!r} earlier in this file. Without this "
                    f"check, the earlier row would be silently overwritten and quietly "
                    f"disappear from the registry."
                )
            registry.people[person_id] = Person(
                id=person_id,
                name=name,
                phone=phone,
                timezone=tz,
                last_donation=_parse_date(row.get("last_donation"), row_number),
                distance_km=_parse_float(row.get("distance_km"), DEFAULT_DISTANCE_KM, row_number, "distance_km"),
                accept_rate=_parse_float(row.get("accept_rate"), DEFAULT_ACCEPT_RATE, row_number, "accept_rate"),
                showup_rate=_parse_float(row.get("showup_rate"), DEFAULT_SHOWUP_RATE, row_number, "showup_rate"),
                # A CSV-supplied showup_rate is someone else's prior history,
                # not something this product observed -- flagged "imported"
                # so it's never displayed or treated as a verified estimate.
                showup_rate_source="imported" if row.get("showup_rate") else "default",
                times_called=int(_parse_float(row.get("times_called"), 0, row_number, "times_called")),
                notes=row.get("notes", ""),
                region=row.get("region") or None,
                locale=row.get("locale") or None,
                donor_eligible=_parse_donor_eligible(row.get("donor_eligible"), row_number)[0],
                donor_eligible_source=_parse_donor_eligible(row.get("donor_eligible"), row_number)[1],
                donor_eligible_set_at=row.get("donor_eligible_set_at") or None,
                skills=_parse_skills(row.get("skills")),
                shift_available_from=_parse_datetime(row.get("shift_available_from"), row_number, "shift_available_from"),
                shift_available_until=_parse_datetime(row.get("shift_available_until"), row_number, "shift_available_until"),
            )

    if not registry.people:
        raise RegistryError("The CSV has headers but no rows.")
    return registry


def save_registry_json(registry: Registry, path: str | Path) -> None:
    """Persist the registry, including learned rates, between sessions."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "people": [
            {
                "id": p.id, "name": p.name, "phone": p.phone, "timezone": p.timezone,
                "last_donation": p.last_donation.isoformat() if p.last_donation else None,
                "distance_km": p.distance_km, "accept_rate": p.accept_rate,
                "showup_rate": p.showup_rate, "times_called": p.times_called, "notes": p.notes,
                "region": p.region, "locale": p.locale,
                "showup_rate_source": p.showup_rate_source,
                "attendance_sample_count": p.attendance_sample_count,
                "predicted_commitment_estimate": p.predicted_commitment_estimate,
                "donor_eligible": p.donor_eligible,
                "donor_eligible_source": p.donor_eligible_source,
                "donor_eligible_set_at": p.donor_eligible_set_at,
                "skills": p.skills,
                "shift_available_from": p.shift_available_from.isoformat() if p.shift_available_from else None,
                "shift_available_until": p.shift_available_until.isoformat() if p.shift_available_until else None,
            }
            for p in registry.people.values()
        ]
    }
    path.write_text(json.dumps(payload, indent=2))


def load_registry_json(path: str | Path) -> Registry:
    path = Path(path)
    if not path.exists():
        return Registry()
    payload = json.loads(path.read_text())
    registry = Registry()
    for raw in payload.get("people", []):
        registry.people[raw["id"]] = Person(
            id=raw["id"], name=raw["name"], phone=raw["phone"], timezone=raw["timezone"],
            last_donation=date.fromisoformat(raw["last_donation"]) if raw.get("last_donation") else None,
            distance_km=raw.get("distance_km", DEFAULT_DISTANCE_KM),
            accept_rate=raw.get("accept_rate", DEFAULT_ACCEPT_RATE),
            showup_rate=raw.get("showup_rate", DEFAULT_SHOWUP_RATE),
            times_called=raw.get("times_called", 0),
            notes=raw.get("notes", ""),
            region=raw.get("region"),
            locale=raw.get("locale"),
            showup_rate_source=raw.get("showup_rate_source", "default"),
            attendance_sample_count=raw.get("attendance_sample_count", 0),
            predicted_commitment_estimate=raw.get("predicted_commitment_estimate"),
            donor_eligible=raw.get("donor_eligible", True),
            donor_eligible_source=raw.get("donor_eligible_source", "system_default_unreviewed"),
            donor_eligible_set_at=raw.get("donor_eligible_set_at"),
            skills=raw.get("skills", []),
            shift_available_from=(
                datetime.fromisoformat(raw["shift_available_from"]) if raw.get("shift_available_from") else None
            ),
            shift_available_until=(
                datetime.fromisoformat(raw["shift_available_until"]) if raw.get("shift_available_until") else None
            ),
        )
    return registry


def record_outcomes(registry: Registry, results: list, *, learning_rate: float = LEARNING_RATE) -> list[str]:
    """Update learned accept rate, and the model's own predicted-commitment
    estimate, from what actually happened on a call.

    `results` is a list of CallResult. Returns the ids that were updated.

    Whether someone *accepted* is directly observable from the call, so
    accept_rate is a genuine running average of real outcomes. Whether they
    *showed up* is NOT observable at call time -- the call ends before that's
    known. This function used to nudge `showup_rate` toward the call's own
    commitment_score as a proxy, which meant the system was learning to
    predict its own predictions rather than real attendance. It no longer
    touches `showup_rate` at all: that field is reserved for genuine
    observations recorded through `record_attendance`. The commitment score
    is instead tracked on `predicted_commitment_estimate`, a clearly separate
    field that is never read by dispatch ranking and never displayed as if it
    were an attendance rate.

    Calls that never reached the person (no answer, failed) contribute
    nothing here -- there is no real signal to learn from. Rehearsal calls
    never reach this function against the operational registry in the first
    place (the dashboard runs them against a private, discarded copy); this
    function has no way to distinguish rehearsal from live on its own, so
    that isolation is the caller's responsibility (see dashboard.py D1).
    """
    from mobilize.core.types import CallOutcome

    updated: list[str] = []
    for result in results:
        person = registry.people.get(result.candidate_id)
        if person is None:
            continue

        accepted = result.outcome in (CallOutcome.FIRM_YES, CallOutcome.SOFT_YES)
        reached = result.outcome not in (CallOutcome.NO_ANSWER, CallOutcome.FAILED)

        if reached:
            person.accept_rate = _nudge(person.accept_rate, 1.0 if accepted else 0.0, learning_rate)
            if accepted:
                prior_estimate = person.predicted_commitment_estimate
                if prior_estimate is None:
                    prior_estimate = DEFAULT_SHOWUP_RATE
                person.predicted_commitment_estimate = _nudge(prior_estimate, result.commitment_score, learning_rate)
        person.times_called += 1
        updated.append(person.id)
    return updated


class AttendanceStatus(str, Enum):
    """What a coordinator has recorded about whether a specific person
    actually arrived for a specific mobilization. Deliberately four states,
    not two -- collapsing PENDING or UNKNOWN into "no" would silently invent
    a no-show that was never actually observed."""

    ARRIVED = "arrived"
    DID_NOT_ARRIVE = "did_not_arrive"
    PENDING = "pending"
    UNKNOWN = "unknown"

    # Only ARRIVED/DID_NOT_ARRIVE are a finalized ground-truth observation
    # that should ever move showup_rate. PENDING/UNKNOWN are recorded (for
    # audit and display) but never counted as attendance either way.


@dataclass(frozen=True)
class AttendanceRecord:
    """One entry in the attendance audit trail. A correction produces a new
    record with `corrected_from` set to the prior status -- the prior record
    is never deleted or edited in place, only superseded."""

    mobilization_id: str
    person_id: str
    status: str  # AttendanceStatus value
    recorded_at: str
    recorded_by: str | None = None
    corrected_from: str | None = None


class AttendanceLog:
    """Durable, append-only audit trail of attendance observations, keyed by
    (mobilization_id, person_id). Uses the same atomic-append pattern as
    `Ledger` (write, flush, fsync before ack) rather than inventing a new
    persistence mechanism -- see mobilize/core/ledger.py.

    A second call for the same (mobilization_id, person_id) is a correction:
    it becomes the new "current" observation for that key, but the prior
    record stays in the file, so the full history of what was recorded and
    when is always recoverable. This is what makes a duplicate click or a
    genuine correction behave differently from "add another sample to an
    average" -- the *current* view has exactly one entry per key, no matter
    how many times it was recorded.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not self._path.exists():
            self._path.touch()
        self._current: dict[tuple[str, str], AttendanceRecord] = {}
        for record in self._replay_all():
            self._current[(record.mobilization_id, record.person_id)] = record

    def record(
        self,
        mobilization_id: str,
        person_id: str,
        status: AttendanceStatus | str,
        *,
        at: str | None = None,
        recorded_by: str | None = None,
    ) -> AttendanceRecord:
        status_value = status.value if isinstance(status, AttendanceStatus) else str(status)
        if status_value not in {s.value for s in (AttendanceStatus.ARRIVED, AttendanceStatus.DID_NOT_ARRIVE,
                                                    AttendanceStatus.PENDING, AttendanceStatus.UNKNOWN)}:
            raise ValueError(f"Unknown attendance status: {status_value!r}")
        key = (mobilization_id, person_id)
        prior = self._current.get(key)
        record = AttendanceRecord(
            mobilization_id=mobilization_id,
            person_id=person_id,
            status=status_value,
            recorded_at=at or datetime.now(timezone.utc).isoformat(),
            recorded_by=recorded_by,
            corrected_from=prior.status if prior is not None else None,
        )
        with self._lock:
            with self._path.open("a") as f:
                f.write(json.dumps(asdict(record)) + "\n")
                f.flush()
                os.fsync(f.fileno())
        self._current[key] = record
        return record

    def current_for_person(self, person_id: str) -> list[AttendanceRecord]:
        """The latest observation per mobilization for this person -- one
        entry per (mobilization_id, person_id) key, never more, regardless of
        how many times it was recorded or corrected."""
        return [r for (mob_id, pid), r in self._current.items() if pid == person_id]

    def current(self, mobilization_id: str, person_id: str) -> AttendanceRecord | None:
        return self._current.get((mobilization_id, person_id))

    def history_for(self, mobilization_id: str, person_id: str) -> list[AttendanceRecord]:
        """Every record ever written for this key, in order -- the full
        audit trail, including superseded corrections."""
        return [r for r in self._replay_all() if r.mobilization_id == mobilization_id and r.person_id == person_id]

    def _replay_all(self) -> list[AttendanceRecord]:
        if not self._path.exists():
            return []
        records: list[AttendanceRecord] = []
        with self._path.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                records.append(AttendanceRecord(**json.loads(line)))
        return records


def record_attendance(
    registry: Registry,
    attendance_log: AttendanceLog,
    mobilization_id: str,
    person_id: str,
    status: AttendanceStatus | str,
    *,
    at: str | None = None,
    recorded_by: str | None = None,
) -> AttendanceRecord | None:
    """Ground truth, when a coordinator records who actually arrived for a
    specific mobilization. This is the ONLY thing that should ever move
    showup_rate -- not a predicted commitment score, not a rehearsal call,
    not an unanswered call.

    A repeated call for the same (mobilization_id, person_id) is a
    correction, not a new sample: it replaces the prior observation for that
    key rather than adding another data point to the average, and the prior
    observation is preserved in `attendance_log`'s audit trail (see
    `AttendanceLog`). showup_rate is then recomputed from scratch as
    arrived / finalized across all of this person's current (i.e.
    post-correction) observations -- so a correction changes the rate
    exactly once, never twice.

    Returns None (and touches nothing) if the person isn't in this registry.
    """
    person = registry.people.get(person_id)
    if person is None:
        return None

    record = attendance_log.record(mobilization_id, person_id, status, at=at, recorded_by=recorded_by)
    _recompute_showup_rate(person, attendance_log)
    return record


def _recompute_showup_rate(person: Person, attendance_log: AttendanceLog) -> None:
    """Recompute showup_rate purely from this person's current (deduplicated,
    corrected) finalized observations. Deliberately a full recompute rather
    than an incremental nudge -- that's what makes "a correction replaces the
    prior observation" exact rather than approximate: the same observation
    counted twice, or a stale one left in after a correction, is structurally
    impossible when the estimate is always rebuilt from the current set."""
    finalized_values = {AttendanceStatus.ARRIVED.value, AttendanceStatus.DID_NOT_ARRIVE.value}
    finalized = [r for r in attendance_log.current_for_person(person.id) if r.status in finalized_values]
    person.attendance_sample_count = len(finalized)
    if finalized:
        arrived = sum(1 for r in finalized if r.status == AttendanceStatus.ARRIVED.value)
        person.showup_rate = arrived / len(finalized)
        person.showup_rate_source = "observed"
    # No finalized observations yet: leave showup_rate (and its source,
    # "default" or "imported") exactly as it was. Recording only PENDING or
    # UNKNOWN for someone must never move their rate.


def _nudge(current: float, observed: float, rate: float) -> float:
    return max(0.02, min(0.98, current + rate * (observed - current)))


def _parse_date(value: str | None, row_number: int) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise RegistryError(
            f"Row {row_number}: last_donation must be a date like 2026-05-14, got {value!r}."
        ) from None


_TRUE_VALUES = {"true", "yes", "1", "y"}
_FALSE_VALUES = {"false", "no", "0", "n"}


def _parse_donor_eligible(value: str | None, row_number: int) -> tuple[bool, str]:
    """A CSV-supplied donor_eligible column is a coordinator's own explicit
    decision -- respected verbatim, source="coordinator". Absent, this app
    falls back to the recency heuristic at candidate-build time and says so
    (source="system_default_unreviewed"); it never invents a clinical
    eligibility value here."""
    if not value:
        return True, "system_default_unreviewed"
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True, "coordinator"
    if normalized in _FALSE_VALUES:
        return False, "coordinator"
    raise RegistryError(
        f"Row {row_number}: donor_eligible must be yes/no/true/false, got {value!r}."
    )


def _parse_skills(value: str | None) -> list[str]:
    if not value:
        return []
    return [s.strip() for s in value.split("|") if s.strip()]


def _parse_datetime(value: str | None, row_number: int, column: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise RegistryError(
            f"Row {row_number}: {column} must be an ISO datetime (e.g. 2026-05-14T09:00:00+00:00), "
            f"got {value!r}."
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_float(value: str | None, default: float, row_number: int, column: str) -> float:
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        raise RegistryError(f"Row {row_number}: {column} must be a number, got {value!r}.") from None
