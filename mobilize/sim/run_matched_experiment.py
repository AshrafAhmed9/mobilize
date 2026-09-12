"""CLI entry point for the B1 matched policy experiment.

Runs all five policies (production ranked-greedy, sequential, fixed-size
waves, stated-yes-only, call-all) against the SAME pre-generated ground
truth for each trial, across a dev seed set and a genuinely separate
held-out seed set, at several roster/need sizes. Prints per-policy
aggregates plus paired bootstrap CIs for ranked_greedy_real vs. each other
policy. Intended to be piped straight into the artifacts file -- this is
not a demo, its stdout IS the evidence.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time

from mobilize.core.types import Need
from mobilize.sim.matched_experiment import (
    POLICIES,
    TrialRow,
    bootstrap_diff_ci,
    build_trial_rows,
    pregenerate_trial,
    run_all_policies,
    summarize_policy,
)

# Frozen, documented seed sets. Dev seeds are used for nothing but sanity
# checking during development; the held-out seeds below were never inspected
# or tuned against while building the policies above.
DEV_SEEDS = list(range(2000, 2020))        # 20 seeds
HELD_OUT_SEEDS = list(range(9000, 9040))   # 40 seeds, disjoint range from dev and from the old harness's 1000-1199

# (pool_size, need_count) sweep -- three sizes as required.
SIZE_SWEEP = [
    (20, 2),
    (60, 5),
    (150, 10),
]


async def run_split(seeds: list[int], ledger_dir: str) -> tuple[list[TrialRow], dict]:
    all_rows: list[TrialRow] = []
    for pool_size, need_count in SIZE_SWEEP:
        need = Need(label="O-negative blood needed", count=need_count, deadline_minutes=60, location="City Hospital", max_calls=min(pool_size, 40))
        for seed in seeds:
            gt = pregenerate_trial(pool_size, seed=seed)
            ledger_path = f"{ledger_dir}/trial_{pool_size}_{need_count}_{seed}.jsonl"
            results = await run_all_policies(need, gt, ledger_path)
            all_rows.extend(build_trial_rows(gt, results))
    return all_rows, {"seeds": seeds, "sizes": SIZE_SWEEP}


def per_policy_rows(rows: list[TrialRow]) -> dict[str, list[TrialRow]]:
    out: dict[str, list[TrialRow]] = {p: [] for p in POLICIES}
    for r in rows:
        out[r.policy].append(r)
    return out


def main() -> None:
    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        dev_rows, dev_meta = asyncio.run(run_split(DEV_SEEDS, tmp))
        heldout_rows, heldout_meta = asyncio.run(run_split(HELD_OUT_SEEDS, tmp))
    elapsed = time.time() - t0

    report: dict = {
        "wall_clock_seconds": round(elapsed, 2),
        "dev": {"meta": dev_meta, "n_rows": len(dev_rows)},
        "held_out": {"meta": heldout_meta, "n_rows": len(heldout_rows)},
        "aggregates": {},
        "bootstrap_ci_vs_ranked_greedy_real": {},
        "raw_trial_rows_held_out": [],
    }

    for split_name, rows in (("dev", dev_rows), ("held_out", heldout_rows)):
        by_policy = per_policy_rows(rows)
        report["aggregates"][split_name] = {p: summarize_policy(by_policy[p]) for p in POLICIES}

        # Paired bootstrap: for each policy vs. ranked_greedy_real, on the
        # 'counted target attainment reached' indicator and on calls_used,
        # matched by (seed, pool_size, need_count) so pairs really are the
        # same trial under two policies.
        baseline_rows = {(r.seed, r.pool_size, r.need_count): r for r in by_policy["ranked_greedy_real"]}
        ci_block = {}
        for policy in POLICIES:
            if policy == "ranked_greedy_real":
                continue
            other_rows = {(r.seed, r.pool_size, r.need_count): r for r in by_policy[policy]}
            keys = [k for k in baseline_rows if k in other_rows]
            keys.sort()
            base_calls = [baseline_rows[k].calls_used for k in keys]
            other_calls = [other_rows[k].calls_used for k in keys]
            base_precision = [
                (baseline_rows[k].accepted_roster_true_showups / baseline_rows[k].accepted_roster_size)
                if baseline_rows[k].accepted_roster_size else 0.0
                for k in keys
            ]
            other_precision = [
                (other_rows[k].accepted_roster_true_showups / other_rows[k].accepted_roster_size)
                if other_rows[k].accepted_roster_size else 0.0
                for k in keys
            ]
            ci_block[policy] = {
                "calls_used_diff_(ranked_greedy_real_minus_policy)": bootstrap_diff_ci(base_calls, other_calls),
                "accepted_roster_precision_diff_(ranked_greedy_real_minus_policy)": bootstrap_diff_ci(base_precision, other_precision),
                "n_paired_trials": len(keys),
            }
        report["bootstrap_ci_vs_ranked_greedy_real"][split_name] = ci_block

    # Raw per-trial data for the held-out split (dev raw rows omitted from
    # the printed report for length; dev exists only to confirm the harness
    # behaves sanely before looking at held-out numbers, per the plan's
    # "don't tune against held-out seeds" requirement).
    report["raw_trial_rows_held_out"] = [
        {
            "policy": r.policy, "seed": r.seed, "pool_size": r.pool_size, "need_count": r.need_count,
            "filled_counted": r.filled_counted, "accepted_roster_size": r.accepted_roster_size,
            "accepted_roster_true_showups": r.accepted_roster_true_showups,
            "contacted_count": r.contacted_count, "contacted_true_showups": r.contacted_true_showups,
            "calls_used": r.calls_used, "dispatch_rounds": r.dispatch_rounds,
            "ambiguous_count": r.ambiguous_count,
            "modeled_time_to_fill_s": r.modeled_time_to_fill_s,
        }
        for r in heldout_rows
    ]

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
