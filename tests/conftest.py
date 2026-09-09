from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from sovereign_gateway.app import create_app

API_KEY = "sk-test-secret"


class AsyncBytesStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def __aiter__(self):
        yield self.content


@pytest.fixture
def upstream_requests() -> list[httpx.Request]:
    return []


@pytest.fixture
def frozen_limiter_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pin the fixed-window limiter's clock so count-then-429 tests can't flake
    # when a wall-clock minute rolls between the last allowed request and the
    # one expected to be blocked. Only the rate_limit module's `time` reference
    # is replaced, so retry-after math elsewhere still reads the real clock.
    import types

    monkeypatch.setattr(
        "sovereign_gateway.rate_limit.time",
        types.SimpleNamespace(time=lambda: 1_000_000.0),
    )


@pytest.fixture
def client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    upstream_requests: list[httpx.Request],
) -> Iterator[TestClient]:
    config = tmp_path / "models.yaml"
    config.write_text(
        """
backends:
  standard:
    base_url: http://standard.internal
    privacy: standard
  tee:
    base_url: http://tee.internal
    api_key_env: TEE_UPSTREAM_KEY
    privacy: confidential
    revision: vllm-openai@testdigest0001
    max_concurrency: 4
models:
  dual-model:
    upstream_model: org/real-model
    standard_backend: standard
    confidential_backend: tee
  standard-only:
    upstream_model: org/standard-model
    standard_backend: standard
"""
    )
    digest = hashlib.sha256(API_KEY.encode()).hexdigest()
    monkeypatch.setenv("MODEL_CONFIG_PATH", str(config))
    monkeypatch.setenv("API_KEY_HASHES", f"test-tenant={digest}")
    monkeypatch.setenv("TEE_UPSTREAM_KEY", "internal-secret")
    monkeypatch.setenv("REQUESTS_PER_MINUTE", "20")
    monkeypatch.setenv("SERVICE_REQUESTS_PER_MINUTE", "20")
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("SITE_BASIC_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("SITE_BASIC_AUTH_PASSWORD", raising=False)

    def upstream(request: httpx.Request) -> httpx.Response:
        upstream_requests.append(request)
        payload: dict[str, Any] = json.loads(request.content)
        if payload.get("stream"):
            content = b'data: {"id":"chunk-1"}\n\ndata: [DONE]\n\n'
            headers = {"content-type": "text/event-stream"}
            if payload.get("metadata", {}).get("gzip_test"):
                content = gzip.compress(content)
                headers["content-encoding"] = "gzip"
            return httpx.Response(
                200,
                headers=headers,
                stream=AsyncBytesStream(content),
            )
        return httpx.Response(
            200,
            json={
                "id": "response-1",
                "object": "chat.completion",
                "model": payload["model"],
                "choices": [{"message": {"role": "assistant", "content": "hello"}}],
            },
        )

    app = create_app()
    with TestClient(app) as test_client:
        old_client = app.state.http
        app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        test_client.headers["Authorization"] = f"Bearer {API_KEY}"
        yield test_client
        test_client.headers.clear()
        # TestClient exits the lifespan after this fixture and closes the replacement client.
        # Close the client originally created by the lifespan here to avoid leaking its pool.
        import anyio

        anyio.run(old_client.aclose)
