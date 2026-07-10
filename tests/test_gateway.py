from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient


def test_authentication_is_required(client: TestClient) -> None:
    response = client.get("/v1/models", headers={"Authorization": ""})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


def test_public_home_and_docs_render(client: TestClient) -> None:
    home = client.get("/")
    docs = client.get("/docs")

    assert home.status_code == 200
    assert "Open models" in home.text
    assert docs.status_code == 200
    assert "Developer docs" in docs.text


def test_lists_models_and_privacy_modes(client: TestClient) -> None:
    response = client.get("/v1/models")

    assert response.status_code == 200
    by_id = {item["id"]: item for item in response.json()["data"]}
    assert by_id["dual-model"]["privacy_modes"] == ["standard", "confidential"]
    assert by_id["standard-only"]["privacy_modes"] == ["standard"]


def test_required_routes_to_confidential_backend(
    client: TestClient, upstream_requests: list[httpx.Request]
) -> None:
    response = client.post(
        "/v1/chat/completions",
        headers={"X-Confidential-Compute": "required"},
        json={"model": "dual-model", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    assert response.headers["x-compute-privacy"] == "confidential"
    assert upstream_requests[-1].url.host == "tee.internal"
    assert upstream_requests[-1].headers["authorization"] == "Bearer internal-secret"
    assert json.loads(upstream_requests[-1].content)["model"] == "org/real-model"


def test_required_never_downgrades(
    client: TestClient, upstream_requests: list[httpx.Request]
) -> None:
    response = client.post(
        "/v1/chat/completions",
        headers={"X-Confidential-Compute": "required"},
        json={"model": "standard-only", "messages": []},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "confidential_compute_unavailable"
    assert not upstream_requests


def test_preferred_reports_standard_fallback(
    client: TestClient, upstream_requests: list[httpx.Request]
) -> None:
    response = client.post(
        "/v1/chat/completions",
        json={"model": "standard-only", "messages": []},
    )

    assert response.status_code == 200
    assert response.headers["x-compute-privacy"] == "standard-fallback"
    assert upstream_requests[-1].url.host == "standard.internal"


def test_streaming_is_passed_through(client: TestClient) -> None:
    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"X-Confidential-Compute": "disabled"},
        json={"model": "dual-model", "stream": True, "messages": []},
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert response.headers["x-compute-privacy"] == "standard"
    assert 'data: {"id":"chunk-1"}' in body
    assert "data: [DONE]" in body


def test_unknown_model_uses_openai_error_shape(client: TestClient) -> None:
    response = client.post(
        "/v1/embeddings",
        json={"model": "missing", "input": "hello"},
    )

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "message": "Model 'missing' does not exist",
            "type": "invalid_request_error",
            "param": None,
            "code": "model_not_found",
        }
    }


def test_request_id_is_preserved(client: TestClient) -> None:
    response = client.get("/healthz", headers={"X-Request-ID": "request-from-client"})

    assert response.headers["x-request-id"] == "request-from-client"


def test_rejects_oversized_body_before_proxying(
    client: TestClient, upstream_requests: list[httpx.Request], monkeypatch
) -> None:
    monkeypatch.setenv("MAX_REQUEST_BYTES", "10")

    response = client.post(
        "/v1/chat/completions",
        content=b'{"model":"dual-model"}',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert not upstream_requests
