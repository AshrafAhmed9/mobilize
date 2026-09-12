# E1 — platform, adapters, and reset

## Tested versions (this workspace, `.venv`)

| Component | Version |
|---|---|
| Platform | macOS 26.6.2 (Darwin 25.6.0), arm64 |
| Python | 3.14.6 |
| fastapi | 0.141.1 |
| mcp | 2.0.0 |
| httpx | 0.28.1 |
| calle-ai | 0.6.0 |
| pydantic | 2.13.4 |
| uvicorn | 0.52.1 |
| websockets | 17.0.1 |
| python-dotenv | 1.2.2 |
| pytest | 9.1.1 |
| hypothesis | 6.165.2 |
| pytest-asyncio | 1.4.0 |

`pyproject.toml` pins minimums (`requires-python >= 3.11`, `httpx>=0.27`,
`fastapi>=0.110`, `mcp>=2.0`, etc.) — the table above is what was actually
resolved and run in this environment, not the floor. A clean clone with only
`pip install -e .[dev]` (no `.env` needed for fixture/simulated modes) is the
acceptance bar; keyless imports alone (`python -c "import mobilize"`) are not
proof dependencies resolve for real, since a missing transitive dependency or
version conflict can still surface only when a name is actually imported
under exercise (e.g. `mcp.server.fastmcp`) or an object constructed. Run the
full suite (`.venv/bin/python -m pytest mobilize/tests/ -q`) and the harness
(`.venv/bin/python -m mobilize.sim.harness`) after a fresh install as the
actual proof.

## Which adapters invoke CALL-E at runtime

| Adapter | Real CALL-E network call? | Path |
|---|---|---|
| `mobilize.transports.calle.CalleTransport` | **Yes** | `httpx.AsyncClient` against `CALLE_BASE_URL`, gated by `CALLE_API_KEY`. The only component in this project that opens a real HTTP connection to CALL-E. |
| CLI `--real` | Yes, via `CalleTransport` | Requires `CALLE_API_KEY`, explicit `--phones`/`--timezones`, and a typed `yes` at an interactive prompt (`mobilize/app/cli.py::run_real`). |
| Dashboard `/ws/run` with `simulate:false` | Yes, via `CalleTransport` | Requires `confirm:true` in the WebSocket payload; refuses otherwise regardless of what the browser's own confirmation dialog does client-side (`mobilize/app/dashboard.py`, the `confirmed_real` check). |
| MCP `mobilize_real` tool | Yes, via `CalleTransport` | Requires `confirm=true`; `confirm=false` (the default) returns a preview (`would_call`) and dispatches nothing. |
| CLI default (no flag), dashboard `simulate:true`, MCP `mobilize_simulated` | **No** | `SimulatedTransport` (`mobilize/transports/simulated.py`) computes outcomes directly from `mobilize.sim.population.simulate_call` — it never touches `_to_call_result` and cannot produce the retraction/governance branches described below. |
| CLI `--fixture <scenario>`, dashboard `/api/fixture/{scenario}`, MCP `mobilize_fixture` | **No** | `FixtureTransport` (`mobilize/sim/fixture_transport.py`) — no `httpx.AsyncClient`, no base URL, no API key anywhere in the class. Routes a hand-authored, API-shaped payload through the real `_to_call_result` so the production translation logic is actually exercised, deterministically, with zero network. |

Only `CalleTransport` can reach the network, and every path that constructs
it is gated behind an explicit, non-default confirmation (a CLI prompt or a
`confirm`/`confirm:true` flag) plus `CALLE_API_KEY`. `mobilize/tests/
test_e1_mcp_roundtrip_and_real_call_gate.py` and `test_dashboard_security.py`
exercise these refusals directly against the real FastAPI app / MCP tool
functions, not just as unit assertions on isolated logic.

## Reset without deleting a user's operational registry

`POST /api/registry/reset` (dashboard) unconditionally deletes
`REGISTRY_STATE_PATH` (`/tmp/mobilize_dashboard_registry.json`) and falls
back to the built-in sample registry. **It does this regardless of whether
the current registry is the sample data or a real list a coordinator
uploaded via `/api/registry/upload`.** `REGISTRY_SOURCE_MARKER_PATH` records
provenance (`"uploaded"` vs. sample-default) but the reset endpoint does not
currently consult that marker before deleting — this is documented, not
silently patched, because changing that endpoint's behavior is outside this
task's scope (dashboard changes here are additive-only) and because the
correct fix (e.g. refuse to reset an uploaded registry without a second
confirmation, or write resets to a separate namespace) is itself a judgment
call about product behavior, not a test-and-docs gap.

**Until that's addressed, the safe reset procedure for a judge/demo session
using real operational data is:**

1. Before resetting, `GET /api/registry` and save the JSON response, or keep
   the original CSV/JSON file that was uploaded via `/api/registry/upload` —
   it is the only backup; there is no server-side undo.
2. Call `POST /api/registry/reset` only when you want to discard the current
   registry (sample or real) and return to the built-in sample data.
3. To restore a real registry after a reset, re-upload the saved file via
   `POST /api/registry/upload`.

This applies only to the dashboard's registry state. The fixture scenarios
(`mobilize/sim/fixture_transport.py`) never touch `REGISTRY_STATE_PATH` at
all — each fixture run uses a fresh, throwaway temp ledger file
(`tempfile.mkstemp`, deleted in a `finally` block) and its own in-memory
`Candidate`s, so running or resetting fixture scenarios can never affect a
real registry or a real ledger. Similarly, `/api/attendance`'s durable
attendance log (`ATTENDANCE_LOG_PATH`) and the real dispatch ledger
(`REAL_LEDGER_PATH`) are separate files from `REGISTRY_STATE_PATH` and are
untouched by `/api/registry/reset`.

## Fresh namespace / determinism notes

- `run_fixture_scenario` (`mobilize/sim/fixture_transport.py`) derives a
  fresh `mobilization_id` per call (`fixture_{scenario}_{uuid4 hex}`) unless
  one is passed explicitly, and always uses a throwaway temp ledger deleted
  after the run — so repeated invocations, or concurrent judge sessions,
  never collide.
- The CLI's existing `--seed` flag controls only `mobilize.sim.population`
  (the plain simulator's pool generation); it has no effect on the
  dashboard's own registry-backed `_RegistryBackedSimulatedTransport`, which
  is unseeded, and no effect on fixture scenarios, which need no seed at all
  since every payload is hand-authored and fixed.
- `normalize_trace`/`normalize_call_result` strip `call_id`, `completed_at`,
  `started_at`, and other timestamp/elapsed fields before any cross-run or
  cross-adapter comparison — required because `_to_call_result` calls
  `utcnow()` internally (real wall-clock, in code this task does not
  modify), so exact byte-for-byte trace equality across runs is not a valid
  test target; semantic equality after normalization is.
