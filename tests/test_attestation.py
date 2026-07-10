from __future__ import annotations

import base64
import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

FAKE_QUOTE = base64.b64encode(b"\x04\x00\x02\x00" + b"q" * 100).decode()


@pytest.fixture
def attestation_client(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ATTESTATION_AGENT_URL", "http://quote-agent.internal:8100")

    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "quote-agent.internal"
        assert request.url.path == "/quote"
        body = json.loads(request.content)
        assert len(bytes.fromhex(body["report_data"])) == 64
        return httpx.Response(
            200,
            json={"quote": FAKE_QUOTE, "provider": "tdx_guest", "generation": "1"},
        )

    client.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    return client


def test_attestation_requires_authentication(attestation_client: TestClient) -> None:
    response = attestation_client.post(
        "/v1/attestation",
        headers={"Authorization": ""},
        json={"nonce": "abcdefgh"},
    )

    assert response.status_code == 401


def test_attestation_rejects_weak_nonce(attestation_client: TestClient) -> None:
    response = attestation_client.post("/v1/attestation", json={"nonce": "short"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_nonce"


def test_attestation_binds_nonce_and_backends(attestation_client: TestClient) -> None:
    response = attestation_client.post(
        "/v1/attestation",
        json={"nonce": "client-nonce-0001"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["format"] == "tdx.quote.v4"
    assert body["quote"] == FAKE_QUOTE
    assert body["binding"]["nonce"] == "client-nonce-0001"
    backends = body["binding"]["confidential_backends"]
    assert backends == [{"backend": "tee", "revision": "vllm-openai@testdigest0001"}]
    expected = hashlib.sha512(body["binding_canonical"].encode()).hexdigest()
    assert body["report_data_sha512"] == expected
    assert json.loads(body["binding_canonical"]) == body["binding"]


def test_attestation_fails_closed_without_agent(client: TestClient) -> None:
    response = client.post("/v1/attestation", json={"nonce": "client-nonce-0001"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "attestation_unavailable"


def test_attestation_fails_closed_when_agent_errors(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATTESTATION_AGENT_URL", "http://quote-agent.internal:8100")

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "quote_generation_failed"})

    client.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))

    response = client.post("/v1/attestation", json={"nonce": "client-nonce-0001"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "attestation_unavailable"