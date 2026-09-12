# Data-flow inventory

Scope: `mobilize/app/dashboard.py`, `mobilize/app/cli.py`, `mobilize/mcp/server.py`,
`mobilize/core/registry.py`, `mobilize/core/validation.py`. Threat model matches the
product as built: a single-operator, localhost-bound tool with no auth layer, not a
multi-tenant hosted service (public hosting is covered separately in
`mobilize/artifacts/e2_public_mode.md` and explicitly out of scope here).

## What's stored, where, how long, who can read it

| Data | Where | Retention | Who can access |
|---|---|---|---|
| Registry (names, real phone numbers, timezones, learned accept/show-up rates) | `/tmp/mobilize_dashboard_registry.json` (dashboard), in-process only for CLI/MCP unless `save_registry_json` is called | Until `/api/registry/reset` or process/tmp cleanup; CLI/MCP don't persist it at all | Anyone with filesystem access to the machine (same as any `/tmp` file); over the network, only whoever can reach the bound host:port (127.0.0.1 by default -- see below) |
| Registry source marker (sample vs. uploaded) | `/tmp/mobilize_dashboard_registry_source.txt` | Same as registry | Same as registry |
| Governance state (do-not-call list, cooldown/fatigue counters, by candidate id) | `/tmp/mobilize_dashboard_governance.json`, `/tmp/mobilize_real_governance.json` (CLI), `/tmp/mobilize_mcp_real_governance.json` (MCP) | Indefinite -- this is intentionally durable so DNC/cooldown persist across runs | Same filesystem-access scope. Contains candidate ids (phone-derived hashes), not raw phone numbers or names. |
| Call ledger (idempotency keys, dispatch/outcome records) | `/tmp/mobilize_dashboard_real_ledger.jsonl`, `/tmp/mobilize_real_ledger.jsonl`, `/tmp/mobilize_mcp_real_ledger.jsonl` (real paths); a fresh temp file per rehearsal run, deleted on completion | Real ledgers: indefinite (crash-resumption depends on this). Rehearsal ledgers: deleted in the `finally` block of `run_mobilization` | Same filesystem-access scope |
| CALL-E API key (`CALLE_API_KEY`) | Process environment only | Lifetime of the process's env | Never written to disk, logs, browser storage, or any HTTP response by this codebase; read once via `os.environ` in `CalleTransport`/CLI/MCP entry points |
| Phone numbers in HTTP/WS responses | `/api/registry`, WebSocket `call_result`/`final`/`error` events | Not persisted server-side beyond the response; browser may cache in DevTools network log (out of this app's control) | Whoever can open the page / connect the socket -- gated by origin check + localhost bind |
| Phone numbers in CLI/MCP output | stdout, or MCP tool response payload | Not persisted by this code; MCP tool responses may be logged/relayed by the calling agent, outside this app's control | Whoever runs the CLI, or the MCP client/agent |

## Masking

`mask_phone()` in `mobilize/core/validation.py` is the single shared implementation
(dashboard's own duplicate `_mask()` was removed in this pass and now aliases it).
Applied at every operator-facing surface that emits a phone number:
- Dashboard `/api/registry`, WebSocket `call_result`/`final` events.
- MCP `mobilize_real` preview response and invalid-phone error message.
Full unmasked phone numbers are used internally (governance keys, `CalleTransport`
dispatch, ledger idempotency keys) but never returned to a client or written to a log
by this codebase.

## Verified in this pass

- **WebSocket origin check** (`_origin_is_trusted`, dashboard.py) -- already present
  and covered by `test_dashboard_security.py::test_untrusted_origin_is_rejected`.
  Confirmed still enforced; no change needed.
- **Real-dispatch confirm gate** -- already present and tested; confirmed still
  enforced.
- **DOM escaping** (`esc()` in the dashboard's inline JS) -- every place registry or
  call data reaches `innerHTML` in `_PAGE` routes through `esc()`. Confirmed by
  reading the full script block; no gap found.
- **CSV formula injection** (`=`, `+`, `-`, `@` prefixed cells) -- examined. This app
  never re-serializes registry data back into a CSV file for a coordinator to open in
  Excel/Sheets (`registry.py` only writes JSON via `save_registry_json`; there is no
  export endpoint). A CSV formula payload uploaded through `/api/registry/upload` is
  stored and displayed as an escaped string in the dashboard table, never re-emitted
  as CSV. No live formula-injection vector exists in the current product; noting this
  rather than adding a defense against a risk that isn't actually reachable.
- **Credentials in browser storage / client bundle** -- `_PAGE`'s inline JS contains
  no secrets; `CALLE_API_KEY` never crosses into the HTML response or WS payloads.

## Fixed in this pass

- **Type confusion on `confirm`/`simulate`** (dashboard.py `/ws/run`): the old code
  used `bool(params.get("confirm", False))`. Python's `bool()` coerces *any*
  non-empty string to `True`, including the string `"false"` -- a client sending
  `{"confirm": "false"}` was silently treated as `confirm: true`, bypassing the
  intended real-dispatch confirmation gate. Fixed by `_validate_run_params()`, which
  requires `confirm`/`simulate` to be actual JSON booleans and rejects anything else.
  See `mobilize/tests/test_dashboard_param_validation.py::test_confirm_as_truthy_string_is_rejected_not_coerced_to_true`.
- **No bounds checking on `need_count`/`max_calls`/`deadline_minutes`**: non-positive
  values reached `Need`/`mobilize()` unvalidated. A non-positive `need_count` makes
  dispatcher.py's `len(confirmed) >= need.count` true before any call is placed,
  reporting the need as "filled" with zero confirmations -- a false success report to
  the coordinator. Fixed at the dashboard boundary (not by changing dispatcher
  policy) via `_validate_run_params()`.
- **Duplicated phone-masking logic**: dashboard.py had its own `_mask()` reimplementing
  `mask_phone()` from `validation.py`, with a subtly different formula (no `max(1, ...)`
  floor on the star count). Not independently exploitable given E.164's minimum length,
  but two divergent implementations of the same redaction logic is exactly how one of
  them silently stops redacting after an unrelated future edit. Now a plain alias.

## Not in scope / explicitly deferred

- Public hosted deployment, recipient authorization for a live mode, credit-abuse
  protection under exposure, and any new auth layer -- these belong to the public-mode
  work in `e2_public_mode.md`, not this inventory.
  This product remains deliberately localhost-bound; no change here makes it safe to
  bind to `0.0.0.0`.
- Legal compliance: the four governance rules (do-not-call, cooldown, fatigue,
  calling-hours) are operational safeguards this codebase enforces mechanically. They
  are not a substitute for, and this document makes no claim of, blanket legal
  compliance (TCPA or equivalent) in any jurisdiction.
