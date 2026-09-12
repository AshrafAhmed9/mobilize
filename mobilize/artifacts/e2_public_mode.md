# E2 — public rehearsal mode

## What was built

`mobilize/app/public_mode.py`: a separate FastAPI app (not a flag on
`dashboard.py`) that only ever runs the deterministic fixture backend
(`mobilize.sim.fixture_transport.run_fixture_scenario`). It never imports
`mobilize.transports.calle` and never reads `CALLE_API_KEY` or any other
credential anywhere in its source — there is no code path in this process
that can reach a real call, regardless of what a client sends
(`confirm`, `live`, `simulate: false` are all explicitly checked and
refused, but the real guarantee is structural: the import doesn't exist).

- `POST /api/session` — issues an opaque session id, seeded with the sample
  registry. Cold start and repeat/reconnect both just call this (or re-fetch
  with an existing id); nothing durable, nothing shared.
- `GET /api/registry` / `POST /api/registry/upload` — per-session, in-memory
  only (`_sessions: dict[str, Registry]`), capped at 1000 entries so a
  long-lived deployment can't grow unbounded. Upload validation reuses the
  real `load_registry_csv` (same error text a coordinator would see in the
  real dashboard).
- `GET /api/scenarios`, `POST /api/run` — runs one of the four E1 fixture
  scenarios (`success`, `refusal`, `opt_out`, `ambiguity`) through the real
  `mobilize()` dispatcher and returns its actual progress trace, not a
  canned screen. Each run gets its own temp ledger and mobilization id
  (from `run_fixture_scenario` itself), so concurrent sessions can't
  collide.
- Every response carries `"mode": "REHEARSAL MODE — no real calls possible"`.

Tests: `mobilize/tests/test_e2_public_mode.py` (15 new, all passing) —
session isolation (two sessions can't see or mutate each other's registry),
real-call requests refused server-side under every flag spelling tried,
no-secrets-loadable (env var set to a bogus key, proven unread via AST
import inspection, not just a mock), cold start, repeat session/reconnect,
upload validation (good/bad/empty CSV), unknown-session and unknown-scenario
error visibility, and concurrent sessions not colliding.

Full suite: 310 passed, 3 xfailed, 2 xpassed (was 295/3/2 before this lane;
+15 new tests, zero regressions).

## Run it locally

```
.venv/bin/python -m mobilize.app.public_mode
open http://localhost:8732
```

Runs on port 8732 (dashboard.py stays on 8731) so both can run side by side
during judging.

## What remains for actual public hosting

Out of scope for this lane per the task's scope limit — needs Ashraf's
authorization and a hosting/DNS/TLS decision, not more engineering here:

- A hosting account and deploy step (Fly.io / Render / a container host —
  no framework changes needed, this is a plain FastAPI app).
- A public DNS name and TLS termination.
- Confirming the process is started with no `CALLE_API_KEY` (or any
  provider credential) in its environment at all, as an operational check
  on top of the code-level guarantee above.
- A decision on whether to also publish a pinned/downloadable build as an
  independent judge-reproduction path (EXECUTION_PLAN E2 mentions this;
  not built here since it's a packaging/distribution step, not a runtime
  behavior).
