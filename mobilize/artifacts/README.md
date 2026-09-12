# Real-call artifacts

Structured outcome results from real CALL-E calls (`outcome`,
`commitment_score`, and, where returned, a per-line transcript with
`offset_seconds` timing), committed here so the "is it really calling, or
just referencing?" question never has to be taken on faith. Phone numbers
are redacted before commit.

Not every result includes a transcript — `smoketest_2_result.json` records
only the mobilization outcome, no transcript or call_id. There is no
separate timing-log artifact beyond the per-line offsets inside a
transcript when one exists.

Populated during the demo phase — see the top-level README's Setup section
for how to place real calls.
