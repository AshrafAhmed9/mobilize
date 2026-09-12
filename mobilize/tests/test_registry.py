from __future__ import annotations

from datetime import date

import pytest

from mobilize.core.registry import (
    AttendanceLog,
    AttendanceStatus,
    Registry,
    RegistryError,
    load_registry_csv,
    load_registry_json,
    record_attendance,
    record_outcomes,
    save_registry_json,
)
from mobilize.core.types import CallOutcome, CallResult


def _write_csv(tmp_path, content: str):
    path = tmp_path / "registry.csv"
    path.write_text(content)
    return path


def test_load_registry_csv_minimal(tmp_path):
    path = _write_csv(tmp_path, "name,phone,timezone\nAsha Rao,+15550101001,Asia/Kolkata\n")
    registry = load_registry_csv(path)
    assert len(registry) == 1
    # Auto-generated ids are now phone-derived, not row-number-based (see
    # test_registry_validation.py for why), so look the person up by
    # content rather than assuming a specific id string.
    person = registry.all()[0]
    assert person.name == "Asha Rao"
    assert person.timezone == "Asia/Kolkata"
    # No history -> defaults, not zero and not crashing.
    assert person.accept_rate == 0.5


def test_load_registry_csv_full_columns(tmp_path):
    path = _write_csv(tmp_path, (
        "id,name,phone,timezone,last_donation,distance_km,accept_rate,showup_rate\n"
        "x1,Karan Mehta,+15550101002,Asia/Kolkata,2026-05-01,4.5,0.7,0.6\n"
    ))
    registry = load_registry_csv(path)
    person = registry.get("x1")
    assert person.last_donation == date(2026, 5, 1)
    assert person.distance_km == 4.5
    assert person.accept_rate == 0.7


def test_load_registry_csv_missing_required_column(tmp_path):
    path = _write_csv(tmp_path, "name,phone\nAsha Rao,+15550101001\n")
    with pytest.raises(RegistryError, match="timezone"):
        load_registry_csv(path)


def test_load_registry_csv_missing_value_in_row(tmp_path):
    path = _write_csv(tmp_path, "name,phone,timezone\n,+15550101001,Asia/Kolkata\n")
    with pytest.raises(RegistryError, match="Row 2"):
        load_registry_csv(path)


def test_load_registry_csv_bad_date(tmp_path):
    path = _write_csv(tmp_path, (
        "name,phone,timezone,last_donation\nAsha Rao,+15550101001,Asia/Kolkata,not-a-date\n"
    ))
    with pytest.raises(RegistryError, match="last_donation"):
        load_registry_csv(path)


def test_load_registry_csv_no_such_file(tmp_path):
    with pytest.raises(RegistryError, match="No such file"):
        load_registry_csv(tmp_path / "does_not_exist.csv")


def test_load_registry_csv_empty_rows(tmp_path):
    path = _write_csv(tmp_path, "name,phone,timezone\n")
    with pytest.raises(RegistryError, match="no rows"):
        load_registry_csv(path)


def test_person_eligibility_by_recency():
    from mobilize.core.registry import Person

    recent = Person(id="a", name="A", phone="+15550101001", timezone="UTC", last_donation=date(2026, 7, 20))
    long_ago = Person(id="b", name="B", phone="+15550101002", timezone="UTC", last_donation=date(2026, 1, 1))
    never = Person(id="c", name="C", phone="+15550101003", timezone="UTC")

    today = date(2026, 8, 6)
    assert not recent.is_eligible(56, today)   # 17 days ago, too recent
    assert long_ago.is_eligible(56, today)      # well past the window
    assert never.is_eligible(56, today)         # no record -> treated as eligible


def test_registry_to_candidates_carries_timezone_and_eligibility():
    path_content = "name,phone,timezone,last_donation\nAsha,+15550101001,Asia/Kolkata,2026-01-01\n"
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        from pathlib import Path
        p = Path(tmp) / "r.csv"
        p.write_text(path_content)
        registry = load_registry_csv(p)
    candidates = registry.candidates(min_days_between_donations=56, today=date(2026, 8, 6))
    assert len(candidates) == 1
    assert candidates[0].timezone == "Asia/Kolkata"
    assert candidates[0].eligible is True


def test_save_and_load_registry_json_round_trip(tmp_path):
    csv_path = _write_csv(tmp_path, (
        "id,name,phone,timezone,last_donation,distance_km,accept_rate,showup_rate\n"
        "x1,Karan Mehta,+15550101002,Asia/Kolkata,2026-05-01,4.5,0.7,0.6\n"
    ))
    registry = load_registry_csv(csv_path)
    json_path = tmp_path / "registry.json"
    save_registry_json(registry, json_path)

    reloaded = load_registry_json(json_path)
    person = reloaded.get("x1")
    assert person.name == "Karan Mehta"
    assert person.last_donation == date(2026, 5, 1)
    assert person.accept_rate == 0.7


def test_load_registry_json_missing_file_returns_empty():
    from pathlib import Path
    registry = load_registry_json(Path("/tmp/definitely_does_not_exist_mobilize.json"))
    assert len(registry) == 0


def test_record_outcomes_nudges_accept_rate_up_on_firm_yes():
    csv_content = "id,name,phone,timezone,accept_rate,showup_rate\nx1,A,+15550101001,UTC,0.5,0.5\n"
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "r.csv"
        p.write_text(csv_content)
        registry = load_registry_csv(p)

    result = CallResult(
        call_id="c1", candidate_id="x1", outcome=CallOutcome.FIRM_YES,
        commitment_score=0.9, stated_yes=True, evidence="leaving now",
    )
    updated = record_outcomes(registry, [result])
    assert "x1" in updated
    person = registry.get("x1")
    assert person.accept_rate > 0.5  # moved toward accepted -- genuinely observed
    # Regression guard for the conflation bug this task fixes: a predicted
    # commitment score must NEVER move showup_rate. Only a real recorded
    # attendance observation may do that (see test_record_attendance_*).
    assert person.showup_rate == 0.5
    assert person.attendance_sample_count == 0
    # The commitment score still gets tracked -- just on a separate field
    # that's clearly labeled as a prediction, not attendance.
    assert person.predicted_commitment_estimate is not None
    assert person.predicted_commitment_estimate > 0.5
    assert person.times_called == 1


def test_record_outcomes_nudges_down_on_decline():
    csv_content = "id,name,phone,timezone,accept_rate\nx1,A,+15550101001,UTC,0.5\n"
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "r.csv"
        p.write_text(csv_content)
        registry = load_registry_csv(p)

    result = CallResult(
        call_id="c1", candidate_id="x1", outcome=CallOutcome.NO,
        commitment_score=0.0, stated_yes=False, evidence="can't make it",
    )
    record_outcomes(registry, [result])
    assert registry.get("x1").accept_rate < 0.5


def test_record_outcomes_no_answer_does_not_move_accept_rate():
    csv_content = "id,name,phone,timezone,accept_rate\nx1,A,+15550101001,UTC,0.5\n"
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "r.csv"
        p.write_text(csv_content)
        registry = load_registry_csv(p)

    result = CallResult(
        call_id="c1", candidate_id="x1", outcome=CallOutcome.NO_ANSWER,
        commitment_score=0.0, stated_yes=False, evidence="",
    )
    record_outcomes(registry, [result])
    # Not reached, so accept_rate is unmoved -- an unanswered call says
    # nothing about willingness to help.
    assert registry.get("x1").accept_rate == 0.5
    assert registry.get("x1").times_called == 1


def test_record_outcomes_unknown_candidate_id_ignored_gracefully():
    registry = Registry()
    result = CallResult(
        call_id="c1", candidate_id="nonexistent", outcome=CallOutcome.FIRM_YES,
        commitment_score=0.9, stated_yes=True, evidence="",
    )
    updated = record_outcomes(registry, [result])
    assert updated == []


def test_record_attendance_ground_truth_overrides_toward_observed(tmp_path):
    csv_content = "id,name,phone,timezone,showup_rate\nx1,A,+15550101001,UTC,0.9\n"
    p = _write_csv(tmp_path, csv_content)
    registry = load_registry_csv(p)
    assert registry.get("x1").showup_rate_source == "imported"

    log = AttendanceLog(tmp_path / "attendance.jsonl")
    record_attendance(registry, log, "mob1", "x1", AttendanceStatus.DID_NOT_ARRIVE)
    person = registry.get("x1")
    assert person.showup_rate == 0.0  # one observation, zero arrivals
    assert person.showup_rate_source == "observed"
    assert person.attendance_sample_count == 1


def test_record_outcomes_never_moves_showup_rate_even_across_many_calls(tmp_path):
    """The bug this task fixes, made explicit: no sequence of predicted
    commitment scores -- however many, however high -- should ever move
    showup_rate. Only record_attendance may do that."""
    csv_content = "id,name,phone,timezone\nx1,A,+15550101001,UTC\n"
    registry = load_registry_csv(_write_csv(tmp_path, csv_content))
    for _ in range(10):
        result = CallResult(
            call_id="c1", candidate_id="x1", outcome=CallOutcome.FIRM_YES,
            commitment_score=0.99, stated_yes=True, evidence="leaving now",
        )
        record_outcomes(registry, [result])
    assert registry.get("x1").showup_rate == 0.5
    assert registry.get("x1").attendance_sample_count == 0


def test_record_attendance_duplicate_click_is_idempotent(tmp_path):
    """Clicking 'arrived' twice for the same person/mobilization must not
    count as two observations."""
    registry = load_registry_csv(_write_csv(tmp_path, "id,name,phone,timezone\nx1,A,+15550101001,UTC\n"))
    log = AttendanceLog(tmp_path / "attendance.jsonl")
    record_attendance(registry, log, "mob1", "x1", AttendanceStatus.ARRIVED)
    record_attendance(registry, log, "mob1", "x1", AttendanceStatus.ARRIVED)
    person = registry.get("x1")
    assert person.attendance_sample_count == 1
    assert person.showup_rate == 1.0
    assert len(log.history_for("mob1", "x1")) == 2  # both writes kept for audit
    assert len(log.current_for_person("x1")) == 1  # but only one current observation


def test_record_attendance_correction_replaces_not_adds(tmp_path):
    """A coordinator who records 'arrived' and then corrects it to
    'did_not_arrive' must end up with one sample counted the new way, not
    two samples averaged together."""
    registry = load_registry_csv(_write_csv(tmp_path, "id,name,phone,timezone\nx1,A,+15550101001,UTC\n"))
    log = AttendanceLog(tmp_path / "attendance.jsonl")
    record_attendance(registry, log, "mob1", "x1", AttendanceStatus.ARRIVED)
    assert registry.get("x1").showup_rate == 1.0

    corrected = record_attendance(registry, log, "mob1", "x1", AttendanceStatus.DID_NOT_ARRIVE)
    person = registry.get("x1")
    assert person.attendance_sample_count == 1  # still one sample, not two
    assert person.showup_rate == 0.0  # fully replaced, not averaged (would be 0.5)
    assert corrected.corrected_from == AttendanceStatus.ARRIVED.value

    history = log.history_for("mob1", "x1")
    assert len(history) == 2
    assert history[0].status == AttendanceStatus.ARRIVED.value
    assert history[1].status == AttendanceStatus.DID_NOT_ARRIVE.value
    assert history[1].corrected_from == AttendanceStatus.ARRIVED.value


def test_record_attendance_unknown_is_not_treated_as_no_show(tmp_path):
    registry = load_registry_csv(_write_csv(tmp_path, "id,name,phone,timezone\nx1,A,+15550101001,UTC\n"))
    log = AttendanceLog(tmp_path / "attendance.jsonl")
    record_attendance(registry, log, "mob1", "x1", AttendanceStatus.UNKNOWN)
    person = registry.get("x1")
    assert person.attendance_sample_count == 0  # not counted as observed at all
    assert person.showup_rate == 0.5  # untouched default, not nudged toward 0

    record_attendance(registry, log, "mob2", "x1", AttendanceStatus.PENDING)
    person = registry.get("x1")
    assert person.attendance_sample_count == 0
    assert person.showup_rate == 0.5


def test_record_attendance_unknown_person_returns_none(tmp_path):
    registry = load_registry_csv(_write_csv(tmp_path, "id,name,phone,timezone\nx1,A,+15550101001,UTC\n"))
    log = AttendanceLog(tmp_path / "attendance.jsonl")
    assert record_attendance(registry, log, "mob1", "not-a-real-id", AttendanceStatus.ARRIVED) is None


def test_attendance_log_reload_shows_same_evidence(tmp_path):
    """State must be persisted, not just held in memory -- a fresh
    AttendanceLog over the same file must see identical evidence."""
    registry = load_registry_csv(_write_csv(tmp_path, "id,name,phone,timezone\nx1,A,+15550101001,UTC\n"))
    log_path = tmp_path / "attendance.jsonl"
    log = AttendanceLog(log_path)
    record_attendance(registry, log, "mob1", "x1", AttendanceStatus.ARRIVED)
    record_attendance(registry, log, "mob2", "x1", AttendanceStatus.DID_NOT_ARRIVE)

    reloaded = AttendanceLog(log_path)
    assert reloaded.current("mob1", "x1").status == AttendanceStatus.ARRIVED.value
    assert reloaded.current("mob2", "x1").status == AttendanceStatus.DID_NOT_ARRIVE.value
    assert len(reloaded.current_for_person("x1")) == 2

    # And a fresh registry recomputed against the reloaded log matches too.
    fresh_registry = load_registry_csv(_write_csv(tmp_path, "id,name,phone,timezone\nx1,A,+15550101001,UTC\n"))
    from mobilize.core.registry import _recompute_showup_rate
    _recompute_showup_rate(fresh_registry.get("x1"), reloaded)
    assert fresh_registry.get("x1").showup_rate == 0.5  # 1 of 2 arrived
    assert fresh_registry.get("x1").attendance_sample_count == 2


def test_attendance_log_rejects_unknown_status(tmp_path):
    log = AttendanceLog(tmp_path / "attendance.jsonl")
    with pytest.raises(ValueError):
        log.record("mob1", "x1", "definitely_showed_up")


def test_nudge_stays_within_bounds():
    from mobilize.core.registry import _nudge

    result = _nudge(0.95, 1.0, 0.9)
    assert result <= 0.98
    result = _nudge(0.05, 0.0, 0.9)
    assert result >= 0.02
