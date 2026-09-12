# Final contribution manifest

Purpose: let a reviewer go Devpost → PR → exact code → reproduction without guessing
a branch. This document pins the final application commit, states its relation to the
already-merged sponsor PR, and records what evidence/build artifacts exist versus what
is still pending. It does not open, push, or submit anything — see EXECUTION_PLAN.md
E3 and this project's CLAUDE.md scope limits.

## 1. Final application commit

- HEAD: `35ff46782dd785a5bdeb4e6fbfeb04b86ca39dff`
- Subject: "Add final_position schema field: delegate open-ended retraction detection
  to CALL-E's own extraction, cross-validated"

**Working tree is dirty.** This manifest reflects the last commit above, not the
uncommitted work sitting on top of it. Before any follow-up PR is opened, the
uncommitted changes below need to be committed (or explicitly excluded) so the PR
maps to a single reviewable commit.

Modified (tracked files with uncommitted edits):
`README.md`, `mobilize/app/cli.py`, `mobilize/app/dashboard.py`,
`mobilize/artifacts/README.md`, `mobilize/artifacts/demo_video_script.md`,
`mobilize/artifacts/devpost_submission.md`, `mobilize/artifacts/smoketest_1_result.json`,
`mobilize/artifacts/smoketest_2_result.json`, `mobilize/core/dispatcher.py`,
`mobilize/core/ledger.py`, `mobilize/core/registry.py`, `mobilize/core/types.py`,
`mobilize/mcp/server.py`, `mobilize/tests/test_ambiguous_dispatch_reconciliation.py`,
`mobilize/tests/test_crash_safety.py`, `mobilize/tests/test_registry.py`,
`mobilize/transports/base.py`, `mobilize/transports/calle.py`

Untracked (new files not yet committed): `.claude/`, `.codex/`, `.gitattributes`,
`AGENTS.md`, `CLAUDE.md`, `COMPETITION.md`, `EXECUTION_PLAN.md`, `PLAN_REVIEW.md`,
`graphify-out/`, `mobilize/app/public_mode.py`,
`mobilize/app/sample_data/sample_shift_registry.csv`, and a batch of new
`mobilize/artifacts/*.md`/`*.json` evidence files and `mobilize/tests/test_*.py` /
`mobilize/sim/*.py` source files (full list: `b2_dispatch_policy_analysis.md`,
`benchmark_audit_b0.md`, `c1_annotation_rubric.md`, `c3_decision_layer_comparison.md`,
`calle_contract_c0.md`, `d4_sample_runs.md`, `d4_sample_runs.py`,
`data_flow_inventory.md`, `e1_platform_and_reset.md`, `e2_public_mode.md`,
`evidence_index.md`, `matched_policy_experiment_b1.md`,
`matched_policy_experiment_b1_raw_output.json`, `submission_readiness.md`,
`validation_protocol.md`, `sim/fixture_transport.py`, `sim/matched_experiment.py`,
`sim/run_matched_experiment.py`, and `tests/test_a2_durable_dispatch.py`,
`tests/test_b2_planner_invariants.py`, `tests/test_c1_final_intent_corpus.py`,
`tests/test_c3_decision_layer_comparison.py`, `tests/test_calle_error_classification.py`,
`tests/test_d3_dashboard_handoff.py`, `tests/test_d4_domain_isolation.py`,
`tests/test_dashboard_param_validation.py`, `tests/test_dashboard_rehearsal_isolation.py`,
`tests/test_decision_provenance.py`, `tests/test_e1_fixture_scenarios.py`,
`tests/test_e1_mcp_roundtrip_and_real_call_gate.py`, `tests/test_e2_public_mode.py`,
`tests/test_governance_staleness.py`, `tests/test_matched_experiment.py`).

This is substantial work-in-progress beyond the last commit. It must land in one or
more real commits before a follow-up PR is prepared — this manifest is not a substitute
for committing it.

## 2. Original merged PR reference

- Merged submission PR: https://github.com/CALLE-AI/awesome-phone-call-agents/pull/98
  (cited in `mobilize/artifacts/devpost_submission.md`, "Try it out" section, alongside
  the app repo `https://github.com/AshrafAhmed9/mobilize`).

- **What's new since it merged is not precisely determinable from local git history
  alone.** This local repository has no tag, branch, or commit message that marks
  which commit PR #98 was built from — `git log --all` shows no ref containing "98",
  and the devpost doc gives no commit SHA for the merge point. The closest available
  signal: `mobilize/artifacts/devpost_submission.md` was last touched (before the
  current dirty edits) at commit `587997f` (2026-08-07, "feat: rebuild as a coordinator
  product, not a developer engine"). Everything from `587997f` through the current HEAD
  `35ff467` (16 commits: crash-safety and self-review fixes, dashboard security fixes,
  wave-dispatch/ambiguous-dispatch reconciliation, candidate identity stabilization,
  commitment-firmness scoring from recipient's own words, negation/retraction handling,
  and the final_position schema field) is a plausible upper bound on post-merge work,
  but this is inferred, not confirmed against the sponsor repo's merge commit. Confirming
  the exact merge-base requires diffing against the sponsor's PR #98 branch/commit
  directly (not done here — this is docs-only, local-only work).

## 3. Dependency snapshot

- `pyproject.toml` (repo root) is the only dependency manifest. No `requirements.txt`
  and no lock file (`poetry.lock`, `uv.lock`, `Pipfile.lock`, etc.) exist in the repo.
- Dependencies are **not exactly version-pinned** — all entries use lower-bound
  constraints (`calle-ai>=0.6.0`, `httpx>=0.27`, `pydantic>=2.6`, `fastapi>=0.110`,
  `uvicorn>=0.29`, `websockets>=12.0`, `python-dotenv>=1.0`, `mcp>=2.0`; dev extras
  `pytest>=8.0`, `hypothesis>=6.100`, `pytest-asyncio>=0.23`). A clean install today
  can resolve newer versions than whatever was used to produce the recorded evidence.
  If exact reproducibility is required, a lock file (or a `pip freeze` snapshot of the
  working `.venv`) should be captured before recording the demo video — not yet done.

## 4. Evidence dataset

Current contents of `mobilize/artifacts/` (one line each):

- `README.md` — artifacts index/overview (currently has uncommitted edits).
- `evidence_index.md` — the F2 judge-readable technical evidence index linking
  call → decision → dispatch → roster and reproduction commands.
- `devpost_submission.md` — Devpost entry draft, including the PR #98 "Try it out" link.
- `demo_video_script.md` — draft script for the F3 three-minute video.
- `benchmark_audit_b0.md` — audit of prior benchmark methodology/claims.
- `matched_policy_experiment_b1.md` + `matched_policy_experiment_b1_raw_output.json` —
  B1 matched-policy comparison writeup and its raw trial data.
- `b2_dispatch_policy_analysis.md` — B2 dispatch policy analysis.
- `c1_annotation_rubric.md` — rubric used for conversation-intent annotation.
- `c3_decision_layer_comparison.md` — C3 decision-layer ablation/comparison study.
- `calle_contract_c0.md` — CALL-E provider contract/interface notes (C0).
- `data_flow_inventory.md` — inventory of data flow through the system.
- `d4_sample_runs.md` + `d4_sample_runs.py` — D4 second-domain sample run writeup/script.
- `e1_platform_and_reset.md` — E1 platform parity and state-reset notes.
- `e2_public_mode.md` — E2 public no-call demo mode writeup.
- `validation_protocol.md` — F4 practitioner/independent-observation collection protocol.
- `submission_readiness.md` — F6 living objection register for blind judge review.
- `smoketest_1_result.json`, `smoketest_2_result.json` — recorded smoketest run outputs
  (currently have uncommitted edits).
- `screenshots/` — dashboard screenshots referenced from the README.
- `final_contribution_manifest.md` — this file.

## 5. Video and deployment version

- **No video has been recorded.** `demo_video_script.md` is a draft script only; F3
  (three-minute video) is not yet filmed.
- **No public deployment has been made.** No hosted/public URL exists yet; E2's public
  mode (`mobilize/app/public_mode.py`) is implemented in the working tree but is
  uncommitted and not deployed anywhere reachable by a judge.
- Both are marked **pending**. Nothing here is fabricated as complete.

## 6. Sponsor validation script

- `scripts/validate_repository.py` was searched for in this local checkout and
  **not found** (`find . -iname "validate_repository.py"` returned nothing). This is
  expected: this repository is `mobilize` (the application), not the sponsor's
  `awesome-phone-call-agents` checkout where that script lives.
- Running the sponsor's contribution validation therefore **requires a separate
  checkout of `CALLE-AI/awesome-phone-call-agents`** and has **not been done** as part
  of this manifest. Do not treat this as a pass — it is an open step, tracked here as
  pending until run against the sponsor's actual repository.

## Status summary

| Item | Status |
|---|---|
| Final application commit | Pinned: `35ff46782dd785a5bdeb4e6fbfeb04b86ca39dff`; uncommitted work on top, listed above |
| Relation to merged PR #98 | Approximate only — no exact merge-base found in local history |
| Dependency snapshot | `pyproject.toml`, lower-bound pins only, no lock file |
| Evidence dataset | Listed in full above; exists in working tree, partly uncommitted |
| Video | Pending — not recorded |
| Public deployment | Pending — not deployed |
| Sponsor `validate_repository.py` | Not run — requires sponsor checkout, not available locally |
