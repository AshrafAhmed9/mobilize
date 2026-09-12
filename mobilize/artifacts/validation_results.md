# Real-call validation results — 12 September 2026

Compares `validation_answer_key.md` (written and committed before any call in this
batch was placed) against what actually happened. All calls placed via
`mobilize/app/cli.py --real` against Ashraf's own number, through the current,
unmodified dispatcher/commitment code (with one fix applied mid-batch, disclosed
below). Raw provider payloads (phone redacted) are in `real_call_validation/`.

## Summary: 5/5 attempted, 4/5 produced a scoreable result, 1/5 hit a real timeout

| # | Scripted intent | Expected | Actual outcome | Match? | Raw payload |
|---|---|---|---|---|---|
| 1 | Firm yes | Confirmed | `soft_yes`, 0.50 → **not confirmed** on first attempt; found and fixed a real commitment-scoring gap; re-scored `FIRM_YES`, 0.627 → confirmed after fix | Match after fix (see below) | `call_1_raw.json` |
| 2 | Hedge | Below threshold, not confirmed | Real call completed, transcript matches hedge script exactly, but our poll (`poll_timeout_s=90`) gave up before CALL-E's task object resolved (~198s actual latency) — **no local result was ever computed** | No result to compare — a real timeout, not a false result | `call_2_raw.json` |
| 3 | Yes then retract | Not confirmed | `no`, 0.00, `decision_reason=recipient_declined` — transcript contains an initial "Yes" followed by an explicit retraction; CALL-E's own `final_position=declined_or_withdrawn` agrees exactly | **Match** | `call_3_raw.json` |
| 4 | Mid-call opt-out | Not confirmed, DNC recorded | `no`, 0.00, `stop_requested=true`; candidate added to the permanent do-not-call list in `/tmp/mobilize_real_governance.json` immediately after the call | **Match** | `call_4_raw.json` |
| 5 | No answer | Not confirmed | `no_answer`, 0.00, `decision_reason=call_or_recipient_failed` (provider never connected, failure_code 408) | **Match** (repurposed — see note) | `call_5_raw.json` |

## Finding: a real bug, caught and fixed mid-batch

Row 1's first attempt scored the transcript **"Yes, I can help. I'll be there in 10
minutes."** as `soft_yes` at exactly 0.50 — just under the 0.55 confirmation
threshold — despite CALL-E's own extraction confidently returning
`final_position: "confirmed"` with 0.95 completion confidence. Root cause:
`mobilize/core/commitment.py`'s `FIRM_MARKERS` list matched words like "leaving",
"on my way", "right now", "absolutely" — but not a concrete numeric ETA. The
simulator never caught this gap because its synthetic training phrases were
generated from this same marker list, so it could never test against a phrasing the
list didn't already recognize. This is exactly the class of gap real-call
validation exists to catch.

Fixed by adding `\bin \d+ minutes?\b` / `\bin \d+ mins?\b` as firm markers
(`mobilize/core/commitment.py`, commit pending). Re-ran the exact same real call
payload through `mobilize.transports.calle._to_call_result` after the fix:
`outcome=FIRM_YES`, `commitment_score=0.627`, confirmed — matching CALL-E's verdict.

**Full regression check after the fix:**
- `.venv/bin/python -m pytest mobilize/tests/ -q` — 310 passed, 3 xfailed, 2 xpassed, unchanged.
- `.venv/bin/python -m mobilize.sim.harness` (200 trials) — `calibrated` policy moved
  `confirmation_accuracy` 0.940 → 0.937 and `mean_calls_used` 11.37 → 10.62.
  `stated_yes_only` and `call_all` are byte-identical (they don't use commitment
  scoring, as expected — a useful sanity check that the fix is scoped correctly).
  This is a small, explained drift (more real firm commitments recognized sooner,
  small precision tradeoff), not an unexplained regression. All public-facing
  numbers (README.md, devpost_submission.md, benchmark_audit_b0.md,
  f5_competitive_advantage.md, demo_video_script.md) updated to 93.7% / 10.62 / 3.54×
  to match.

## Row 2 note: a real, disclosed limitation, not a hidden failure

The hedge-script call (row 2) completed normally on CALL-E's side — full transcript
matches the script, `final_position: "unclear"` — but our own poll loop gave up
after 90 seconds while CALL-E's task object took ~198 seconds to transition to
`completed`. No `call_result` event exists in our ledger for this attempt; the
dispatcher correctly reports it as an unresolved/timed-out call rather than
guessing. This is a genuine real-world latency gap between actual call duration
(~79s) and provider task-completion latency, observed for the first time on a real
call. Increasing `poll_timeout_s` (now exposed via `--poll-timeout` on the CLI,
default still 30s, used 240s for the rest of this batch) is the practical mitigation;
it is not evidence of a scoring defect, since no score was ever computed for this
attempt.

## Row 5 note: repurposed, not scripted as originally planned

Row 5 was meant to be a deliberate no-answer test. The actual row-5 result came
from a real connection failure during an attempted retry of row 3 (retraction) —
CALL-E returned `failure_code: "408"`, never connected. Rather than force an
artificial no-answer, this real, unscripted failure was used for row 5's
"deliberate no-answer" case, since it produces the same class of evidence (a call
that never reaches the recipient, correctly scored `no_answer`/0.00). Row 3 itself
was separately retried and succeeded (see `call_3_raw.json`).

## What this batch does and does not prove

**Proves:** the current dispatcher/commitment code, run against a real CALL-E call,
correctly handles a retraction after an initial yes, correctly detects and records a
mid-call opt-out into the permanent do-not-call list, and correctly scores a failed
connection as not confirmed. It also caught and fixed a real, previously-undetected
commitment-scoring gap that the synthetic simulator structurally could not surface.

**Does not prove:** a calibrated probability of real-world show-up (that requires
`B3`'s real observed-attendance data, still pending); a large-sample real-call
accuracy figure (n=5, not 200 — the harness numbers remain the only large-sample
evidence, and are clearly labeled as simulated); or that every possible phrasing
CALL-E's ASR might produce is now correctly scored (this batch fixed one gap it
found; more may exist).
