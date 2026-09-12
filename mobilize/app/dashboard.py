"""The coordinator-facing product: load a registry, describe a need, watch
it fill, get an actionable list back. This is the thing a real donor
coordinator could actually use -- not a developer demo of the engine.

    python -m mobilize.app.dashboard
    open http://localhost:8731

Ships with a sample registry (mobilize/app/sample_data/sample_registry.csv)
so it works with zero setup. Runs entirely against the free simulator by
default; a real mobilization is a separate, explicit, confirmed action --
see registry_real_dispatch below -- never triggered by the same button that
runs the free preview.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from mobilize.core.dispatcher import mobilize
from mobilize.core.ids import derive_mobilization_id
from mobilize.core.ledger import Ledger
from mobilize.core.policy import GovernancePolicy, load_governance_state
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
from mobilize.core.types import Need, StopReason
from mobilize.core.validation import mask_phone
from mobilize.sim.fixture_transport import SCENARIOS, run_fixture_scenario
from mobilize.sim.population import generate_population
from mobilize.transports.simulated import SimulatedTransport

APP_DIR = Path(__file__).resolve().parent
SAMPLE_REGISTRY_CSV = APP_DIR / "sample_data" / "sample_registry.csv"
REGISTRY_STATE_PATH = Path("/tmp/mobilize_dashboard_registry.json")
# Separate from REGISTRY_STATE_PATH on purpose: the state file gets written
# automatically the first time the bundled sample is loaded, so its mere
# existence can't distinguish "sample, persisted" from "a coordinator
# actually uploaded their own list" -- that provenance is tracked here.
REGISTRY_SOURCE_MARKER_PATH = Path("/tmp/mobilize_dashboard_registry_source.txt")
GOVERNANCE_STATE_PATH = Path("/tmp/mobilize_dashboard_governance.json")
# Durable attendance audit trail -- separate file from REGISTRY_STATE_PATH on
# purpose, same reasoning as the ledger: the registry snapshot is derived
# state (showup_rate is recomputed from this log), the log is the actual
# evidence, and evidence should never be reconstructable-but-not-kept.
ATTENDANCE_LOG_PATH = Path("/tmp/mobilize_dashboard_attendance.jsonl")
# The one durable ledger a real (non-rehearsal) run writes to -- named as a
# module constant, not an inline literal, so the reconciliation endpoint
# below opens the exact same file a live /ws/run wrote its dispatch_intent/
# dispatched/result entries to. Rehearsal runs use their own throwaway temp
# ledger (deleted when the connection closes) and are never reconcilable --
# there is nothing durable there to reconcile.
REAL_LEDGER_PATH = Path("/tmp/mobilize_dashboard_real_ledger.jsonl")

app = FastAPI(title="mobilize")
_attendance_log: AttendanceLog | None = None

# D3: human-readable text for each StopReason code (mirrors the pattern
# types.py already uses for DECISION_REASON_TEXT) -- a coordinator watching
# the dashboard needs to know WHY dispatch stopped, not just that it did.
# Kept here rather than in core/types.py: this is UI copy, not engine state,
# and D3's scope is the dashboard, not the dispatch policy.
STOP_REASON_TEXT: dict[str, str] = {
    StopReason.TARGET_MET.value: "The need was filled -- enough confirmations came in.",
    StopReason.DEADLINE.value: "The deadline passed before the need was filled.",
    StopReason.BUDGET_EXHAUSTED.value: "The call budget (max calls) was used up before the need was filled.",
    StopReason.NO_ELIGIBLE_CANDIDATES.value: "Nobody eligible was left to call before the need was filled.",
    StopReason.UNRESOLVED_DISPATCH_OR_CALL.value: (
        "One or more calls are in an unknown state (a timeout or connection error that may or may "
        "not have reached CALL-E) -- dispatch stopped rather than risk a double-call. Reconcile the "
        "listed candidate(s) before treating this run as finished."
    ),
    StopReason.OPERATOR_PAUSE.value: "Paused by an operator.",
}

# D3: operator-readable guidance for the events the dashboard used to drop
# on the floor -- each of these means something a coordinator needs to act
# on, not just a debug line. Matched by exact event name plus, for
# dispatch_failed (which covers three different branches in dispatcher.py --
# a pre-flight ValueError, an explicit CALL-E rejection, and a genuinely
# ambiguous network failure -- see dispatcher.py's `_dispatch_one`), by the
# `safely_rejected`/`ambiguous` flags already present on that event's payload.
def _event_guidance(event: str, data: dict) -> str | None:
    if event == "governance_filtered":
        return ("These candidates became blocked by policy (do-not-call, cooldown, fatigue, or "
                "calling hours) between planning and dispatch -- they were dropped from this wave, "
                "not called.")
    if event == "dispatch_failed":
        if data.get("ambiguous"):
            return ("This call's dispatch raised an error that could mean CALL-E already accepted "
                     "it before the error surfaced -- it may still be live. Reconcile before "
                     "dispatching further to this person.")
        if data.get("safely_rejected"):
            return "CALL-E explicitly rejected this dispatch (auth, rate limit, unsupported destination, or policy) -- no call was placed."
        return "This dispatch failed before any network call was made (e.g. an invalid phone number) -- no call was placed."
    if event == "call_timed_out":
        return ("This call never returned a terminal result before the poll timeout -- it may "
                "still be live. Reconcile before treating this mobilization as finished.")
    if event == "recovery_unresolved":
        return ("A call from an earlier, interrupted run for this mobilization is still unresolved "
                "-- reconcile it before dispatching further waves.")
    if event == "recovery_reconciled":
        return "This previously-unresolved call has already been reconciled by an operator and is no longer blocking dispatch."
    if event == "recovering_in_flight":
        return "Resuming a prior run: checking on calls that were already dispatched before this process (re)started."
    return None


def _current_attendance_log() -> AttendanceLog:
    global _attendance_log
    if _attendance_log is None:
        _attendance_log = AttendanceLog(ATTENDANCE_LOG_PATH)
    return _attendance_log


def _current_registry() -> Registry:
    """The coordinator's working registry: whatever they've loaded and
    however outcomes have updated it since, persisted between page loads
    the same way a real tool would remember your list."""
    if REGISTRY_STATE_PATH.exists():
        registry = load_registry_json(REGISTRY_STATE_PATH)
        if len(registry):
            return registry
    registry = load_registry_csv(SAMPLE_REGISTRY_CSV)
    save_registry_json(registry, REGISTRY_STATE_PATH)
    if not REGISTRY_SOURCE_MARKER_PATH.exists():
        REGISTRY_SOURCE_MARKER_PATH.write_text("sample")
    return registry


def _registry_source_label() -> str:
    marker = REGISTRY_SOURCE_MARKER_PATH.read_text().strip() if REGISTRY_SOURCE_MARKER_PATH.exists() else "sample"
    return "your uploaded list" if marker == "uploaded" else "sample registry"


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _PAGE


@app.get("/api/registry")
async def get_registry() -> dict:
    registry = _current_registry()
    today = date.today()
    people = [
        {
            "id": p.id, "name": p.name, "phone": _mask(p.phone), "timezone": p.timezone,
            "days_since_donation": round(p.days_since_last_donation(today)),
            "eligible": p.is_eligible(56, today),
            "accept_rate": round(p.accept_rate, 2), "showup_rate": round(p.showup_rate, 2),
            "times_called": p.times_called,
            # Never display showup_rate on its own -- always alongside where
            # it came from (nothing recorded yet / a CSV's own prior history /
            # this product's own observations) and how much evidence backs
            # it, so "50%" doesn't get read as a confident measurement when
            # it's really just the untouched default.
            "showup_rate_source": p.showup_rate_source,
            "attendance_sample_count": p.attendance_sample_count,
            "predicted_commitment_estimate": (
                round(p.predicted_commitment_estimate, 2) if p.predicted_commitment_estimate is not None else None
            ),
        }
        for p in sorted(registry.all(), key=lambda x: -x.to_candidate().prior_score())
    ]
    return {"count": len(registry), "source": _registry_source_label(), "people": people}


@app.post("/api/registry/upload")
async def upload_registry(payload: dict) -> dict:
    """Accepts raw CSV text pasted or uploaded from the browser -- no
    command line required. This is the actual product surface: a
    coordinator's own spreadsheet becomes a working registry in one step."""
    csv_text = payload.get("csv", "")
    # A unique file per request, not a fixed shared path -- two uploads
    # landing close together (two tabs, a double-click) would otherwise
    # race: request A writes its CSV, before A reads it back request B
    # overwrites the same path with different data, and A silently loads
    # B's registry instead of its own.
    import tempfile

    fd, tmp_name = tempfile.mkstemp(suffix=".csv", prefix="mobilize_upload_")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(csv_text)
        try:
            registry = load_registry_csv(tmp_path)
        except RegistryError as exc:
            return {"error": str(exc)}
    finally:
        tmp_path.unlink(missing_ok=True)
    save_registry_json(registry, REGISTRY_STATE_PATH)
    REGISTRY_SOURCE_MARKER_PATH.write_text("uploaded")
    return {"count": len(registry), "message": f"Loaded {len(registry)} people."}


@app.post("/api/registry/reset")
async def reset_registry() -> dict:
    """WARNING (see mobilize/artifacts/e1_platform_and_reset.md): this
    unconditionally deletes REGISTRY_STATE_PATH and falls back to the
    built-in sample registry, regardless of whether the current registry is
    the sample data or a real list a coordinator uploaded via
    /api/registry/upload -- REGISTRY_SOURCE_MARKER_PATH records provenance
    ("uploaded" vs sample) but this endpoint does not consult it before
    deleting. There is no server-side undo. Before calling this against a
    real operational registry, GET /api/registry and save the response, or
    keep the original CSV/JSON you uploaded -- it is the only backup. This
    endpoint is intended for resetting a demo to the sample data between
    judge runs, not for clearing real data.
    """
    if REGISTRY_STATE_PATH.exists():
        REGISTRY_STATE_PATH.unlink()
    if REGISTRY_SOURCE_MARKER_PATH.exists():
        REGISTRY_SOURCE_MARKER_PATH.unlink()
    registry = _current_registry()
    return {"count": len(registry), "message": "Reset to the sample registry."}


@app.get("/api/fixture/{scenario}")
async def run_fixture_endpoint(scenario: str) -> dict:
    """E1: run one deterministic, API-shaped fixture scenario through the
    real mobilize() dispatcher and the real production _to_call_result
    translation ladder (transports/calle.py) -- the same shared runner used
    by `python -m mobilize.app.cli --fixture <scenario>` and the MCP
    `mobilize_fixture` tool, so the identical scenario is reproducible from
    all three surfaces. See mobilize/sim/fixture_transport.py. No network
    call is possible: FixtureTransport never constructs an HTTP client.
    `scenario` must be one of: 'success', 'refusal', 'opt_out', 'ambiguity'.
    """
    if scenario not in SCENARIOS:
        return {"error": f"Unknown scenario {scenario!r}. Known: {sorted(SCENARIOS)}"}
    return await run_fixture_scenario(scenario)


_ATTENDANCE_STATUSES = {s.value for s in AttendanceStatus}


@app.post("/api/attendance")
async def record_attendance_action(payload: dict) -> dict:
    """The coordinator explicitly recording what actually happened for one
    person on one mobilization: arrived, did_not_arrive, pending, or unknown,
    with a server-assigned timestamp. This is the ONLY endpoint that can move
    a person's showup_rate -- rehearsal runs, unanswered calls, and predicted
    commitment scores never reach this path (see record_outcomes in
    registry.py). Sending the same mobilization_id/person_id again is a
    correction, not a second sample -- see AttendanceLog/record_attendance.
    """
    mobilization_id = str(payload.get("mobilization_id", "")).strip()
    person_id = str(payload.get("person_id", "")).strip()
    status = str(payload.get("status", "")).strip()
    recorded_by = payload.get("recorded_by")
    if not mobilization_id:
        return {"error": "mobilization_id is required."}
    if not person_id:
        return {"error": "person_id is required."}
    if status not in _ATTENDANCE_STATUSES:
        return {"error": f"status must be one of: {', '.join(sorted(_ATTENDANCE_STATUSES))}."}

    registry = _current_registry()
    log = _current_attendance_log()
    record = record_attendance(registry, log, mobilization_id, person_id, status,
                                recorded_by=str(recorded_by) if recorded_by else None)
    if record is None:
        return {"error": f"No such person in the current registry: {person_id!r}."}
    save_registry_json(registry, REGISTRY_STATE_PATH)

    person = registry.get(person_id)
    return {
        "recorded": {
            "mobilization_id": record.mobilization_id, "person_id": record.person_id,
            "status": record.status, "recorded_at": record.recorded_at,
            "corrected_from": record.corrected_from,
        },
        "showup_rate": round(person.showup_rate, 2),
        "showup_rate_source": person.showup_rate_source,
        "attendance_sample_count": person.attendance_sample_count,
    }


@app.get("/api/attendance/{person_id}")
async def get_attendance_history(person_id: str) -> dict:
    """Full audit trail for one person, across every mobilization -- every
    observation and correction ever recorded, not just the current state."""
    log = _current_attendance_log()
    current = sorted(log.current_for_person(person_id), key=lambda r: r.recorded_at)
    mobilization_ids = [r.mobilization_id for r in current]
    history = [r for mob_id in mobilization_ids for r in log.history_for(mob_id, person_id)]
    as_dict = lambda r: {"mobilization_id": r.mobilization_id, "status": r.status,
                          "recorded_at": r.recorded_at, "corrected_from": r.corrected_from}
    return {
        "person_id": person_id,
        "current": [as_dict(r) for r in current],
        "history": [as_dict(r) for r in history],
    }


_RECONCILE_DECISIONS = {
    "confirmed_no_call_placed", "confirmed_call_placed_no_answer",
    "confirmed_call_placed_declined", "confirmed_call_placed_agreed", "treat_as_failed",
}


def _validate_reconcile_params(payload: dict) -> tuple[str, str, str, str | None, str | None]:
    """Server-side validation for /api/reconcile, at the boundary, the same
    way _validate_run_params guards /ws/run -- this writes a durable,
    attributed ledger entry (Ledger.record_reconciliation), so a malformed
    or empty field here would become bad permanent audit history."""
    mobilization_id = str(payload.get("mobilization_id", "")).strip()
    candidate_id = str(payload.get("candidate_id", "")).strip()
    decision = str(payload.get("decision", "")).strip()
    operator = payload.get("recorded_by")
    note = payload.get("note")
    if not mobilization_id:
        raise ValueError("mobilization_id is required.")
    if not candidate_id:
        raise ValueError("candidate_id is required.")
    if decision not in _RECONCILE_DECISIONS:
        raise ValueError(f"decision must be one of: {', '.join(sorted(_RECONCILE_DECISIONS))}.")
    if operator is not None and not isinstance(operator, str):
        raise ValueError("recorded_by must be a string.")
    if note is not None and not isinstance(note, str):
        raise ValueError("note must be a string.")
    return mobilization_id, candidate_id, decision, (operator or None), (note or None)


@app.post("/api/reconcile")
async def reconcile_ambiguous_call(payload: dict) -> dict:
    """The sanctioned way for a coordinator to unblock an ambiguous/unresolved
    call from the dashboard itself -- writes an explicit, attributed
    Ledger.record_reconciliation entry (never a silent retry, never a
    deletion of the original ambiguous entry) and returns the mobilization's
    remaining unresolved set so the UI can update without a full page reload.
    Only meaningful for a LIVE mobilization_id: rehearsal ledgers are
    throwaway temp files deleted when their run ends, so there is nothing
    durable here to reconcile against for a rehearsal id."""
    try:
        mobilization_id, candidate_id, decision, operator, note = _validate_reconcile_params(payload)
    except ValueError as exc:
        return {"error": str(exc)}

    ledger = Ledger(str(REAL_LEDGER_PATH))
    ledger.record_reconciliation(mobilization_id, candidate_id, decision=decision,
                                  operator=operator or "dashboard_operator", note=note)
    remaining_unresolved = sorted(ledger.unresolved(mobilization_id))
    registry = _current_registry()
    return {
        "reconciled": {"mobilization_id": mobilization_id, "candidate_id": candidate_id, "decision": decision},
        "still_unresolved": [
            {"id": cid, "name": registry.get(cid).name if registry.get(cid) else cid}
            for cid in remaining_unresolved
        ],
    }


def _confirmed_person_dict(result, registry: Registry, mode: str) -> dict:
    """The handoff-safe view of one confirmed CallResult: enough for a
    coordinator to act on (who, how sure, what they said, which call, when),
    nothing a privacy overreach would include. Deliberately excludes
    `result.transcript` (the full raw conversation) and any unmasked phone
    number -- see D3's export requirement. Used by both the /ws/run 'final'
    payload and (indirectly, via the same shape) the client-side export."""
    person = registry.get(result.candidate_id)
    return {
        "id": result.candidate_id,
        "name": person.name if person else result.candidate_id,
        "phone": mask_phone(person.phone) if person else "",
        "commitment": round(result.commitment_score, 2),
        "evidence": result.evidence,
        "verdict": result.outcome.value,
        "call_id": result.call_id,
        "mode": mode,
        "captured_at": result.completed_at.isoformat() if result.completed_at else None,
        # D4: a stated ETA is a claim from the call transcript, not an
        # observation -- never treated as a confirmed arrival. No transport
        # currently populates these (see CallResult.stated_eta); when one
        # does, an ETA with no evidence, or none at all, must read as
        # "needs operator review", never as a silent on-time confirmation.
        "stated_eta": result.stated_eta,
        "stated_eta_evidence": result.stated_eta_evidence,
        "eta_needs_review": result.stated_eta is None or not result.stated_eta_evidence,
    }


def _origin_is_trusted(ws: WebSocket, expected_host: str, port: int) -> bool:
    """Reject cross-origin WebSocket connections -- a page from any other
    origin (e.g. one opened in another tab, or a malicious remote page if
    this instance is ever reachable over a network) can otherwise drive
    this endpoint exactly like the app's own JavaScript can, since a
    WebSocket handshake is not itself same-origin-restricted by the
    browser. Standard mitigation used by local-first dev servers (Jupyter
    does the same check)."""
    origin = ws.headers.get("origin", "")
    allowed = {f"http://{expected_host}:{port}", f"http://localhost:{port}", f"http://127.0.0.1:{port}"}
    return origin in allowed


def _validate_run_params(params: dict) -> tuple[str, int, float, int, str, bool, bool]:
    """Server-side validation of client-supplied run parameters. The
    browser's own inputs are typed (number/checkbox), but the WebSocket
    endpoint has no schema enforcement of its own -- any client (or a
    crafted message from a trusted origin's JS console) can send whatever
    JSON it wants. Two concrete risks this closes:

    1. Python's bool() coerces ANY non-empty string to True, including the
       string "false" -- bool("false") is True. `confirm` and `simulate`
       gate real-money call dispatch (see registry_real_dispatch above), so
       silently treating a malformed confirm:"false" as confirm:true would
       be a real-dispatch bypass. Both flags must be actual JSON booleans.
    2. need_count/max_calls/deadline_minutes flow straight into Need and
       then into mobilize()'s wave-sizing math with no bounds check. A
       non-positive need_count makes `len(confirmed) >= need.count` true
       before a single call is placed (dispatcher.py reports the need as
       filled with zero confirmations), and a non-positive max_calls or
       deadline breaks the budget/deadline math the same way. These are
       rejected here, at the boundary, rather than by changing dispatcher
       policy itself.
    """
    if not isinstance(params.get("simulate", True), bool):
        raise ValueError("simulate must be a boolean.")
    if not isinstance(params.get("confirm", False), bool):
        raise ValueError("confirm must be a boolean.")
    use_simulated_outcomes = params.get("simulate", True)
    confirmed_real = params.get("confirm", False)

    need_label = params.get("need_label", "Urgent help needed")
    if not isinstance(need_label, str) or not need_label.strip():
        raise ValueError("need_label must be a non-empty string.")

    location = params.get("location", "")
    if not isinstance(location, str):
        raise ValueError("location must be a string.")

    need_count_raw = params.get("need_count", 3)
    max_calls_raw = params.get("max_calls", 40)
    deadline_raw = params.get("deadline_minutes", 60)

    if isinstance(need_count_raw, bool) or not isinstance(need_count_raw, int) or need_count_raw < 1:
        raise ValueError("need_count must be a positive integer.")
    if isinstance(max_calls_raw, bool) or not isinstance(max_calls_raw, int) or max_calls_raw < 1:
        raise ValueError("max_calls must be a positive integer.")
    if isinstance(deadline_raw, bool) or not isinstance(deadline_raw, (int, float)) or deadline_raw <= 0:
        raise ValueError("deadline_minutes must be a positive number.")
    if need_count_raw > max_calls_raw:
        raise ValueError("need_count cannot exceed max_calls.")

    domain_raw = params.get("domain", "donor")
    if domain_raw not in ("donor", "shift"):
        raise ValueError("domain must be 'donor' or 'shift'.")

    required_skill = params.get("required_skill") or None
    if required_skill is not None and not isinstance(required_skill, str):
        raise ValueError("required_skill must be a string or null.")

    latest_useful_arrival_raw = params.get("latest_useful_arrival_minutes")
    if latest_useful_arrival_raw is not None:
        if isinstance(latest_useful_arrival_raw, bool) or not isinstance(latest_useful_arrival_raw, (int, float)) \
                or latest_useful_arrival_raw <= 0:
            raise ValueError("latest_useful_arrival_minutes must be a positive number or null.")

    return (need_label, need_count_raw, float(deadline_raw), max_calls_raw, location,
            use_simulated_outcomes, confirmed_real, domain_raw, required_skill,
            float(latest_useful_arrival_raw) if latest_useful_arrival_raw is not None else None)


@app.websocket("/ws/run")
async def run_mobilization(ws: WebSocket) -> None:
    await ws.accept()
    rehearsal_ledger_path: Path | None = None
    try:
        expected_host = os.environ.get("MOBILIZE_DASHBOARD_HOST", "127.0.0.1")
        port = int(os.environ.get("MOBILIZE_DASHBOARD_PORT", 8731))
        if not _origin_is_trusted(ws, expected_host, port):
            await ws.send_json({"event": "error", "data": {"message": "Rejected: untrusted origin."}})
            return

        params = await ws.receive_json()
        try:
            need_label, need_count, deadline_minutes, max_calls, location, \
                use_simulated_outcomes, confirmed_real, domain, required_skill, \
                latest_useful_arrival_minutes = _validate_run_params(params)
        except ValueError as exc:
            await ws.send_json({"event": "error", "data": {"message": str(exc)}})
            return

        registry = _current_registry()
        # D4: which eligibility rule applies depends on `domain`, selected
        # explicitly by the coordinator running this mobilization -- never
        # both, never guessed from what columns happen to be in the CSV.
        # The donor-domain 56-day recency rule is only ever evaluated when
        # domain == "donor"; a shift mobilization uses required_skill/
        # availability-window checks instead (see registry.py's
        # Person.shift_eligibility). Neither rule is applied to the other
        # domain's candidates.
        candidates = registry.candidates(
            domain,
            min_days_between_donations=56,
            required_skill=required_skill,
        )
        need = Need(label=need_label, count=need_count, deadline_minutes=deadline_minutes,
                    location=location, max_calls=max_calls, required_skill=required_skill,
                    latest_useful_arrival_minutes=latest_useful_arrival_minutes)

        # Real-call governance and durability, wired the same way as the
        # CLI and MCP entry points: persisted across invocations (a fresh
        # in-memory GovernanceState() every run would make DNC/cooldown/
        # fatigue tracking silently useless), and a deterministic
        # mobilization_id derived from the request itself rather than
        # id(ws) -- the latter is a NEW value on every reconnect, which
        # would generate a fresh idempotency key on every retry and defeat
        # crash-safe resumption exactly like the earlier CLI/MCP bug.
        phones_key = sorted(c.phone for c in candidates)
        if use_simulated_outcomes:
            # Rehearsal must never touch the operational registry, the real
            # governance file, or a ledger that persists between runs -- see
            # D1. `outcome_registry` is a private, session-local copy: the
            # simulator reads real accept/show-up priors off it for
            # realistic rehearsal, and record_outcomes below writes any
            # hypothetical learning onto this copy only, never onto
            # `registry` or REGISTRY_STATE_PATH. The ledger similarly gets a
            # fresh temp file per run (not the fixed accumulating path) so
            # a repeated rehearsal starts from the same idempotency state
            # and is deterministic after "reset" -- reset here just means
            # "this run never touched anything durable to begin with".
            import copy
            import tempfile

            outcome_registry = copy.deepcopy(registry)
            transport = _RegistryBackedSimulatedTransport(outcome_registry)
            fd, rehearsal_ledger_name = tempfile.mkstemp(suffix=".jsonl", prefix="mobilize_rehearsal_ledger_")
            os.close(fd)
            rehearsal_ledger_path = Path(rehearsal_ledger_name)
            ledger = Ledger(str(rehearsal_ledger_path))
            governance_state = governance_policy = governance_state_path = None
            mobilization_id = f"dash_sim_{id(ws)}"
        else:
            # The confirmation dialog in the browser is a UX nicety, not a
            # security boundary -- any client can send confirm:true anyway.
            # What actually stops an unintended real dispatch is (a) origin
            # checking above, (b) binding to localhost by default, and (c)
            # requiring this explicit field so the request is at minimum
            # unambiguous about intent, matching the MCP tool's confirm
            # requirement.
            if not confirmed_real:
                await ws.send_json({"event": "error", "data": {
                    "message": "Real dispatch requires confirm:true. No calls were placed."}})
                return
            from mobilize.transports.calle import CalleTransport
            transport = CalleTransport()
            ledger = Ledger(str(REAL_LEDGER_PATH))
            governance_state = load_governance_state(GOVERNANCE_STATE_PATH)
            governance_policy = GovernancePolicy()
            governance_state_path = GOVERNANCE_STATE_PATH
            mobilization_id = derive_mobilization_id(need_label, phones_key)

        loop = asyncio.get_event_loop()

        def on_progress(event: str, data: dict[str, Any]) -> None:
            safe_data = dict(data)
            if "candidate_id" in safe_data:
                person = registry.get(safe_data["candidate_id"])
                if person:
                    safe_data["name"] = person.name
            if "candidates" in safe_data:
                safe_data["names"] = [registry.get(c).name if registry.get(c) else c for c in safe_data["candidates"]]
            # D3: attach operator-readable guidance to events that used to be
            # dropped silently by the UI (governance_filtered, dispatch_failed,
            # call_timed_out, recovery_unresolved, and the related recovery
            # events) -- see _event_guidance above.
            guidance = _event_guidance(event, safe_data)
            if guidance:
                safe_data["guidance"] = guidance
            asyncio.run_coroutine_threadsafe(ws.send_json({"event": event, "data": safe_data}), loop)

        result = await mobilize(need, candidates, transport, ledger=ledger, on_progress=on_progress,
                                 mobilization_id=mobilization_id,
                                 governance_state=governance_state, governance_policy=governance_policy,
                                 governance_state_path=governance_state_path)

        if use_simulated_outcomes:
            # Hypothetical learning only -- applied to the private copy made
            # above, shown to the user as session-local, and never written
            # to REGISTRY_STATE_PATH. Discarded the moment this handler
            # returns; nothing durable is touched.
            updated_ids = record_outcomes(outcome_registry, result.all_results)
            display_registry = outcome_registry
        else:
            updated_ids = record_outcomes(registry, result.all_results)
            save_registry_json(registry, REGISTRY_STATE_PATH)
            display_registry = registry

        mode = "rehearsal" if use_simulated_outcomes else "live"
        confirmed_people = [_confirmed_person_dict(r, registry, mode) for r in result.confirmed]
        ambiguous_people = [
            {"id": cid, "name": registry.get(cid).name if registry.get(cid) else cid}
            for cid in result.ambiguous_candidate_ids
        ]

        await ws.send_json({
            "event": "final",
            "data": {
                "mode": mode,
                # Only meaningful for a live run -- rehearsal's mobilization_id
                # is a synthetic per-connection value and its registry copy is
                # discarded, so attendance must never be recorded against it.
                "mobilization_id": None if use_simulated_outcomes else mobilization_id,
                "filled": result.filled,
                "confirmed": confirmed_people,
                "need_count": need_count,
                "calls_used": result.calls_used,
                "waves": len(result.waves),
                "time_to_fill_seconds": result.time_to_fill_seconds,
                "over_recruitment_ratio": result.over_recruitment_ratio,
                "registry_size": len(display_registry),
                "never_called": len(display_registry) - result.calls_used,
                "learned_from_outcomes": len(updated_ids),
                # D3: the run's plan/rationale, so the operational screen can
                # actually explain success/shortage instead of just reporting it.
                "need_label": need_label,
                "location": location,
                "deadline_minutes": deadline_minutes,
                "max_calls": max_calls,
                "domain": domain,
                "required_skill": required_skill,
                # Advisory only -- see Need.latest_useful_arrival_minutes.
                # Distinct from deadline_minutes (the dispatch cutoff,
                # enforced by dispatcher.py); this is not enforced anywhere,
                # just surfaced for operator judgment on late/uncertain ETAs.
                "latest_useful_arrival_minutes": latest_useful_arrival_minutes,
                "stop_reason": result.stop_reason,
                "stop_reason_text": STOP_REASON_TEXT.get(result.stop_reason or "", None),
                "counts": result.counts,
                # In-flight/uncertain calls -- never auto-retried, must be
                # reconciled by a human (see /api/reconcile above). A non-empty
                # list here is exactly the "do not show a clean finished
                # checkmark" case D3 calls out.
                "ambiguous_candidate_ids": ambiguous_people,
            },
        })
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # surfaced to the UI instead of a silent disconnect
        try:
            await ws.send_json({"event": "error", "data": {"message": str(exc)}})
        except Exception:
            pass
    finally:
        if rehearsal_ledger_path is not None:
            rehearsal_ledger_path.unlink(missing_ok=True)


class _RegistryBackedSimulatedTransport:
    """Simulates call outcomes drawn from each REAL registry person's own
    learned accept/show-up rates, instead of a synthetic population. This
    is what makes the dashboard rehearsable against your actual list before
    spending a real credit -- the ranking and the rehearsal both use the
    same numbers a real run would use."""

    def __init__(self, registry: Registry) -> None:
        import random
        self._registry = registry
        self._rng = random.Random()
        self._pending: dict[str, tuple[str, float, bool, bool, str]] = {}

    async def dispatch(self, candidate, need_label, location, *, idempotency_key):
        import time as _time
        import uuid

        call_id = f"reg_{uuid.uuid4().hex[:10]}"
        person = self._registry.get(candidate.id)
        accept_rate = person.accept_rate if person else 0.5
        showup_rate = person.showup_rate if person else 0.5

        picked_up = self._rng.random() < 0.75
        accepted = picked_up and self._rng.random() < accept_rate
        evidence = "leaving now, ten minutes" if (accepted and self._rng.random() < showup_rate) else \
                   ("I'll try, maybe" if accepted else ("no answer" if not picked_up else "can't make it"))
        latency = self._rng.uniform(0.05, 0.35)

        self._pending[call_id] = (candidate.id, _time.monotonic() + latency, picked_up, accepted, evidence)
        return call_id

    async def poll(self, call_id: str, *, expected_candidate=None):
        import time as _time
        from mobilize.core.commitment import calibrated_commitment
        from mobilize.core.types import CallOutcome, CallResult, utcnow

        entry = self._pending.get(call_id)
        if entry is None:
            return None
        candidate_id, ready_at, picked_up, accepted, evidence = entry
        if _time.monotonic() < ready_at:
            return None

        person = self._registry.get(candidate_id)
        showup_rate = person.showup_rate if person else 0.5

        if not picked_up:
            return CallResult(call_id=call_id, candidate_id=candidate_id, outcome=CallOutcome.NO_ANSWER,
                               commitment_score=0.0, stated_yes=False, evidence="No answer.")
        if not accepted:
            return CallResult(call_id=call_id, candidate_id=candidate_id, outcome=CallOutcome.NO,
                               commitment_score=0.0, stated_yes=False, evidence=evidence)

        commitment = calibrated_commitment(evidence=evidence, candidate_prior_showup_rate=showup_rate)
        outcome = CallOutcome.FIRM_YES if commitment >= 0.6 else CallOutcome.SOFT_YES
        return CallResult(call_id=call_id, candidate_id=candidate_id, outcome=outcome,
                           commitment_score=commitment, stated_yes=True, evidence=evidence)


_mask = mask_phone


_PAGE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>mobilize</title>
<style>
  * { box-sizing: border-box; }
  body { background: #0b0e14; color: #e6e6e6; font-family: -apple-system, "SF Pro Text", "Segoe UI", sans-serif; padding: 24px; max-width: 1100px; margin: 0 auto; }
  h1 { font-size: 20px; font-weight: 700; color: #fff; margin-bottom: 4px; }
  .subtitle { color: #9ca3af; font-size: 13px; margin-bottom: 20px; }
  .panel { background: #10141c; border: 1px solid #232a38; border-radius: 10px; padding: 16px; margin-bottom: 16px; }
  .panel h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.04em; color: #9ca3af; margin: 0 0 12px 0; }
  label { display: block; font-size: 12px; color: #9ca3af; margin: 10px 0 4px; }
  input, textarea { width: 100%; background: #171c26; color: #e6e6e6; border: 1px solid #2a3140; border-radius: 6px; padding: 8px 10px; font-family: inherit; font-size: 13px; }
  textarea { font-family: "SF Mono", monospace; font-size: 11px; height: 90px; }
  .row { display: flex; gap: 12px; }
  .row > div { flex: 1; }
  button { background: #2563eb; color: #fff; border: none; border-radius: 6px; padding: 9px 16px; cursor: pointer; font-weight: 600; font-size: 13px; margin-top: 12px; }
  button:hover { background: #1d4ed8; }
  button.secondary { background: #232a38; }
  button.secondary:hover { background: #2a3140; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th { text-align: left; color: #9ca3af; font-weight: 600; padding: 6px 8px; border-bottom: 1px solid #232a38; }
  td { padding: 6px 8px; border-bottom: 1px solid #171c26; }
  .badge { display: inline-block; padding: 1px 6px; border-radius: 4px; font-size: 10px; font-weight: 700; }
  .badge.eligible { background: #14301f; color: #4ade80; }
  .badge.ineligible { background: #301414; color: #f87171; }
  #map { display: grid; grid-template-columns: repeat(10, 1fr); gap: 6px; margin: 12px 0; }
  .node { width: 100%; aspect-ratio: 1; border-radius: 50%; background: #2a3140; transition: all 0.3s; }
  .node.dialing { background: #f59e0b; animation: pulse 0.8s infinite; }
  .node.firm_yes { background: #22c55e; }
  .node.soft_yes { background: #eab308; }
  .node.no, .node.no_answer, .node.failed { background: #3f4656; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
  #log { background: #0d1017; border-radius: 6px; padding: 10px; height: 180px; overflow-y: auto; font-size: 12px; font-family: "SF Mono", monospace; }
  .line { padding: 2px 0; }
  .firm_yes { color: #22c55e; } .soft_yes { color: #eab308; } .no, .no_answer, .failed { color: #6b7280; }
  #results { font-size: 13px; }
  #results .confirmed-row { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #171c26; }
  .pill { background: #1e293b; border-radius: 999px; padding: 2px 10px; font-size: 11px; color: #93c5fd; }
  .summary { display: flex; gap: 24px; margin: 12px 0; }
  .summary .stat { text-align: center; }
  .summary .stat .n { font-size: 22px; font-weight: 700; color: #fff; }
  .summary .stat .l { font-size: 10px; color: #9ca3af; text-transform: uppercase; }
  .msg { font-size: 12px; padding: 8px; border-radius: 6px; margin-top: 8px; }
  .msg.error { background: #301414; color: #f87171; }
  .msg.ok { background: #14301f; color: #4ade80; }
  .msg.warn { background: #3a2a0d; color: #fbbf24; }
  .exc-line { padding: 6px 0; border-bottom: 1px solid #171c26; font-size: 12px; }
  .exc-line .exc-what { color: #fbbf24; font-weight: 600; }
  .exc-line .exc-guidance { color: #9ca3af; margin-top: 2px; }
  .plan-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin: 10px 0; font-size: 12px; }
  .plan-grid .k { color: #9ca3af; font-size: 10px; text-transform: uppercase; }
  .plan-grid .v { color: #fff; font-size: 14px; font-weight: 600; }
  .handoff-row { display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid #171c26; font-size: 12px; }
  .handoff-row .meta { color: #9ca3af; font-size: 11px; }
  #mode-banner { display: inline-block; padding: 3px 10px; border-radius: 999px; font-size: 11px; font-weight: 700; letter-spacing: 0.03em; text-transform: uppercase; margin-bottom: 12px; }
  #mode-banner.rehearsal { background: #1e293b; color: #93c5fd; }
  #mode-banner.live { background: #301414; color: #f87171; }
</style>
</head>
<body>
  <h1>mobilize</h1>
  <div class="subtitle">Load your registry, describe who you need, watch it fill. Free rehearsal against your own list by default -- no real calls unless you ask for them.</div>
  <div id="mode-banner" class="rehearsal">Rehearsal mode -- no calls placed, nothing saved yet</div>

  <div class="panel">
    <h2>1 · Registry</h2>
    <div id="registry-summary">Loading...</div>
    <label>Paste your own CSV (columns: name, phone, timezone -- optionally last_donation, distance_km, accept_rate, showup_rate)</label>
    <textarea id="csv-input" placeholder="name,phone,timezone
Asha Rao,+15550101001,Asia/Kolkata"></textarea>
    <button onclick="uploadRegistry()">Load this list</button>
    <button class="secondary" onclick="resetRegistry()">Reset to sample registry</button>
    <div id="upload-msg"></div>
    <div id="registry-table" style="margin-top:12px; max-height:200px; overflow-y:auto;"></div>
  </div>

  <div class="panel">
    <h2>2 · What do you need</h2>
    <div class="row">
      <div><label>Describe the need</label><input id="need_label" value="O-negative blood needed urgently"></div>
      <div><label>Location</label><input id="location" value="City Hospital"></div>
    </div>
    <div class="row">
      <div><label>How many confirmed</label><input id="need_count" type="number" value="3"></div>
      <div><label>Deadline (minutes)</label><input id="deadline_minutes" type="number" value="60"></div>
      <div><label>Max calls</label><input id="max_calls" type="number" value="40"></div>
    </div>
    <div class="row">
      <div><label>Scenario</label>
        <select id="domain">
          <option value="donor">Donor coordination</option>
          <option value="shift">Emergency shift coverage</option>
        </select>
      </div>
      <div><label>Required skill (shift only)</label><input id="required_skill" placeholder="e.g. RN, EMT"></div>
      <div><label>Latest useful arrival (minutes, optional)</label><input id="latest_useful_arrival_minutes" type="number" placeholder="e.g. 90"></div>
    </div>
    <button onclick="run(true)">Run rehearsal (free, simulated on your list)</button>
    <button class="secondary" onclick="run(false)" id="real-btn">Run for real (spends CALL-E credits)</button>
  </div>

  <div class="panel">
    <h2>3 · Live dispatch</h2>
    <div id="map"></div>
    <div id="log"></div>
  </div>

  <div class="panel" id="exceptions-panel" hidden>
    <h2>Exceptions &amp; recovery</h2>
    <div id="exceptions"></div>
  </div>

  <div class="panel">
    <h2>4 · Result &amp; handoff</h2>
    <div id="results">Run a mobilization to see who confirmed.</div>
  </div>

<script>
let nodes = {};

// Every value interpolated into innerHTML below is escaped through this --
// registry names/timezones come from a coordinator's OWN uploaded CSV,
// which this app must treat as untrusted input. Without this, a name like
// <img src=x onerror=...> in an uploaded spreadsheet would execute as
// script in this same-origin page.
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

// Source labels for showup_rate: never show a bare percentage without
// saying whether it's real evidence this product collected, a prior a
// coordinator's own CSV shipped in, or just the untouched default.
function showupLabel(p) {
  const pct = `${(p.showup_rate*100).toFixed(0)}%`;
  if (p.showup_rate_source === 'observed') {
    return `${pct} show-up <span class="pill" title="from ${esc(p.attendance_sample_count)} recorded observation(s)">n=${esc(p.attendance_sample_count)} observed</span>`;
  }
  if (p.showup_rate_source === 'imported') {
    return `${pct} show-up <span class="pill" title="from the uploaded CSV's own showup_rate column, not verified by this product">imported prior</span>`;
  }
  return `${pct} show-up <span class="pill" title="no data yet -- neutral default">no data</span>`;
}

async function loadRegistrySummary() {
  const r = await fetch('/api/registry').then(r => r.json());
  document.getElementById('registry-summary').innerHTML =
    `<b>${esc(r.count)}</b> people loaded (${esc(r.source)}). Ranked by likelihood to confirm and follow through.`;
  const rows = r.people.slice(0, 30).map(p => `
    <tr>
      <td>${esc(p.name)}</td>
      <td>${esc(p.phone)}</td>
      <td>${esc(p.timezone)}</td>
      <td><span class="badge ${p.eligible ? 'eligible' : 'ineligible'}">${p.eligible ? 'eligible' : 'not yet'}</span></td>
      <td>${(p.accept_rate*100).toFixed(0)}% accept</td>
      <td>${showupLabel(p)}</td>
      <td>${esc(p.times_called)}x called</td>
      <td>
        <button class="secondary" style="margin:0;padding:3px 8px;font-size:11px;" onclick="recordAttendance('${esc(p.id)}','arrived')">Arrived</button>
        <button class="secondary" style="margin:0;padding:3px 8px;font-size:11px;" onclick="recordAttendance('${esc(p.id)}','did_not_arrive')">No-show</button>
        <button class="secondary" style="margin:0;padding:3px 8px;font-size:11px;" onclick="recordAttendance('${esc(p.id)}','unknown')">Unknown</button>
      </td>
    </tr>`).join('');
  document.getElementById('registry-table').innerHTML =
    `<table><thead><tr><th>Name</th><th>Phone</th><th>TZ</th><th>Status</th><th>Accept</th><th>Show-up</th><th>History</th><th>Record attendance</th></tr></thead><tbody>${rows}</tbody></table>`;
}

// Ground truth, recorded by a coordinator after the fact -- the only thing
// allowed to move showup_rate. Keyed to the mobilization the dashboard is
// currently showing results for (set in the 'final' WS handler below); a
// click before any run has completed records against a generic 'manual'
// bucket instead, since there's no specific mobilization to attribute it to.
let currentMobilizationId = 'manual';

async function recordAttendance(personId, status) {
  const res = await fetch('/api/attendance', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({mobilization_id: currentMobilizationId, person_id: personId, status}),
  }).then(r => r.json());
  if (res.error) { alert(res.error); return; }
  loadRegistrySummary();
}

async function uploadRegistry() {
  const csv = document.getElementById('csv-input').value.trim();
  if (!csv) return;
  const res = await fetch('/api/registry/upload', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({csv})
  }).then(r => r.json());
  const msgEl = document.getElementById('upload-msg');
  if (res.error) {
    msgEl.innerHTML = `<div class="msg error">${esc(res.error)}</div>`;
  } else {
    msgEl.innerHTML = `<div class="msg ok">${esc(res.message)}</div>`;
    loadRegistrySummary();
  }
}

async function resetRegistry() {
  await fetch('/api/registry/reset', {method: 'POST'});
  document.getElementById('upload-msg').innerHTML = '';
  loadRegistrySummary();
}

function setModeBanner(mode) {
  const el = document.getElementById('mode-banner');
  el.className = mode;
  el.textContent = mode === 'live'
    ? 'Live mode -- real calls placed, registry updated'
    : 'Rehearsal mode -- no calls placed, nothing saved yet';
}

// D3: events that used to be silently dropped by the UI -- now rendered
// here with the server-attached operator guidance, plus an "unresolved
// count" the false-completion guard below reads.
const DROPPED_EVENTS = new Set(['governance_filtered', 'dispatch_failed', 'call_timed_out', 'recovery_unresolved', 'recovery_reconciled', 'recovering_in_flight']);
function renderExceptionEvent(event, data) {
  const panel = document.getElementById('exceptions-panel');
  panel.hidden = false;
  const who = data.names ? data.names.join(', ') : (data.name || data.candidate_id || '');
  const what = {
    governance_filtered: `Governance filtered ${esc(data.blocked)} candidate(s) from wave ${esc(data.wave)}${who ? ' — ' + esc(who) : ''}`,
    dispatch_failed: `Dispatch failed for ${esc(who)}${data.error ? ': ' + esc(data.error) : ''}`,
    call_timed_out: `Call timed out for ${esc(who)}`,
    recovery_unresolved: `Unresolved from a prior run: ${esc(who)}`,
    recovery_reconciled: `Reconciled from a prior run: ${esc(who)}`,
    recovering_in_flight: `Recovering ${esc(data.count)} in-flight call(s) from a prior run`,
  }[event] || `${esc(event)}: ${esc(who)}`;
  const div = document.createElement('div');
  div.className = 'exc-line';
  div.innerHTML = `<div class="exc-what">${what}</div>` +
    (data.guidance ? `<div class="exc-guidance">${esc(data.guidance)}</div>` : '');
  document.getElementById('exceptions').appendChild(div);
}

function run(simulate) {
  let confirmed = false;
  if (!simulate) {
    if (!confirm('This places REAL CALL-E calls and spends real credits. Continue?')) return;
    confirmed = true;
  }
  setModeBanner(simulate ? 'rehearsal' : 'live');
  document.getElementById('map').innerHTML = '';
  document.getElementById('log').innerHTML = '';
  document.getElementById('results').innerHTML = 'Running...';
  document.getElementById('exceptions').innerHTML = '';
  document.getElementById('exceptions-panel').hidden = true;
  nodes = {};

  const ws = new WebSocket(`ws://${location.host}/ws/run`);
  ws.onopen = () => ws.send(JSON.stringify({
    need_label: document.getElementById('need_label').value,
    location: document.getElementById('location').value,
    need_count: +document.getElementById('need_count').value,
    deadline_minutes: +document.getElementById('deadline_minutes').value,
    max_calls: +document.getElementById('max_calls').value,
    domain: document.getElementById('domain').value,
    required_skill: document.getElementById('required_skill').value || null,
    latest_useful_arrival_minutes: document.getElementById('latest_useful_arrival_minutes').value
      ? +document.getElementById('latest_useful_arrival_minutes').value : null,
    simulate: simulate,
    confirm: confirmed,
  }));
  ws.onmessage = (msg) => {
    const {event, data} = JSON.parse(msg.data);
    const log = document.getElementById('log');
    if (event === 'error') {
      document.getElementById('results').innerHTML = `<div class="msg error">${esc(data.message)}</div>`;
    } else if (event === 'wave_dispatch') {
      (data.names || data.candidates).forEach((name, i) => {
        const cid = data.candidates[i];
        if (!nodes[cid]) {
          const el = document.createElement('div');
          el.className = 'node dialing'; el.title = name;
          document.getElementById('map').appendChild(el);
          nodes[cid] = el;
        } else { nodes[cid].className = 'node dialing'; }
      });
      log.innerHTML += `<div class="line">— wave ${esc(data.wave)}: dialing ${esc(data.candidates.length)} in parallel</div>`;
    } else if (event === 'call_result') {
      if (nodes[data.candidate_id]) nodes[data.candidate_id].className = 'node ' + data.outcome;
      log.innerHTML += `<div class="line ${esc(data.outcome)}">${esc(data.name || data.candidate_id)}  ${esc(data.outcome)}  commitment=${data.commitment.toFixed(2)}</div>`;
    } else if (event === 'need_met') {
      log.innerHTML += `<div class="line firm_yes">✓ need met at ${data.time_to_fill_seconds.toFixed(1)}s — no further wave dispatched</div>`;
    } else if (event === 'opted_out') {
      log.innerHTML += `<div class="line failed">${esc(data.candidate_id)} asked not to be contacted again — added to do-not-call</div>`;
    } else if (DROPPED_EVENTS.has(event)) {
      renderExceptionEvent(event, data);
    } else if (event === 'final') {
      renderFinal(data, simulate);
      if (data.mode !== 'rehearsal') {
        if (data.mobilization_id) currentMobilizationId = data.mobilization_id;
        loadRegistrySummary();
      }
    }
    log.scrollTop = log.scrollHeight;
  };
}

// Last final-event payload, kept only so exportHandoff() can build the
// download without re-deriving anything the server already computed --
// never anything beyond what's already rendered (no transcript, no raw phone).
let lastFinalData = null;

async function reconcileCandidate(mobilizationId, candidateId, decision) {
  const res = await fetch('/api/reconcile', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({mobilization_id: mobilizationId, candidate_id: candidateId, decision, recorded_by: 'dashboard_operator'}),
  }).then(r => r.json());
  if (res.error) { alert(res.error); return; }
  if (lastFinalData) {
    lastFinalData.ambiguous_candidate_ids = res.still_unresolved;
    renderFinal(lastFinalData, lastFinalData.mode !== 'live');
  }
}

function exportHandoff() {
  if (!lastFinalData) return;
  // Actionable roster/evidence summary for a handoff -- masked phones,
  // normalized verdicts, call references, no raw transcript. Deliberately
  // NOT the full 'final' payload (which is fine to hold in memory for this
  // session but is not what a coordinator handing off to someone else needs).
  const summary = {
    mode: lastFinalData.mode,
    mobilization_id: lastFinalData.mobilization_id,
    need_label: lastFinalData.need_label,
    location: lastFinalData.location,
    filled: lastFinalData.filled,
    stop_reason: lastFinalData.stop_reason,
    stop_reason_text: lastFinalData.stop_reason_text,
    confirmed: lastFinalData.confirmed.map(c => ({
      name: c.name, phone: c.phone, verdict: c.verdict, commitment: c.commitment,
      evidence: c.evidence, call_id: c.call_id, mode: c.mode, captured_at: c.captured_at,
    })),
    unresolved: (lastFinalData.ambiguous_candidate_ids || []).map(p => p.name),
  };
  const blob = new Blob([JSON.stringify(summary, null, 2)], {type: 'application/json'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `mobilize_handoff_${lastFinalData.mobilization_id || 'rehearsal'}.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function renderFinal(data, simulate) {
  lastFinalData = data;
  setModeBanner(data.mode || (simulate ? 'rehearsal' : 'live'));

  const planGrid = `
    <div class="plan-grid">
      <div><div class="k">Need</div><div class="v">${esc(data.need_label)}</div></div>
      <div><div class="k">Location</div><div class="v">${esc(data.location) || '—'}</div></div>
      <div><div class="k">Deadline</div><div class="v">${esc(data.deadline_minutes)} min</div></div>
      <div><div class="k">Max calls (budget)</div><div class="v">${esc(data.max_calls)}</div></div>
      <div><div class="k">Waves dispatched</div><div class="v">${esc(data.waves)}</div></div>
      <div><div class="k">Over-recruitment</div><div class="v">${data.over_recruitment_ratio != null ? data.over_recruitment_ratio.toFixed(2)+'x' : '—'}</div></div>
    </div>`;

  const counts = data.counts || {};
  const excess = counts.excess_commitments || 0;
  const excessNote = excess > 0
    ? `<div class="msg ok" style="margin-top:6px;">${esc(excess)} extra confirmation(s) beyond target (over-recruitment) — visible here, not hidden.</div>` : '';

  const unresolvedList = data.ambiguous_candidate_ids || [];
  const isUnresolved = unresolvedList.length > 0;

  // D3: never show a clean "finished" checkmark while calls may still be
  // live and unreconciled -- this is the false-completion guard.
  const statusBanner = isUnresolved
    ? `<div class="msg warn">⚠ Not fully resolved — ${esc(unresolvedList.length)} call(s) are in an unknown state and may still be live. Reconcile below before treating this mobilization as finished.</div>`
    : (data.filled
        ? `<div class="msg ok">✓ Need filled and no unresolved calls.</div>`
        : `<div class="msg warn">Target not met. ${esc(data.stop_reason_text || 'See stop reason below.')}</div>`);

  const reconcileRows = unresolvedList.map(p => `
    <div class="handoff-row">
      <span>${esc(p.name)}</span>
      <span>
        <button class="secondary" style="margin:0;padding:3px 8px;font-size:11px;" onclick="reconcileCandidate('${esc(data.mobilization_id)}','${esc(p.id)}','confirmed_no_call_placed')">No call placed</button>
        <button class="secondary" style="margin:0;padding:3px 8px;font-size:11px;" onclick="reconcileCandidate('${esc(data.mobilization_id)}','${esc(p.id)}','confirmed_call_placed_no_answer')">Placed, no answer</button>
        <button class="secondary" style="margin:0;padding:3px 8px;font-size:11px;" onclick="reconcileCandidate('${esc(data.mobilization_id)}','${esc(p.id)}','treat_as_failed')">Treat as failed</button>
      </span>
    </div>`).join('');
  const reconcilePanel = isUnresolved ? `
    <div style="margin-top:14px;">
      <h2 style="font-size:12px;color:#9ca3af;text-transform:uppercase;">Reconcile unknowns</h2>
      ${reconcileRows}
    </div>` : '';

  const confirmedRows = data.confirmed.map(c => `
    <div class="handoff-row">
      <span>
        <b>${esc(c.name)}</b> · ${esc(c.phone)}<br>
        <span class="meta">verdict ${esc(c.verdict)} · call ${esc(c.call_id)} · ${esc(c.mode)} · captured ${esc(c.captured_at) || 'unknown'}</span><br>
        <span class="meta">"${esc(c.evidence)}"</span>
      </span>
      <span style="text-align:right;">
        <div class="pill">commitment ${esc(c.commitment)}</div><br>
        <button class="secondary" style="margin-top:4px;padding:3px 8px;font-size:11px;" onclick="recordAttendance('${esc(c.id)}','arrived')">Arrived</button>
        <button class="secondary" style="margin-top:4px;padding:3px 8px;font-size:11px;" onclick="recordAttendance('${esc(c.id)}','did_not_arrive')">No-show</button>
      </span>
    </div>`).join('') || '<div style="color:#9ca3af">Nobody confirmed.</div>';

  const learningNote = data.mode === 'rehearsal'
    ? `Hypothetical learning from ${esc(data.learned_from_outcomes)} simulated outcome(s) — session-local only, not saved. Your real registry is unchanged.`
    : `Registry updated from ${esc(data.learned_from_outcomes)} outcome(s) — rankings above will reflect this next run.`;

  document.getElementById('results').innerHTML = `
    ${planGrid}
    ${statusBanner}
    ${excessNote}
    <div class="summary">
      <div class="stat"><div class="n">${esc(data.confirmed.length)}/${esc(data.need_count)}</div><div class="l">confirmed</div></div>
      <div class="stat"><div class="n">${esc(data.calls_used)}</div><div class="l">calls used</div></div>
      <div class="stat"><div class="n">${esc(data.never_called)}</div><div class="l">never called</div></div>
      <div class="stat"><div class="n">${data.time_to_fill_seconds ? data.time_to_fill_seconds.toFixed(1)+'s' : '—'}</div><div class="l">time to fill</div></div>
    </div>
    <div style="font-size:11px;color:#9ca3af;">Stop reason: <b>${esc(data.stop_reason) || 'unknown'}</b> — ${esc(data.stop_reason_text) || 'not recorded.'}</div>
    ${confirmedRows}
    ${reconcilePanel}
    <div style="margin-top:10px; color:#9ca3af; font-size:11px;">${learningNote}</div>
    <button class="secondary" onclick="exportHandoff()">Export handoff roster (JSON, masked)</button>
  `;
}

loadRegistrySummary();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("MOBILIZE_DASHBOARD_PORT", 8731))
    # Localhost only by default -- this is a single-operator local tool, not
    # a deployed service, and there is no authentication on top of it.
    # Binding to 0.0.0.0 would expose the registry (real names and phone
    # numbers) and the real-call dispatch path to anyone else on the same
    # network. Set MOBILIZE_DASHBOARD_HOST=0.0.0.0 explicitly to opt in.
    host = os.environ.get("MOBILIZE_DASHBOARD_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port)
