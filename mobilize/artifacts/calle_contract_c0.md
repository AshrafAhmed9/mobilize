# CALL-E contract: measured vs. documented

Date: 12 September 2026. GET-only investigation, no live API access in this
sandbox (no `CALLE_API_KEY`, no network calls made). Everything under
"Measured" below was read from installed code, not from live traffic.
Everything under "Documented, unverified live" is what the artifacts and
generated SDK claim but nothing here has exercised against the real API.

## What's measured (read directly from the installed `calle-ai==0.6.0` SDK
and from `mobilize/transports/calle.py`)

Installed package: `calle-ai` 0.6.0, importable as `calle` (site-packages
path confirmed). It ships a hand-written thin client (`calle/client.py`,
`calle/calls.py`, `calle/errors.py`) plus an OpenAPI-generated model package
(`calle/generated/models/`). `mobilize/transports/calle.py` does **not**
use this SDK — it talks to the REST API directly over `httpx.AsyncClient`,
by deliberate design (documented in its own module docstring: the SDK's
`create_and_wait` blocks per-call, which breaks concurrent wave dispatch).
That means the SDK's own error-envelope parsing (`api_error_from_response`)
is not automatically inherited by mobilize's transport — see gap below.

**Terminal states.** `CallStatus` (`calle/generated/models/call_status.py`):
`queued`, `in_progress`, `completed`, `failed`, `canceled`. Mobilize's
`TERMINAL_STATUSES = {"completed", "failed", "canceled"}` in `calle.py`
matches this exactly — `queued`/`in_progress` correctly treated as
non-terminal (`poll()` returns `None`).

`AttemptStatus` (`attempt_status.py`) adds `dialing` as a non-terminal
attempt-level state on top of the call-level set.

`RecipientStatus` (`recipient_status.py`) is a **different, smaller** enum:
`pending`, `in_progress`, `completed`, `failed`, `skipped` — no `canceled`.
`_to_call_result` in `calle.py` checks
`recipient.get("status") in ("failed", "canceled")`; per the current
generated model, a recipient status of `"canceled"` cannot occur — that
branch is defensive/inert, not wrong, but it's checking for a value the
documented schema says a recipient never has. Worth a comment or removing
if a future SDK bump confirms it's truly unreachable.

**Task vs. attempt vs. provider identifiers — three distinct IDs.**
- `CallTaskObject`/call task `id` — the `call_...`-style ID returned by
  `POST /v1/calls` and stored as `call_id` throughout mobilize (ledger,
  `CallResult.call_id`, `smoketest_1_result.json`). This is the ID you poll
  with `GET /v1/calls/{id}`.
- `CallTaskAttempt.id` — "stable outbound attempt identifier," one per dial
  attempt under `recipients[].attempts[]`. Not the same namespace as the
  task ID.
- `CallTaskAttempt.provider_call_id` — "Provider call identifier for
  support correlation **when available**" (nullable). This is the field
  the execution plan requires for billing reconciliation. It is explicitly
  optional/nullable in the schema — a `None` value is a documented
  possibility, not a bug, and must be treated as inconclusive rather than
  a mismatch.

**Null structured results.** `CallTaskRecipient.structured_result` is
typed `CallTaskRecipientStructuredResultType0 | None`, and the docstring
is explicit: `null` means "CALL-E could not produce a schema-valid result
... from the terminal call evidence, or no `recipient_result_schema` was
provided" — i.e. null is a real, expected outcome, not an error. Mobilize's
`_to_call_result` already treats `structured_result` as possibly-empty
(`recipient.get("structured_result") or call.get("structured_result") or {}`)
and falls through to `can_come == "unknown"` handling, which is correct.

**Metadata binding.** `dispatch()` sends `metadata: {"candidate_id": ...}`
in the request body, matching `CreateCallRequest`'s generic
`metadata` field. `_to_call_result` reads `call.get("metadata")` back and
requires it to match the expected candidate — this round-trip is exactly
what the SDK's request/response model supports (metadata is opaque
passthrough, not validated by CALL-E itself as far as the generated types
show).

**Schema behavior.** `result_schema` (whole-task) and
`recipient_result_schema` (per-recipient) are separate, independent
optional fields on `CreateCallRequest` — mobilize only ever sends
`result_schema` (`MOBILIZE_RESULT_SCHEMA`, defined in
`mobilize/transports/base.py`), never `recipient_result_schema`. Per-recipient
schema isn't in use. `CreateCallRequest`'s docstring confirms
`result_schema`'s field `description`s are passed to CALL-E's own
extraction model — which is exactly the mechanism `final_position`'s long
descriptive text in `MOBILIZE_RESULT_SCHEMA` relies on.

**Region/locale.** `CallTaskRecipientRequest.region`/`.locale` are both
optional strings (`region`: "country or region code ... for example `US`";
`locale`: "BCP 47 locale hint ... for example `en-US`") — no enumerated
allow-list in the generated types, so support is determined server-side.
Mobilize's `dispatch()` sends per-candidate `region`/`locale` with a
transport-level default fallback, matching the request shape.

**Error codes (documented, not yet handled equivalently).**
`APIErrorCode` (`calle/generated/models/api_error_code.py`) lists, among
others: `unsupported_region`, `unsupported_language`, `invalid_phone`,
`recipient_blocked`, `rate_limit_exceeded`, `insufficient_balance`,
`idempotency_conflict`, `policy_violation`, `result_schema_invalid`,
`recipient_result_schema_invalid`, `call_not_ready`. The bundled SDK parses
these into typed exceptions (`CalleAuthenticationError` for 401/403,
`CalleRateLimitError` for 429, generic `CalleAPIError` otherwise) via
`api_error_from_response`. **Gap:** `mobilize/transports/calle.py` calls
`response.raise_for_status()` directly and does not parse the
`{"error": {"code": ..., "message": ...}}` envelope at all — an
`unsupported_region` or `rate_limit_exceeded` response currently surfaces
only as a generic `httpx.HTTPStatusError`, with none of the structured
`code` available to the dispatcher for differentiated handling (e.g.
distinguishing a permanent `unsupported_region` from a transient
`rate_limit_exceeded`). This is a real, currently-unaddressed gap — flagged
here as a follow-up since it's outside this verification task's scope.

**Idempotency.** `create()` accepts `idempotency_key` as a header
passthrough on the SDK side; mobilize sends the same via
`Idempotency-Key` on `dispatch()`, truncated to 255 chars, sourced from the
ledger's precomputed key. `idempotency_conflict` is a listed error code,
implying CALL-E does enforce idempotency server-side, but this sandbox
cannot verify the actual conflict-response shape or replay behavior live.

**Goal Runs.** `GoalRunStatus` mirrors `CallStatus`
(`queued`/`in_progress`/`completed`/`failed`/`canceled`).
`GoalRunErrorCode` adds outcome-shaped codes (`declined`, `no_answer`,
`timed_out`, `result_invalid`, etc.) not present in `APIErrorCode`. Mobilize
does not use Goal Runs at all (`calle.py` only calls `/v1/calls`) — this
surface is present in the installed SDK but out of scope for the current
transport.

## What's documented but NOT verified live in this sandbox

- Actual live response shapes, actual latency, actual per-region support
  matrix (the generated types have no region allow-list — only real traffic
  or CALL-E's hosted docs would show which regions/locales are actually
  accepted vs. rejected with `unsupported_region`).
- Real rate limits, real per-call pricing/billing granularity.
- Whether `provider_call_id` is populated in practice for calls placed
  through this account, or how often it's null.
- Real `idempotency_conflict` response behavior on a retried key.
- Whether webhook delivery (`calle.webhooks`, unused by mobilize) works as
  documented.

None of the above can be confirmed without live `CALLE_API_KEY` credentials.
This section should be re-run as a live smoke test once Ashraf has
credentials in an authorized environment.

## The call_id reconciliation problem

`mobilize/artifacts/smoketest_1_result.json` records:

```
"call_id": "call_2jcA9r17_ndyzxp8IYkkhA"
```

This is a **call task ID** — the ID returned by `POST /v1/calls` and used
for `GET /v1/calls/{id}` polling, per `CallTaskObject`/the task-level `id`
field. It is not, and was never meant to be, the same value as a billing
record's ID.

The two real historical billing-record IDs Ashraf has (from CALL-E's
dashboard, outside this repo): `b981d2935adf4053be2d9e6a96599ad4` and
`db39ddca6b684932bbdd908764740b58` — 32-character hex strings. Per the
generated schema, the correct field to compare against those is
`recipients[].attempts[].provider_call_id` ("provider call identifier for
support correlation"), **not** the call task ID and **not** a dashboard
attempt ID (`CallTaskAttempt.id`, a separate "stable outbound attempt
identifier" in its own namespace).

**This sandbox cannot perform that reconciliation, for two independent
reasons:**

1. No live API access — the actual `recipients[].attempts[].provider_call_id`
   for the historical `call_2jcA9r17_ndyzxp8IYkkhA` task cannot be retrieved
   here to compare against the two hex billing IDs.
2. `smoketest_1_result.json` itself doesn't preserve that field. It's a
   flattened, hand-summarized record (`call_id`, `outcome`,
   `commitment_score`, `evidence`, a merged `transcript`, `raw_status`) —
   it does not retain the raw `recipients[].attempts[]` array, so
   `provider_call_id` was never captured even at the time this smoketest
   ran. The `raw` `CallResult` field that `calle.py`'s `_to_call_result`
   populates (the full API response) is not what got persisted to this
   artifact.

Per the execution plan: **a missing/expired historical task is
inconclusive — do not invent a match.** This is exactly that case: neither
"the two hex billing IDs correspond to this smoketest call" nor "they
don't" can be asserted from what's stored. The correction is procedural,
not a fabricated resolution: `smoketest_1_result.json` needs a
`provenance_note` (added below, not a silent rewrite) recording that its
`call_id` is a task ID, is not directly comparable to the two hex billing
IDs, and that closing this gap requires either live re-polling of the
original task (if still retrievable — CALL-E task retention is unknown to
this sandbox) or capturing `provider_call_id` on any future real call.

## devpost_submission.md correction

`mobilize/artifacts/devpost_submission.md` (line 124, under "Accomplishments
that I'm proud of") states: *"The transcript is committed in the repo."*
This is false as of this review. `mobilize/artifacts/smoketest_2_result.json`
— the artifact for the "second real call" the sentence refers to — contains
no `transcript`, no `call_id`, and no raw `status` field at all; it's a
summary object (`need`, `mobilization_id`, a `result` block with
`filled`/`confirmed_count`/`outcome`/`commitment_score`/`note`). Corrected
in place with a dated note rather than silently deleted (see diff in that
file).

## Acceptance checklist

- [x] Redacted contract/evidence record (this file).
- [x] Current request/response shapes documented from the installed SDK's
      generated models (no live example available — none fabricated).
- [x] Unsupported-region handling: documented as a listed `APIErrorCode`
      (`unsupported_region`), explicitly flagged as unverified live and as
      not yet parsed by `mobilize/transports/calle.py`.
- [x] Measured vs. merely-documented capability list (above).
- [x] `call_id` mismatch: not resolved with fabricated evidence — flagged
      as an unresolved, currently-inconclusive discrepancy with a
      provenance/correction note added to the affected artifact, per the
      execution plan's explicit instruction not to invent replacement IDs.
- [x] No new calls placed, no credentials printed (none exist in this
      sandbox).
