"""Crash-safety proof: kill -9 the dispatcher process mid-mobilization,
restart against the same ledger file, and assert zero duplicate dials and
zero lost confirmations.

This runs the dispatcher in a real subprocess so SIGKILL is a true,
un-catchable process kill -- not a simulated exception -- matching the
methodology used for the KV-store project's crash-safety tests.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WORKER_SCRIPT = """
import asyncio
import os
import sys
import time

sys.path.insert(0, {repo_root!r})

from mobilize.core.dispatcher import mobilize
from mobilize.core.ledger import Ledger
from mobilize.core.types import Need
from mobilize.sim.population import generate_population
from mobilize.transports.simulated import SimulatedTransport

async def main():
    donors = generate_population(300, seed=7)
    transport = SimulatedTransport(donors, seed=7, min_latency_s=0.3, max_latency_s=1.5)
    pool = [d.candidate for d in donors]
    need = Need(label="O-negative blood needed", count=3, deadline_minutes=60,
                location="City Hospital", max_calls=40)
    ledger = Ledger({ledger_path!r})

    def on_progress(event, data):
        if event == "wave_dispatch":
            with open({marker_path!r}, "w") as f:
                f.write("wave_dispatched")
        print(event, data, flush=True)

    result = await mobilize(need, pool, transport, ledger=ledger, on_progress=on_progress,
                             mobilization_id="mob_crashtest", recovery_timeout_s={recovery_timeout_s})
    print("COMPLETED", result.filled, result.calls_used, flush=True)
    print("AMBIGUOUS", sorted(result.ambiguous_candidate_ids), flush=True)

asyncio.run(main())
"""


def _run_worker(ledger_path: str, marker_path: str, timeout: float, recovery_timeout_s: float = 5.0) -> subprocess.Popen:
    script = WORKER_SCRIPT.format(
        repo_root=REPO_ROOT, ledger_path=ledger_path, marker_path=marker_path,
        recovery_timeout_s=recovery_timeout_s,
    )
    venv_python = os.path.join(REPO_ROOT, ".venv", "bin", "python3")
    python_bin = venv_python if os.path.exists(venv_python) else sys.executable
    return subprocess.Popen(
        [python_bin, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def test_kill_minus_9_mid_dispatch_then_resume_no_duplicates_no_losses():
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = os.path.join(tmp, "ledger.jsonl")
        marker_path = os.path.join(tmp, "wave_dispatched.marker")

        # First run: kill it the instant it has dispatched wave 0, before any
        # results have been recorded -- the worst case for double-dialing.
        proc = _run_worker(ledger_path, marker_path, timeout=10)
        deadline = time.time() + 10
        while not os.path.exists(marker_path) and time.time() < deadline:
            time.sleep(0.02)
        assert os.path.exists(marker_path), "worker never reached wave dispatch before timeout"

        time.sleep(0.05)  # let the dispatch loop log a couple of entries
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=5)
        assert proc.returncode != 0  # confirms it was actually killed mid-flight, not finished

        assert os.path.exists(ledger_path)
        with open(ledger_path) as f:
            lines_after_crash = [json.loads(line) for line in f if line.strip()]
        dispatched_after_crash = {e["candidate_id"] for e in lines_after_crash if e["kind"] == "dispatched"}
        assert len(dispatched_after_crash) > 0, "expected at least one dispatch logged before the kill"

        # Second run: same ledger file, same mobilization_id. It must not
        # re-dial anyone already in the ledger.
        from mobilize.core.ledger import Ledger

        ledger = Ledger(ledger_path)
        already = {c for c in dispatched_after_crash if ledger.already_dispatched("mob_crashtest", c)}
        assert already == dispatched_after_crash

        # SimulatedTransport keeps call state in memory only (a documented,
        # deliberate property -- see its own docstring), so a fresh instance
        # in this second process genuinely cannot resolve the pre-crash
        # call_ids no matter how long recovery polls. A short recovery
        # timeout here just keeps the test fast; the outcome (unresolved)
        # would be the same at any timeout for this transport.
        resume_proc = _run_worker(ledger_path, marker_path, timeout=30, recovery_timeout_s=0.3)
        out, _ = resume_proc.communicate(timeout=30)

        with open(ledger_path) as f:
            mid_entries = [json.loads(line) for line in f if line.strip()]

        dispatched_candidates = [e["candidate_id"] for e in mid_entries if e["kind"] == "dispatched"]

        # The critical crash-safety assertion: no candidate was ever
        # dispatched twice, even though the process was killed and the
        # dispatcher restarted cold against the same ledger file.
        assert len(dispatched_candidates) == len(set(dispatched_candidates)), (
            f"duplicate dispatch detected after crash+restart: {dispatched_candidates}"
        )
        # No confirmation recorded before the crash was lost.
        pre_crash_results = {e["candidate_id"] for e in lines_after_crash if e["kind"] == "result"}
        result_candidates_mid = {e["candidate_id"] for e in mid_entries if e["kind"] == "result"}
        assert pre_crash_results.issubset(result_candidates_mid)

        # A2: the wave-0 candidates dispatched before the kill genuinely
        # cannot be recovered from this transport -- each of them may still
        # be a real, live call as far as this process can tell. The correct,
        # safe behavior is to REFUSE to dispatch a further wave on top of
        # that uncertainty, not to plow ahead and potentially double-recruit.
        # This is a real behavior change from before this task: previously
        # `call_timed_out`/`recovery_unresolved` candidates were logged but
        # never actually blocked further dispatch.
        assert "COMPLETED False" in out, f"expected safe refusal to proceed while recovery is unresolved:\\n{out}"
        unresolved_after_crash = set(dispatched_after_crash) - result_candidates_mid
        assert unresolved_after_crash, "test setup issue: nothing was actually left unresolved to reconcile"

        # Now the "successful reconciliation" half of the acceptance
        # criteria: an operator reviews the unresolved candidates (e.g. via
        # CALL-E's own dashboard/call log, out of band) and explicitly
        # reconciles each one. This does not erase the original ambiguous
        # ledger entries, and does not let those specific candidates be
        # redialed -- it only stops them from blocking further waves.
        for candidate_id in unresolved_after_crash:
            ledger.record_reconciliation(
                "mob_crashtest", candidate_id,
                decision="operator_confirmed_no_further_action_needed",
                operator="test-harness",
                note="reviewed out of band; simulated transport cannot recover state after restart",
            )

        final_proc = _run_worker(ledger_path, marker_path, timeout=30, recovery_timeout_s=0.3)
        out2, _ = final_proc.communicate(timeout=30)

        with open(ledger_path) as f:
            final_entries = [json.loads(line) for line in f if line.strip()]
        final_dispatched = [e["candidate_id"] for e in final_entries if e["kind"] == "dispatched"]

        # Still never a duplicate dispatch, across all three runs.
        assert len(final_dispatched) == len(set(final_dispatched)), (
            f"duplicate dispatch detected after reconciliation: {final_dispatched}"
        )
        # The reconciled candidates were never redispatched -- reconciliation
        # unblocks further waves, it never reopens the candidate itself.
        assert unresolved_after_crash.issubset(set(final_dispatched))
        assert len(set(final_dispatched) - set(dispatched_after_crash)) >= 0  # new candidates may be dispatched now
        assert "COMPLETED True" in out2, f"mobilization did not complete after reconciliation:\\n{out2}"
