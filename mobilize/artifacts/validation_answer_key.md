# Real-call validation answer key — pre-declared 12 September 2026

Written and committed **before any call in this batch is placed**, per
`validation_protocol.md` Part B. Each row is a scripted intent Ashraf will say on a
real call to his own number, with the verdict the current dispatcher/commitment code
is expected to produce. Results go in `real_call_validation/` after the calls, compared
against this table unedited.

Product revision at time of writing: HEAD, working tree dirty (see
`final_contribution_manifest.md` for the exact modified-file list). This is the first
real-call run against the current `final_position` schema field and retraction-hardened
commitment scoring — none of the historical smoketests exercised this code.

| # | Scripted intent | What Ashraf says (roughly, natural variation expected) | Expected outcome | Expected confirmed? |
|---|---|---|---|---|
| 1 | Firm yes | "Yes, I can help. I'll be there in about 10 minutes." | `firm_yes`, commitment score comfortably above 0.55 | Yes |
| 2 | Hedge | "Maybe, I'll try, I'm not sure I can make it work." | `soft_yes` or below-threshold score | No |
| 3 | Yes then retract | "Yes, I'll come... actually wait, scratch that, something came up, I can't." | Verdict driven by `final_position`, NOT the initial yes — not confirmed | No |
| 4 | Mid-call opt-out | "Actually, please don't call me again about this." | Do-not-call path triggered; recorded in governance/opt-out list, not just scored as a refusal | No, and DNC recorded |
| 5 | No answer / deliberate non-response | Let it ring out or stay silent | `no_answer` or equivalent, commitment score 0.00, not confirmed | No |

## Rules for this batch

- Each call uses the current, unmodified dispatcher path (not rehearsal mode) —
  `mobilize()` called for real, against Ashraf's own real phone number.
- Capture the raw CALL-E payload (`GET /v1/calls/{id}` output, phone redacted) and the
  resulting `CallResult` for every call, saved as
  `mobilize/artifacts/real_call_validation/call_N_raw.json` and
  `call_N_result.json`.
- Record actual elapsed call time and any real credit cost from the billing dashboard.
- If a result contradicts the expected verdict, that is not an error to hide — it is
  either a real bug (fix it, add a regression test, document it) or a sign the script
  needs a clearer verbal cue on retry. Report failures as failures.
- After all calls, write `mobilize/artifacts/validation_results.md` comparing this
  table to actual results row by row, N/N, with links to the raw payloads.
