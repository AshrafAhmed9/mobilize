"""A3: provider errors from CALL-E's REST API must get distinct, explicit
dispositions, not all be treated the same way.

Before this fix, `CalleTransport.dispatch()` called `response.raise_for_status()`
directly with no error-envelope parsing (flagged as a real, unaddressed gap in
`mobilize/artifacts/calle_contract_c0.md`'s "Error codes" section). Every HTTP
failure -- a 401 auth error, a 429 rate limit, a 400 unsupported_region, or a
genuine 503 outage -- surfaced identically as `httpx.HTTPStatusError`, which
`dispatcher._dispatch_one` only ever catches via its generic `except Exception`
branch. That branch means "CALL-E might have accepted this call" -- correct for
a real timeout or a 5xx, but wrong for a 401/429/400, which are explicit
provider-side rejections that were DEFINITELY never queued. Treating those as
ambiguous over-counts calls_used and blocks every later wave (via
`ambiguous_candidate_ids`) for a call that provably never happened.
"""

from __future__ import annotations

import httpx
import pytest

from mobilize.core.dispatcher import mobilize
from mobilize.core.ledger import Ledger
from mobilize.core.types import Need
from mobilize.tests.test_planner import make_candidate
from mobilize.transports.base import CalleAmbiguousError, CalleSafeRejectionError
from mobilize.transports.calle import CalleTransport


def _make_transport(status_code: int, error_body: dict | None) -> CalleTransport:
    transport = CalleTransport(api_key="test-key", base_url="https://api.heycall-e.com")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=error_body or {})

    # Swap in a mock transport under the same trusted base_url so
    # validate_trusted_base_url (checked at construction) isn't bypassed.
    transport._client = httpx.AsyncClient(
        base_url="https://api.heycall-e.com",
        transport=httpx.MockTransport(handler),
    )
    return transport


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status_code,error_body",
    [
        (401, {"error": {"code": "unauthorized", "message": "bad key"}}),
        (403, {"error": {"code": "forbidden", "message": "no access"}}),
        (429, {"error": {"code": "rate_limit_exceeded", "message": "slow down"}}),
        (400, {"error": {"code": "unsupported_region", "message": "no route"}}),
    ],
)
async def test_provider_rejection_raises_safe_not_ambiguous(status_code, error_body):
    transport = _make_transport(status_code, error_body)
    candidate = make_candidate("c1")
    with pytest.raises(CalleSafeRejectionError):
        await transport.dispatch(candidate, "need", "loc", idempotency_key="k1")


@pytest.mark.asyncio
async def test_outage_raises_ambiguous_not_safe():
    transport = _make_transport(503, {"error": {"code": "internal_error", "message": "down"}})
    candidate = make_candidate("c1")
    with pytest.raises(CalleAmbiguousError):
        await transport.dispatch(candidate, "need", "loc", idempotency_key="k1")


@pytest.mark.asyncio
async def test_rate_limited_dispatch_is_not_counted_as_ambiguous_by_the_dispatcher(tmp_path):
    """End-to-end through mobilize(): a rate-limited candidate must not land
    in ambiguous_candidate_ids or count toward calls_used, and must not block
    a later wave the way a real ambiguous failure does."""

    class RateLimitedThenOkTransport:
        def __init__(self) -> None:
            self.attempted: list[str] = []

        async def dispatch(self, candidate, need_label, location, *, idempotency_key):
            self.attempted.append(candidate.id)
            if candidate.id == "c_limited":
                raise CalleSafeRejectionError("CALL-E rejected the request (429, rate_limit_exceeded): slow down")
            return f"call_{candidate.id}"

        async def poll(self, call_id, *, expected_candidate=None):
            return None

    ledger = Ledger(tmp_path / "ledger.jsonl")
    transport = RateLimitedThenOkTransport()
    pool = [make_candidate("c_limited", accept=0.9, showup=0.9), make_candidate("c_ok", accept=0.9, showup=0.9)]
    need = Need(label="x", count=5, deadline_minutes=60, location="loc", max_calls=5)

    result = await mobilize(
        need, pool, transport, ledger=ledger, mobilization_id="mob_ratelimit",
        poll_timeout_s=0.5,
    )

    assert "c_limited" not in result.ambiguous_candidate_ids
    # calls_used only reflects the one real, accepted dispatch (c_ok) --
    # the safely-rejected c_limited attempt is a non-event, exactly like a
    # ValueError pre-flight failure.
    assert result.calls_used == 1
