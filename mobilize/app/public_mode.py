"""E2: a public-safe rehearsal-only surface, deliberately separate from
`mobilize/app/dashboard.py`.

Why a separate app instead of a flag on the existing dashboard: the
dashboard's real-dispatch path is one `if not confirmed_real: reject` check
away from `CalleTransport` (see its `/ws/run`) -- correct for a trusted local
operator, but a public deployment should not have that code, or a provider
key, reachable at all. This module never imports `mobilize.transports.calle`
and never reads `CALLE_API_KEY` (or any credential) anywhere in its source,
so there is no code path here that can escalate to a real call regardless of
what a client sends -- "confirm", "live", "simulate": false, anything.

Engine events are real, not canned: `/api/run` calls
`mobilize.sim.fixture_transport.run_fixture_scenario`, which drives the
actual `mobilize()` dispatcher (core/dispatcher.py, unmodified) against a
deterministic `FixtureTransport`. The "trace" a caller gets back is the
dispatcher's own progress events, normalized for reproducibility -- the same
function the CLI's `--fixture-scenario` and the MCP `mobilize_fixture` tool
use (see fixture_transport.py's module docstring).

Session isolation: each browser session gets its own `Registry` in an
in-memory dict keyed by an opaque session id issued by `POST /api/session`.
There is no shared file path, no shared module-level Registry, and no
overlap with the dashboard's `/tmp/mobilize_dashboard_*` state -- two
sessions (or two judges hitting the same deployment) cannot see or mutate
each other's uploaded registry. Fixture runs don't even touch session
state -- `run_fixture_scenario` builds its own fresh temp ledger and
mobilization id per call (see its docstring) -- so concurrent runs across
sessions can't collide there either.

Run locally:
    python -m mobilize.app.public_mode
    open http://localhost:8732

This is intentionally not the same port as dashboard.py (8731), so both can
run side by side. See mobilize/artifacts/e2_public_mode.md for what remains
for actual public hosting (a hosting account, DNS, TLS -- Ashraf's call, out
of scope here).
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from mobilize.core.registry import Registry, RegistryError, load_registry_csv
from mobilize.sim.fixture_transport import SCENARIOS, run_fixture_scenario

MODE_LABEL = "REHEARSAL MODE — no real calls possible"

APP_DIR = Path(__file__).resolve().parent
SAMPLE_REGISTRY_CSV = APP_DIR / "sample_data" / "sample_registry.csv"

app = FastAPI(title="mobilize-public-rehearsal")

# Per-session registries. Never persisted to disk, never shared with
# dashboard.py's REGISTRY_STATE_PATH, and capped so a public deployment
# left running can't grow this dict without bound.
_sessions: dict[str, Registry] = {}
_SESSION_CAP = 1000


def _sample_registry() -> Registry:
    try:
        return load_registry_csv(SAMPLE_REGISTRY_CSV)
    except RegistryError:
        return Registry()


def _person_dict(p) -> dict:
    return {
        "id": p.id, "name": p.name,
        "accept_rate": round(p.accept_rate, 2), "showup_rate": round(p.showup_rate, 2),
    }


def _require_session(session_id: str | None) -> Registry:
    if not session_id or session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Unknown session_id. POST /api/session first.")
    return _sessions[session_id]


@app.post("/api/session")
async def create_session() -> dict:
    """Cold start: issue a fresh, isolated session seeded with the bundled
    sample registry. Calling this again with no session_id (e.g. after a
    refresh that lost client state) is just another cold start -- it always
    works and never touches any other session's data."""
    if len(_sessions) >= _SESSION_CAP:
        _sessions.pop(next(iter(_sessions)))
    session_id = uuid.uuid4().hex
    _sessions[session_id] = _sample_registry()
    return {"mode": MODE_LABEL, "session_id": session_id, "count": len(_sessions[session_id])}


@app.get("/api/registry")
async def get_registry(session_id: str | None = None) -> dict:
    """Repeat session / reconnect: the same session_id returns the same
    isolated registry it had before, unaffected by any other session's
    uploads or runs in between."""
    registry = _require_session(session_id)
    people = sorted(registry.all(), key=lambda p: -p.accept_rate)
    return {"mode": MODE_LABEL, "count": len(registry), "people": [_person_dict(p) for p in people]}


@app.post("/api/registry/upload")
async def upload_registry(payload: dict) -> dict:
    """Replaces only the calling session's registry. Validation errors (bad
    header, empty file, duplicate phone, ...) come straight from the same
    `load_registry_csv` the real dashboard uses, so they're the real
    coordinator-facing error text, not a stub."""
    session_id = payload.get("session_id")
    _require_session(session_id)
    csv_text = payload.get("csv", "")
    if not isinstance(csv_text, str) or not csv_text.strip():
        return {"mode": MODE_LABEL, "error": "csv must be non-empty text."}

    fd, tmp_name = tempfile.mkstemp(suffix=".csv", prefix="mobilize_public_upload_")
    tmp_path = Path(tmp_name)
    try:
        import os

        with os.fdopen(fd, "w") as f:
            f.write(csv_text)
        try:
            registry = load_registry_csv(tmp_path)
        except RegistryError as exc:
            return {"mode": MODE_LABEL, "error": str(exc)}
    finally:
        tmp_path.unlink(missing_ok=True)

    _sessions[session_id] = registry
    return {"mode": MODE_LABEL, "count": len(registry), "message": f"Loaded {len(registry)} people."}


@app.get("/api/scenarios")
async def list_scenarios() -> dict:
    return {"mode": MODE_LABEL, "scenarios": sorted(SCENARIOS)}


@app.post("/api/run")
async def run(payload: dict) -> dict[str, Any]:
    """The only endpoint that dispatches anything, and it can only ever run
    the deterministic fixture backend -- there is no branch here, or
    anywhere else in this module, that constructs `CalleTransport` or reads
    a provider API key. A client asking for a real/live call (any spelling:
    confirm, live, simulate: false) is refused server-side, unconditionally;
    that refusal does not depend on the flag being well-formed or even
    present, since the "real" code path simply does not exist in this
    process.
    """
    session_id = payload.get("session_id")
    _require_session(session_id)

    if payload.get("confirm") or payload.get("live") or payload.get("simulate") is False:
        return {
            "mode": MODE_LABEL,
            "error": "Real dispatch is not available in public rehearsal mode. "
                     "Only deterministic fixture scenarios can run here.",
        }

    scenario = payload.get("scenario")
    if scenario not in SCENARIOS:
        return {"mode": MODE_LABEL, "error": f"Unknown scenario {scenario!r}. Choose one of: {sorted(SCENARIOS)}."}

    result = await run_fixture_scenario(scenario)
    result["mode"] = MODE_LABEL
    return result


_PAGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>mobilize -- public rehearsal</title>
<style>
body{font-family:system-ui,sans-serif;max-width:760px;margin:32px auto;padding:0 16px;background:#0b0e14;color:#e5e7eb;}
.banner{background:#7c2d12;color:#fed7aa;padding:8px 12px;border-radius:6px;font-weight:600;margin-bottom:16px;}
select,button{font-size:14px;padding:6px 10px;margin-right:8px;}
pre{background:#111827;padding:12px;border-radius:6px;overflow-x:auto;white-space:pre-wrap;}
</style></head>
<body>
<div class="banner">REHEARSAL MODE -- no real calls possible</div>
<h1>mobilize -- public rehearsal</h1>
<p>Runs the real dispatcher against deterministic fixture scenarios. No provider key is loaded in this
process and no real call can be triggered from here, with or without confirmation.</p>
<div>
  <select id="scenario"><option>success</option><option>refusal</option><option>opt_out</option><option>ambiguity</option></select>
  <button onclick="runScenario()">Run rehearsal</button>
</div>
<pre id="out">Click "Run rehearsal" to see real engine events for the selected scenario.</pre>
<script>
let sessionId = null;
async function ensureSession() {
  if (sessionId) return sessionId;
  const r = await fetch('/api/session', {method: 'POST'});
  const j = await r.json();
  sessionId = j.session_id;
  return sessionId;
}
async function runScenario() {
  await ensureSession();
  const scenario = document.getElementById('scenario').value;
  const r = await fetch('/api/run', {
    method: 'POST', headers: {'content-type': 'application/json'},
    body: JSON.stringify({session_id: sessionId, scenario}),
  });
  document.getElementById('out').textContent = JSON.stringify(await r.json(), null, 2);
}
</script>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(_PAGE_HTML)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8732)
