from __future__ import annotations

import httpx
import pytest

from sovereign_gateway.accounts import AccountsClient, WalletGrant


@pytest.mark.asyncio
async def test_wallet_charge_retries_with_stable_idempotency_key(monkeypatch) -> None:
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) < 3:
            return httpx.Response(503, json={"success": False})
        return httpx.Response(200, json={"success": True})

    monkeypatch.setenv("OS_ACCOUNTS_APP_API_KEY", "app-secret")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = AccountsClient(http)
        charged = await client.charge_wallet(
            WalletGrant(token="grant-token", cap_credits=10, grant_id="grant-1"),
            actual_microusd=3_000,
            billing_id="billing-1",
            request_id="request-1",
        )

    assert charged is True
    assert len(attempts) == 3
    bodies = [request.read().decode() for request in attempts]
    assert all('"idempotency_key":"inference:billing-1"' in body for body in bodies)
