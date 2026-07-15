from __future__ import annotations

import base64
import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

FAKE_QUOTE = base64.b64encode(b"\x04\x00\x02\x00" + b"q" * 100).decode()


class TrackingAsyncByteStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.closed = False

    async def __aiter__(self):
        yield self.content

    async def aclose(self) -> None:
        self.closed = True


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


def test_attestation_caps_chunked_request_before_contacting_agent(
    attestation_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_GATEWAY_ATTESTATION_REQUEST_BYTES", "32")
    body = json.dumps({"nonce": "a" * 128}).encode()

    response = attestation_client.post(
        "/v1/attestation",
        headers={"content-type": "application/json"},
        content=(chunk for chunk in (body[:16], body[16:])),
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


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


def test_attestation_caps_chunked_quote_response_and_closes_stream(
    attestation_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_UPSTREAM_JSON_RESPONSE_BYTES", "32")
    stream = TrackingAsyncByteStream(
        json.dumps({"quote": "q" * 128, "provider": "tdx_guest"}).encode()
    )

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    attestation_client.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))

    response = attestation_client.post(
        "/v1/attestation",
        json={"nonce": "client-nonce-0001"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "attestation_unavailable"
    assert stream.closed is True


@pytest.mark.parametrize("body", [b"not json", b"[]"])
def test_attestation_fails_closed_on_invalid_quote_response(
    attestation_client: TestClient,
    body: bytes,
) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    attestation_client.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))

    response = attestation_client.post(
        "/v1/attestation",
        json={"nonce": "client-nonce-0001"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "attestation_unavailable"
