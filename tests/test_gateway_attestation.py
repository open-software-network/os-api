from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from sovereign_gateway import app as app_module
from sovereign_gateway.gateway_attestation import (
    DEFAULT_AUDIENCE,
    GatewayAttestation,
    GatewayAttestationUnavailable,
    request_gateway_attestation,
)


@pytest.mark.asyncio
async def test_requests_nonce_bound_confidential_space_oidc_token() -> None:
    def launcher(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/token"
        assert json.loads(request.content) == {
            "audience": DEFAULT_AUDIENCE,
            "token_type": "OIDC",
            "nonces": ["client-nonce-0001"],
        }
        return httpx.Response(200, json={"token": "signed-attestation-token"})

    evidence = await request_gateway_attestation(
        "client-nonce-0001", transport=httpx.MockTransport(launcher)
    )

    assert evidence.public_dict() == {
        "object": "gateway.attestation",
        "format": "confidential-space.oidc",
        "token": "signed-attestation-token",
        "audience": DEFAULT_AUDIENCE,
        "nonce": "client-nonce-0001",
    }


@pytest.mark.asyncio
async def test_missing_launcher_token_fails_closed() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))

    with pytest.raises(GatewayAttestationUnavailable):
        await request_gateway_attestation("client-nonce-0001", transport=transport)


def test_gateway_attestation_is_public_and_fails_closed_without_a_tee(
    client: TestClient,
) -> None:
    response = client.post(
        "/v1/gateway/attestation",
        headers={"Authorization": ""},
        json={"nonce": "client-nonce-0001"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "gateway_attestation_unavailable"


def test_gateway_attestation_rejects_weak_nonce(client: TestClient) -> None:
    response = client.post("/v1/gateway/attestation", json={"nonce": "short"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_nonce"


@pytest.mark.parametrize("length", [9, 89])
def test_gateway_attestation_rejects_nonce_outside_launcher_bounds(
    client: TestClient, length: int
) -> None:
    response = client.post("/v1/gateway/attestation", json={"nonce": "a" * length})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_nonce"


@pytest.mark.parametrize("length", [10, 88])
def test_gateway_attestation_accepts_nonce_at_launcher_bounds(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, length: int
) -> None:
    async def attest(nonce: str) -> GatewayAttestation:
        return GatewayAttestation("signed-token", DEFAULT_AUDIENCE, nonce)

    monkeypatch.setattr(app_module, "request_gateway_attestation", attest)
    response = client.post("/v1/gateway/attestation", json={"nonce": "a" * length})

    assert response.status_code == 200
    assert response.json()["nonce"] == "a" * length
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["access-control-allow-origin"] == "*"


def test_gateway_attestation_fails_closed_outside_confidential_space(client: TestClient) -> None:
    response = client.post(
        "/v1/gateway/attestation", json={"nonce": "client-nonce-0001"}
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "gateway_attestation_unavailable"
