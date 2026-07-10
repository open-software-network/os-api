from __future__ import annotations

import asyncio
import math
import os
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class AccountUser:
    id: str
    handle: str
    email: str | None
    display_name: str | None
    avatar_url: str | None


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str


@dataclass(frozen=True)
class WalletGrant:
    token: str
    cap_credits: int
    grant_id: str


@dataclass(frozen=True)
class WalletDecision:
    grant: WalletGrant | None
    reason: str | None = None
    available_credits: int | None = None


class AccountsClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http
        self.site_url = os.getenv("OS_ACCOUNTS_URL", "https://accounts.opensoftware.co").rstrip("/")
        self.api_url = os.getenv(
            "OS_ACCOUNTS_API_URL", "https://accounts-api.opensoftware.co"
        ).rstrip("/")
        self.client_id = os.getenv("OS_ACCOUNTS_CLIENT_ID", "")
        self.app_api_key = os.getenv("OS_ACCOUNTS_APP_API_KEY", "")

    @property
    def login_enabled(self) -> bool:
        return bool(self.client_id)

    @property
    def billing_enabled(self) -> bool:
        return bool(self.app_api_key)

    async def exchange_code(self, code: str, verifier: str, redirect_uri: str) -> TokenPair | None:
        envelope = await self._json(
            "/auth/token",
            method="POST",
            body={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
            },
        )
        data = envelope.get("data") if envelope.get("success") else None
        return _token_pair(data)

    async def refresh(self, refresh_token: str) -> TokenPair | None:
        envelope = await self._json(
            "/auth/refresh", method="POST", body={"refresh_token": refresh_token}
        )
        data = envelope.get("data") if envelope.get("success") else None
        return _token_pair(data)

    async def logout(self, refresh_token: str) -> None:
        await self._json("/auth/logout", method="POST", body={"refresh_token": refresh_token})

    async def me(self, access_token: str) -> tuple[AccountUser | None, int | None]:
        envelope = await self._json("/me", authorization=f"Bearer {access_token}")
        if not envelope.get("success") or not isinstance(envelope.get("data"), dict):
            return None, _optional_int(envelope.get("error_code"))
        data = envelope["data"]
        return (
            AccountUser(
                id=str(data["id"]),
                handle=str(data.get("handle") or data["id"]),
                email=_optional_str(data.get("email")),
                display_name=_optional_str(data.get("display_name")),
                avatar_url=_optional_str(data.get("avatar_url")),
            ),
            None,
        )

    async def balance(self, access_token: str) -> dict[str, Any] | None:
        envelope = await self._json("/billing/balance", authorization=f"Bearer {access_token}")
        return envelope.get("data") if envelope.get("success") else None

    async def authorize_wallet(
        self,
        user_id: str,
        estimate_microusd: int,
        request_id: str,
    ) -> WalletDecision:
        if not self.billing_enabled:
            return WalletDecision(grant=None)
        estimate_credits = max(1, math.ceil(max(0, estimate_microusd) / 1_000))
        envelope = await self._json(
            "/authorize",
            method="POST",
            authorization=f"Bearer {self.app_api_key}",
            request_id=request_id,
            body={
                "user_id": user_id,
                "action": "inference",
                "estimate_credits": estimate_credits,
                "hold_ttl_seconds": 600,
            },
        )
        data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
        if not envelope.get("success") or not data.get("allowed") or not data.get("token"):
            return WalletDecision(
                grant=None,
                reason=str(data.get("reason") or envelope.get("message") or "billing_denied"),
                available_credits=_optional_int(data.get("available_credits")),
            )
        return WalletDecision(
            grant=WalletGrant(
                token=str(data["token"]),
                cap_credits=int(data.get("cap_credits", estimate_credits)),
                grant_id=str(data.get("grant_id", "")),
            )
        )

    async def charge_wallet(
        self,
        grant: WalletGrant | None,
        actual_microusd: int,
        billing_id: str,
        request_id: str,
    ) -> bool:
        if not grant or not self.billing_enabled:
            return True
        credits = min(grant.cap_credits, max(1, math.ceil(max(0, actual_microusd) / 1_000)))
        body = {
            "token": grant.token,
            "credits": credits,
            "idempotency_key": f"inference:{billing_id}",
        }
        for attempt in range(3):
            envelope = await self._json(
                "/charge",
                method="POST",
                authorization=f"Bearer {self.app_api_key}",
                request_id=request_id,
                body=body,
            )
            if envelope.get("success"):
                return True
            if attempt < 2:
                await asyncio.sleep(0.1 * (2**attempt))
        return False

    async def _json(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        authorization: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        headers = {"accept": "application/json"}
        if authorization:
            headers["authorization"] = authorization
        if request_id:
            headers["x-request-id"] = request_id
        try:
            response = await self.http.request(
                method,
                f"{self.api_url}{path}",
                json=body,
                headers=headers,
                timeout=15,
            )
            parsed = response.json()
            return parsed if isinstance(parsed, dict) else {}
        except (httpx.HTTPError, ValueError):
            return {"success": False, "data": None, "message": "accounts_unavailable"}


def _token_pair(data: Any) -> TokenPair | None:
    if not isinstance(data, dict) or not data.get("access_token") or not data.get("refresh_token"):
        return None
    return TokenPair(
        access_token=str(data["access_token"]),
        refresh_token=str(data["refresh_token"]),
    )


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
