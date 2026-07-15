from __future__ import annotations

import base64
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from sovereign_gateway import app as app_module
from sovereign_gateway.app import create_app


def test_authentication_is_required(client: TestClient) -> None:
    response = client.get("/v1/models", headers={"Authorization": ""})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


def test_inference_rejects_malformed_utf8_json(client: TestClient) -> None:
    response = client.post(
        "/v1/chat/completions",
        content=b'{"model":"dual-model","messages":["\xff"]}',
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "Request body must include a valid model"


def test_public_home_and_docs_render(client: TestClient) -> None:
    home = client.get("/")
    docs = client.get("/docs")

    assert home.status_code == 200
    assert "Open models" in home.text
    assert docs.status_code == 200
    assert "Developer docs" in docs.text


def test_responses_identify_gateway_region(client: TestClient) -> None:
    response = client.get("/v1/models")

    assert response.headers["x-gateway-region"] == "local"


def test_site_basic_auth_does_not_replace_api_key_auth(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("SITE_BASIC_AUTH_USERNAME", "operator")
    monkeypatch.setenv("SITE_BASIC_AUTH_PASSWORD", "test-password")

    missing = client.get("/", headers={"Authorization": ""})
    wrong = client.get(
        "/docs",
        headers={"Authorization": _basic_auth("operator", "wrong")},
    )
    allowed = client.get(
        "/dashboard",
        headers={"Authorization": _basic_auth("operator", "test-password")},
        follow_redirects=False,
    )
    api = client.get("/v1/models")
    readiness = client.get("/readyz", headers={"Authorization": ""})

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == (
        'Basic realm="OpenSoftware API", charset="UTF-8"'
    )
    assert missing.headers["cache-control"] == "no-store"
    assert wrong.status_code == 401
    assert allowed.status_code == 303
    assert allowed.headers["location"] == "/auth/start"
    assert api.status_code == 200
    assert readiness.status_code == 200


def _basic_auth(username: str, password: str) -> str:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {encoded}"


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


def test_responses_identify_model_revision_when_pinned(
    client: TestClient, upstream_requests: list[httpx.Request]
) -> None:
    pinned = client.post(
        "/v1/chat/completions",
        headers={"X-Confidential-Compute": "required"},
        json={"model": "dual-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    unpinned = client.post(
        "/v1/chat/completions",
        headers={"X-Confidential-Compute": "disabled"},
        json={"model": "standard-only", "messages": []},
    )

    assert pinned.headers["x-model-backend"] == "tee"
    assert pinned.headers["x-model-revision"] == "vllm-openai@testdigest0001"
    assert unpinned.status_code == 200
    assert "x-model-revision" not in unpinned.headers


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


def test_default_zero_retention_does_not_use_standard_fallback(
    client: TestClient, upstream_requests: list[httpx.Request]
) -> None:
    response = client.post(
        "/v1/chat/completions",
        json={"model": "standard-only", "messages": []},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "privacy_requirements_unavailable"
    assert not upstream_requests


def test_explicit_downgrade_reports_standard_fallback(
    client: TestClient, upstream_requests: list[httpx.Request]
) -> None:
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "standard-only",
            "messages": [],
            "privacy": {"allow_downgrade": True},
        },
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
    assert 'data: {"id":"chunk-1","model":"dual-model"}' in body
    assert "data: [DONE]" in body


def test_gzip_compressed_upstream_stream_is_decoded(client: TestClient) -> None:
    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"X-Confidential-Compute": "disabled"},
        json={
            "model": "dual-model",
            "stream": True,
            "messages": [],
            "metadata": {"gzip_test": True},
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert 'data: {"id":"chunk-1","model":"dual-model"}' in body
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


def test_embeddings_do_not_receive_generation_output_limits(
    client: TestClient,
    upstream_requests: list[httpx.Request],
) -> None:
    response = client.post(
        "/v1/embeddings",
        headers={"X-Confidential-Compute": "disabled"},
        json={"model": "dual-model", "input": "hello"},
    )

    assert response.status_code == 200
    forwarded = json.loads(upstream_requests[-1].content)
    assert "max_completion_tokens" not in forwarded
    assert "max_tokens" not in forwarded


def test_venice_compatibility_surface_is_dormant_by_default(client: TestClient) -> None:
    response = client.post(
        "/v1/audio/transcriptions",
        data={"model": "nvidia/parakeet-tdt-0.6b-v3"},
        files={"file": ("audio.wav", b"RIFF-audio", "audio/wav")},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"

    image = client.post(
        "/v1/image/generate",
        json={"model": "chroma", "prompt": "a red bicycle"},
    )

    assert image.status_code == 404
    assert image.json()["error"]["code"] == "not_found"

    for operation in ("quote", "queue", "retrieve"):
        video = client.post(f"/v1/video/{operation}", json={})
        assert video.status_code == 404
        assert video.json()["error"]["code"] == "not_found"


def test_request_id_is_preserved(client: TestClient) -> None:
    response = client.get("/healthz", headers={"X-Request-ID": "request-from-client"})

    assert response.headers["x-request-id"] == "request-from-client"


def test_invalid_request_id_is_replaced(client: TestClient) -> None:
    response = client.get("/healthz", headers={"X-Request-ID": "not valid with spaces"})

    assert response.headers["x-request-id"] != "not valid with spaces"
    assert len(response.headers["x-request-id"]) == 36


def test_success_responses_include_rate_limit_metadata(client: TestClient) -> None:
    response = client.get("/v1/models")

    assert response.headers["x-ratelimit-limit-requests"] == "20"
    assert response.headers["x-ratelimit-remaining-requests"] == "19"
    assert int(response.headers["x-ratelimit-reset-requests"]) > int(time.time())


def test_rate_limit_returns_retry_metadata(client: TestClient) -> None:
    for _ in range(20):
        response = client.get("/v1/models")
        assert response.status_code == 200

    response = client.get("/v1/models")

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limit_exceeded"
    assert response.headers["x-ratelimit-remaining-requests"] == "0"
    assert 1 <= int(response.headers["retry-after"]) <= 60


def test_readiness_reports_dependency_state(client: TestClient) -> None:
    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "components": {}}


def test_readiness_stays_ready_when_redis_degraded(client: TestClient) -> None:
    class DownRedis:
        async def ping(self) -> None:
            raise ConnectionError("redis down")

    client.app.state.redis = DownRedis()

    response = client.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["components"]["redis"] == "degraded"


def test_readiness_fails_when_firestore_down(client: TestClient) -> None:
    class DownKeys:
        async def healthcheck(self) -> None:
            raise ConnectionError("firestore down")

    client.app.state.managed_keys = DownKeys()

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["components"]["firestore"] == "unavailable"


def test_unhandled_api_errors_use_safe_openai_shape(client: TestClient) -> None:
    async def broken_send(*args, **kwargs):
        raise RuntimeError("sensitive internal detail")

    client.app.state.http.send = broken_send
    response = client.post(
        "/v1/chat/completions",
        json={"model": "dual-model", "messages": []},
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "sensitive" not in response.text
    assert response.headers["x-request-id"]


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


def test_routed_inference_uses_limited_reader_for_chunked_body(
    client: TestClient,
    upstream_requests: list[httpx.Request],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_REQUEST_BYTES", "32")
    original = app_module._read_request_body_limited
    used_limited_reader = False

    async def tracking_reader(request, maximum):
        nonlocal used_limited_reader
        used_limited_reader = True
        return await original(request, maximum)

    monkeypatch.setattr(app_module, "_read_request_body_limited", tracking_reader)
    body = b'{"model":"dual-model","padding":"' + b"x" * 64 + b'"}'

    response = client.post(
        "/v1/chat/completions",
        headers={"Content-Type": "application/json"},
        content=(chunk for chunk in (body[:16], body[16:])),
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert used_limited_reader is True
    assert not upstream_requests


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MAX_IMAGE_GENERATE_REQUEST_BYTES", "not-an-integer"),
        ("IMAGE_GENERATE_TIMEOUT_SECONDS", "nan"),
        ("VENICE_COMPAT_ENABLED", "sometimes"),
        ("ENABLE_MANAGED_KEYS", "sometimes"),
        ("REQUIRE_CONFIDENTIAL_SPACE", "sometimes"),
    ],
)
def test_invalid_environment_fails_application_startup(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv("API_KEY_HASHES", f"test={'0' * 64}")
    monkeypatch.setenv("LIVE_PROVIDER_CATALOG_ENABLED", "false")
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        with TestClient(create_app()):
            pass
