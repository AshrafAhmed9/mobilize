"""D4: donor and shift are genuinely separate scenarios sharing one engine.

The bug this guards against: the dashboard used to call
`registry.candidates(min_days_between_donations=56)` unconditionally -- a
56-day whole-blood-donation interval silently gating everyone, including
shift workers it has no business judging. These tests prove domain
isolation directly against Registry/Person, independent of the dashboard.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from mobilize.core.registry import Person, Registry, RegistryDomain, load_registry_csv

SAMPLE_DONOR_CSV = Path(__file__).resolve().parent.parent / "app" / "sample_data" / "sample_registry.csv"
SAMPLE_SHIFT_CSV = Path(__file__).resolve().parent.parent / "app" / "sample_data" / "sample_shift_registry.csv"

TODAY = date(2026, 9, 12)


def test_shift_candidates_not_excluded_by_donor_recency_rule():
    """Several people in the shift sample donated within the last 56 days
    (would fail the donor recency rule). Reading the SAME registry in the
    shift domain, with no shift constraints supplied, must not exclude any
    of them on that basis -- the donor rule is simply never evaluated."""
    registry = load_registry_csv(SAMPLE_SHIFT_CSV)
    candidates = registry.candidates(RegistryDomain.SHIFT, today=TODAY)
    recently_donated_ids = {"s001", "s002", "s004"}  # last_donation within 56 days of TODAY
    assert recently_donated_ids <= {c.id for c in candidates}
    for c in candidates:
        if c.id in recently_donated_ids:
            assert c.eligible is True, f"{c.id} was excluded by a donor-domain rule in the shift domain"
            assert c.ineligibility_reason is None


def test_donor_candidates_not_filtered_by_shift_skill_rule():
    """Donor candidates carry no `skills` data at all. Reading the donor
    registry in the donor domain must never apply a shift qualification
    check to them."""
    registry = load_registry_csv(SAMPLE_DONOR_CSV)
    candidates = registry.candidates(RegistryDomain.DONOR, today=TODAY)
    assert len(candidates) == len(registry)
    # Eligibility here is governed only by recency/coordinator flag, never
    # by skills (which none of these people have).
    for person, candidate in zip(registry.all(), candidates):
        expected_eligible, _ = person.donor_eligibility(56, TODAY)
        assert candidate.eligible == expected_eligible


def test_shift_domain_filters_incompatible_shift_candidates():
    """A shift candidate missing the required skill, or whose availability
    window doesn't overlap the shift, is flagged ineligible -- not silently
    dispatched, not silently included in the eligible pool."""
    registry = load_registry_csv(SAMPLE_SHIFT_CSV)
    shift_start = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    shift_end = datetime(2026, 9, 12, 20, 0, tzinfo=timezone.utc)
    candidates = registry.candidates(
        RegistryDomain.SHIFT, required_skill="RN", shift_start=shift_start, shift_end=shift_end, today=TODAY
    )
    by_id = {c.id: c for c in candidates}

    assert by_id["s001"].eligible is True  # RN, window overlaps
    assert by_id["s003"].eligible is True  # RN, window overlaps

    assert by_id["s002"].eligible is False  # EMT, not RN
    assert "skill" in by_id["s002"].ineligibility_reason.lower()

    assert by_id["s006"].eligible is False  # no skills recorded at all
    assert by_id["s005"].eligible is False  # RN missing AND window already passed


def test_missing_availability_fails_closed_for_shift():
    """A shift candidate with the right skill but no recorded availability
    window can't be verified as available -- must fail closed, not be
    silently treated as available."""
    person = Person(
        id="x1", name="No Window", phone="+15550109999", timezone="UTC",
        skills=["RN"], shift_available_from=None, shift_available_until=None,
    )
    shift_start = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    shift_end = datetime(2026, 9, 12, 20, 0, tzinfo=timezone.utc)
    eligible, reason = person.shift_eligibility("RN", shift_start, shift_end)
    assert eligible is False
    assert "availability" in reason.lower()


def test_donor_coordinator_flag_overrides_recency_both_ways():
    """A coordinator-set donor_eligible flag is respected verbatim, even
    when it disagrees with the recency heuristic -- mobilize does not
    second-guess a human's clinical-adjacent decision with its own math."""
    recently_donated = Person(
        id="p1", name="Recent", phone="+15550100001", timezone="UTC",
        last_donation=date(2026, 9, 1),  # 11 days before TODAY -- fails 56-day recency
        donor_eligible=True, donor_eligible_source="coordinator",
    )
    eligible, reason = recently_donated.donor_eligibility(56, TODAY)
    assert eligible is True
    assert reason is None

    long_ago_but_flagged_ineligible = Person(
        id="p2", name="Flagged out", phone="+15550100002", timezone="UTC",
        last_donation=date(2020, 1, 1),  # recency would easily pass
        donor_eligible=False, donor_eligible_source="coordinator",
    )
    eligible, reason = long_ago_but_flagged_ineligible.donor_eligibility(56, TODAY)
    assert eligible is False
    assert "coordinator" in reason.lower()


def test_donor_eligibility_falls_back_to_recency_when_unreviewed_and_says_so():
    """No coordinator flag on file: eligibility falls back to the recency
    heuristic, and the source clearly says it is unreviewed, not a
    coordinator decision -- see registry.py's Person.donor_eligibility."""
    person = Person(
        id="p3", name="Unreviewed", phone="+15550100003", timezone="UTC",
        last_donation=date(2026, 9, 1),  # 11 days before TODAY -- fails recency
    )
    assert person.donor_eligible_source == "system_default_unreviewed"
    eligible, reason = person.donor_eligibility(56, TODAY)
    assert eligible is False
    assert "not a clinical decision" in reason.lower()
