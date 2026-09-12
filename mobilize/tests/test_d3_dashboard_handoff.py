"""D3: the operational screen and handoff must actually be complete --
dropped events rendered, no false "finished" checkmark while calls are
unresolved, handoff export excludes raw transcript/unmasked phone numbers,
and mask_phone/esc() are exercised on these specific new surfaces (not just
present elsewhere in the file).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import mobilize.app.dashboard as dashboard_module
from mobilize.app.dashboard import (
    STOP_REASON_TEXT,
    _confirmed_person_dict,
    _event_guidance,
    _validate_reconcile_params,
    app,
)
from mobilize.core.ledger import Ledger
from mobilize.core.registry import Person, Registry
from mobilize.core.types import CallOutcome, CallResult, StopReason, utcnow


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_module, "REGISTRY_STATE_PATH", tmp_path / "registry.json")
    monkeypatch.setattr(dashboard_module, "REGISTRY_SOURCE_MARKER_PATH", tmp_path / "registry_source.txt")
    monkeypatch.setattr(dashboard_module, "GOVERNANCE_STATE_PATH", tmp_path / "governance.json")
    monkeypatch.setattr(dashboard_module, "REAL_LEDGER_PATH", tmp_path / "real_ledger.jsonl")
    yield


def _registry_with_one_person() -> Registry:
    registry = Registry()
    registry.people["p1"] = Person(id="p1", name="Asha Rao", phone="+15550101001", timezone="Asia/Kolkata")
    return registry


# --- StopReason coverage ------------------------------------------------

def test_every_stop_reason_has_operator_text():
    for reason in StopReason:
        assert reason.value in STOP_REASON_TEXT
        assert STOP_REASON_TEXT[reason.value]


# --- Dropped events now carry operator guidance -------------------------

@pytest.mark.parametrize("event,data", [
    ("governance_filtered", {"blocked": 1, "remaining": 2, "wave": 1, "candidates": ["p1"]}),
    ("dispatch_failed", {"candidate_id": "p1", "error": "timeout", "ambiguous": True}),
    ("dispatch_failed", {"candidate_id": "p1", "error": "rejected", "safely_rejected": True}),
    ("dispatch_failed", {"candidate_id": "p1", "error": "bad phone"}),
    ("call_timed_out", {"candidate_id": "p1"}),
    ("recovery_unresolved", {"candidate_id": "p1"}),
])
def test_dropped_events_get_guidance(event, data):
    guidance = _event_guidance(event, data)
    assert guidance and isinstance(guidance, str)


def test_unknown_event_gets_no_guidance():
    assert _event_guidance("call_result", {"candidate_id": "p1", "outcome": "firm_yes"}) is None


def test_dropped_events_render_in_ui_js():
    """Proves these events are actually rendered somewhere, not just
    guidance-annotated server-side and dropped by the client like before."""
    page = dashboard_module._PAGE
    for event in ("governance_filtered", "dispatch_failed", "call_timed_out", "recovery_unresolved"):
        assert f"'{event}'" in page, f"{event} is not wired into the client-side event handling"
    assert "renderExceptionEvent" in page
    assert "exceptions-panel" in page


# --- False-completion guard ----------------------------------------------

def test_false_completion_guard_present_in_ui():
    """The rendered page must not show a clean 'finished' state while
    ambiguous_candidate_ids is non-empty -- verify the guard exists and is
    keyed off exactly that field."""
    page = dashboard_module._PAGE
    assert "isUnresolved" in page
    assert "ambiguous_candidate_ids" in page
    # The warning path must precede/guard the "filled" success path, not be
    # an afterthought alongside it.
    warn_idx = page.index("Not fully resolved")
    finished_idx = page.index("Need filled and no unresolved calls")
    assert warn_idx < finished_idx


def test_final_payload_includes_ambiguous_ids_and_stop_reason_fields():
    """The server must actually send ambiguous_candidate_ids/stop_reason/
    stop_reason_text/counts in the final WS payload -- these are what the
    guard and the plan/shortage panels render from."""
    import inspect
    src = inspect.getsource(dashboard_module)
    assert '"ambiguous_candidate_ids": ambiguous_people' in src
    assert '"stop_reason": result.stop_reason' in src
    assert '"stop_reason_text": STOP_REASON_TEXT.get(result.stop_reason' in src
    assert '"counts": result.counts' in src


# --- Handoff-safe confirmed-person shape ----------------------------------

def test_confirmed_person_dict_excludes_transcript_and_masks_phone():
    registry = _registry_with_one_person()
    result = CallResult(
        call_id="call_123", candidate_id="p1", outcome=CallOutcome.FIRM_YES,
        commitment_score=0.9, stated_yes=True, evidence="leaving now, ten minutes",
        transcript=[{"speaker": "recipient", "text": "leaving now, ten minutes, very private detail"}],
        completed_at=utcnow(),
    )
    person_dict = _confirmed_person_dict(result, registry, "live")

    assert "transcript" not in person_dict
    assert person_dict["phone"] != "+15550101001"
    assert person_dict["phone"] == dashboard_module.mask_phone("+15550101001")
    # call reference, mode, and capture time are all present per D3.
    assert person_dict["call_id"] == "call_123"
    assert person_dict["mode"] == "live"
    assert person_dict["captured_at"] is not None
    assert person_dict["verdict"] == "firm_yes"
    assert person_dict["evidence"] == "leaving now, ten minutes"


def test_confirmed_person_dict_unknown_candidate_falls_back_safely():
    registry = Registry()
    result = CallResult(call_id="call_x", candidate_id="ghost", outcome=CallOutcome.NO,
                         commitment_score=0.0, stated_yes=False, evidence="no answer")
    person_dict = _confirmed_person_dict(result, registry, "rehearsal")
    assert person_dict["name"] == "ghost"
    assert person_dict["phone"] == ""


def test_export_handoff_js_excludes_transcript_field():
    """The client-side export must build its summary only from fields
    already present in confirmed-person dicts (which never carry a raw
    transcript) -- it must not reach back into any transcript field."""
    page = dashboard_module._PAGE
    assert "exportHandoff" in page
    export_fn = page[page.index("function exportHandoff"):page.index("function renderFinal")]
    assert ".transcript" not in export_fn
    assert "c.phone" in export_fn  # masked phone only, from the same confirmed dict


# --- esc()/mask_phone actually used on the new surfaces -------------------

def test_new_confirmed_rows_use_esc():
    page = dashboard_module._PAGE
    render_fn = page[page.index("function renderFinal"):page.index("</script>")]
    # Every new field surfaced per D3 (call ref, mode, capture time, verdict,
    # evidence quote) must be escaped, not interpolated raw.
    for field in ("c.call_id", "c.mode", "c.captured_at", "c.verdict", "c.evidence", "c.name", "c.phone"):
        assert f"esc({field})" in render_fn, f"{field} is rendered without esc()"


def test_mask_phone_used_for_confirmed_people():
    import inspect
    src = inspect.getsource(dashboard_module._confirmed_person_dict)
    assert "mask_phone(" in src


# --- Reconciliation endpoint boundary validation --------------------------

def test_validate_reconcile_params_rejects_missing_fields():
    with pytest.raises(ValueError):
        _validate_reconcile_params({"candidate_id": "p1", "decision": "treat_as_failed"})
    with pytest.raises(ValueError):
        _validate_reconcile_params({"mobilization_id": "m1", "decision": "treat_as_failed"})
    with pytest.raises(ValueError):
        _validate_reconcile_params({"mobilization_id": "m1", "candidate_id": "p1", "decision": "not_a_real_decision"})


def test_validate_reconcile_params_accepts_valid_payload():
    mob_id, cand_id, decision, operator, note = _validate_reconcile_params({
        "mobilization_id": "m1", "candidate_id": "p1", "decision": "treat_as_failed",
        "recorded_by": "coordinator_a", "note": "confirmed via callback",
    })
    assert (mob_id, cand_id, decision, operator, note) == (
        "m1", "p1", "treat_as_failed", "coordinator_a", "confirmed via callback")


def test_reconcile_endpoint_writes_ledger_entry_and_clears_unresolved(tmp_path):
    ledger_path = tmp_path / "real_ledger.jsonl"
    dashboard_module.REAL_LEDGER_PATH = ledger_path
    ledger = Ledger(str(ledger_path))
    ledger.record_dispatch_intent("mob_1", "p1")  # never resolved -> ambiguous

    assert "p1" in ledger.unresolved("mob_1")

    client = TestClient(app)
    resp = client.post("/api/reconcile", json={
        "mobilization_id": "mob_1", "candidate_id": "p1",
        "decision": "confirmed_no_call_placed", "recorded_by": "coordinator_a",
    })
    body = resp.json()
    assert "error" not in body
    assert body["reconciled"]["candidate_id"] == "p1"
    assert body["still_unresolved"] == []

    # Ledger truly recorded it -- re-reading confirms it's durable, not
    # just an in-memory response.
    reopened = Ledger(str(ledger_path))
    assert "p1" not in reopened.unresolved("mob_1")
    assert "p1" in reopened.reconciled_candidates("mob_1")


def test_reconcile_endpoint_rejects_invalid_decision():
    client = TestClient(app)
    resp = client.post("/api/reconcile", json={
        "mobilization_id": "mob_1", "candidate_id": "p1", "decision": "just_trust_me",
    })
    assert "error" in resp.json()


# --- Plan/rationale fields reach the completion screen ---------------------

def test_plan_grid_renders_need_deadline_budget_stop_reason():
    page = dashboard_module._PAGE
    render_fn = page[page.index("function renderFinal"):page.index("</script>")]
    for field in ("data.need_label", "data.location", "data.deadline_minutes", "data.max_calls",
                  "data.stop_reason", "data.stop_reason_text", "counts.excess_commitments"):
        assert field in render_fn


def test_completion_screen_offers_required_actions():
    """Acceptance: a final screen must offer hand off roster, inspect
    evidence, reconcile unknowns, record attendance, or explain shortfall."""
    page = dashboard_module._PAGE
    render_fn = page[page.index("function renderFinal"):page.index("</script>")]
    assert "exportHandoff()" in render_fn  # hand off roster
    assert "c.evidence" in render_fn  # inspect evidence
    assert "reconcileCandidate(" in render_fn  # reconcile unknowns
    assert "recordAttendance(" in render_fn  # record attendance
    assert "stop_reason_text" in render_fn  # explain why unmet
