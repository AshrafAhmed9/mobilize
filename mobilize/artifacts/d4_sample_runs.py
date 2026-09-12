"""D4 reproducible sample runs: donor coordination and emergency shift
coverage through the SAME engine (core/dispatcher.mobilize), each reading
domain-appropriate eligibility off Registry.candidates(domain=...).

Both runs use the free simulator (_RegistryBackedSimulatedTransport, the
same class the dashboard's rehearsal mode uses) -- no real calls, no
CALL-E credits spent. This is what D4's acceptance criteria calls
"reproducible... doesn't need to be a live call".

Run:
    .venv/bin/python -m mobilize.artifacts.d4_sample_runs

See mobilize/artifacts/d4_sample_runs.md for what each run demonstrates
and sample output.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from mobilize.app.dashboard import _RegistryBackedSimulatedTransport
from mobilize.core.dispatcher import mobilize
from mobilize.core.ledger import Ledger
from mobilize.core.registry import RegistryDomain, load_registry_csv
from mobilize.core.types import Need

APP_DIR = Path(__file__).resolve().parent.parent / "app"


async def run_donor_scenario() -> None:
    registry = load_registry_csv(APP_DIR / "sample_data" / "sample_registry.csv")
    candidates = registry.candidates(RegistryDomain.DONOR)  # default min_days_between_donations=56
    need = Need(label="O-negative blood needed urgently", count=3, deadline_minutes=60,
                location="City Hospital", max_calls=40)
    transport = _RegistryBackedSimulatedTransport(registry)
    ledger = Ledger("/tmp/mobilize_d4_sample_donor_ledger.jsonl")
    result = await mobilize(need, candidates, transport, ledger=ledger, mobilization_id="d4_sample_donor")
    _report("DONOR", registry, candidates, result)


async def run_shift_scenario() -> None:
    registry = load_registry_csv(APP_DIR / "sample_data" / "sample_shift_registry.csv")
    shift_start = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    shift_end = datetime(2026, 9, 12, 20, 0, tzinfo=timezone.utc)
    candidates = registry.candidates(
        RegistryDomain.SHIFT, required_skill="RN", shift_start=shift_start, shift_end=shift_end
    )
    # A demonstrated shortage case: need 5 RNs, only 2 qualified (s001, s003)
    # exist in the sample -- the run cannot fill, by design, to prove
    # shortage handling is visible rather than hidden.
    need = Need(label="RN coverage, night shift", count=5, deadline_minutes=45,
                location="Downtown ER", max_calls=10, required_skill="RN",
                latest_useful_arrival_minutes=90)
    transport = _RegistryBackedSimulatedTransport(registry)
    ledger = Ledger("/tmp/mobilize_d4_sample_shift_ledger.jsonl")
    result = await mobilize(need, candidates, transport, ledger=ledger, mobilization_id="d4_sample_shift")
    _report("SHIFT (shortage case)", registry, candidates, result)


def _report(label: str, registry, candidates, result) -> None:
    eligible = [c for c in candidates if c.eligible]
    ineligible = [c for c in candidates if not c.eligible]
    print(f"\n=== {label} ===")
    print(f"registry size: {len(registry)} | eligible: {len(eligible)} | ineligible: {len(ineligible)}")
    for c in ineligible:
        print(f"  ineligible: {c.name} -- {c.ineligibility_reason}")
    print(f"filled: {result.filled} | confirmed: {len(result.confirmed)} | calls_used: {result.calls_used}")
    print(f"stop_reason: {result.stop_reason}")


async def main() -> None:
    await run_donor_scenario()
    await run_shift_scenario()


if __name__ == "__main__":
    asyncio.run(main())
