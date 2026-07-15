from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient

from sovereign_gateway.app import (
    DEFAULT_NON_TEXT_MAX_COMPLETION_TOKENS,
    BillingContext,
    _actual_cost,
    _auto_catalog_model,
    _endpoint_headers,
    _enforce_non_text_output_cap,
    _estimate_fallback_charge,
    _estimate_reservation_cost,
    _metered_stream,
    _prepare_billing,
    _settle_billing,
    _stream_video_response,
    _validate_client_encryption_key,
    create_app,
)
from sovereign_gateway.auth import Principal
from sovereign_gateway.config import GatewayConfig
from sovereign_gateway.routing import (
    NON_TEXT_INPUT_HEURISTIC_ALLOWANCE,
    EndpointHealth,
    RoutingError,
    estimate_bounded_prompt_units,
    parse_routing_policy,
    route_endpoints,
    select_auto_models,
)


class DelayedAsyncByteStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes, delay: float) -> None:
        self.content = content
        self.delay = delay

    async def __aiter__(self):
        await asyncio.sleep(self.delay)
        yield self.content


class TrackingAsyncByteStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.closed = False

    async def __aiter__(self):
        yield self.content

    async def aclose(self) -> None:
        self.closed = True


API_KEY = "sk-routing-test"

CONFIG = """
providers:
  phala:
    name: Phala
    base_url: https://phala.test
    api_key_env: PHALA_API_KEY
    adapter: phala
  venice:
    name: Venice
    base_url: https://venice.test/api
    api_key_env: VENICE_API_KEY
    adapter: venice
models:
  openai/gpt-oss-20b:
    max_context_length: 131072
    quality_score: 90
    capabilities: [tools, tool_choice, structured_outputs, reasoning]
  backup/private-model:
    max_context_length: 32768
    quality_score: 40
endpoints:
  phala-gpt-oss-20b:
    model: openai/gpt-oss-20b
    provider: phala
    upstream_model: phala/gpt-oss-20b
    priority: 10
    capabilities: [tools, tool_choice, structured_outputs, reasoning]
    privacy:
      level: tee
      execution: gpu-tee
      retention: zero
      attestation: phala-aci-receipt
    pricing:
      input_per_million_usd: 0.04
      output_per_million_usd: 0.15
  venice-gpt-oss-20b:
    model: openai/gpt-oss-20b
    provider: venice
    upstream_model: tee-gpt-oss-20b
    priority: 20
    capabilities: [tools, tool_choice, structured_outputs, reasoning]
    privacy:
      level: tee
      execution: gpu-tee
      retention: zero
      attestation: venice-tee-signature
    pricing:
      input_per_million_usd: 0.05
      output_per_million_usd: 0.19
  phala-backup:
    model: backup/private-model
    provider: phala
    upstream_model: backup/private-model
    priority: 10
    privacy:
      level: tee
      execution: gpu-tee
      retention: zero
      attestation: phala-aci-receipt
    pricing:
      input_per_million_usd: 0.01
      output_per_million_usd: 0.02
"""


@pytest.fixture
def routed_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    config = tmp_path / "providers.yaml"
    config.write_text(CONFIG)
    monkeypatch.setenv("MODEL_CONFIG_PATH", str(config))
    monkeypatch.setenv("API_KEY_HASHES", f"router={hashlib.sha256(API_KEY.encode()).hexdigest()}")
    monkeypatch.setenv("PHALA_API_KEY", "phala-secret")
    monkeypatch.setenv("VENICE_API_KEY", "venice-secret")
    monkeypatch.setenv("VENICE_COMPAT_ENABLED", "true")
    monkeypatch.setenv("LIVE_PROVIDER_CATALOG_ENABLED", "false")
    monkeypatch.delenv("REDIS_URL", raising=False)
    calls: list[str] = []
    payloads: list[dict[str, object]] = []
    response_streams: list[TrackingAsyncByteStream] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.method == "GET" and request.url.path.endswith("/v1/models"):
            if request.url.host == "venice.test":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": "zai-org-glm-5-2",
                                "type": "text",
                                "model_spec": {
                                    "name": "GLM 5.2",
                                    "privacy": "private",
                                    "pricing": {
                                        "input": {"usd": 1.4},
                                        "output": {"usd": 4.4},
                                    },
                                    "availableContextTokens": 100000,
                                    "capabilities": {},
                                },
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "phala/live-only",
                            "name": "Phala Live Only",
                            "is_tee": True,
                            "pricing": {"prompt": "0.0000001", "completion": "0.0000002"},
                        }
                    ]
                },
            )
        if request.url.path.endswith("/audio/transcriptions"):
            assert request.headers["authorization"] == "Bearer venice-secret"
            assert b'name="model"' in request.content
            assert b"nvidia/parakeet-tdt-0.6b-v3" in request.content
            assert b'filename="audio.wav"' in request.content
            assert b"private-name.wav" not in request.content
            if b"oversized-response" in request.content:
                stream = TrackingAsyncByteStream(json.dumps({"text": "x" * 128}).encode())
                response_streams.append(stream)
                return httpx.Response(200, stream=stream)
            if b"\r\n\r\ntext\r\n" in request.content:
                return httpx.Response(
                    200,
                    content=b"hello world\n",
                    headers={"content-type": "text/html"},
                )
            if b"\r\n\r\nsrt\r\n" in request.content:
                return httpx.Response(
                    200,
                    content=b"1\n00:00:00,000 --> 00:00:01,000\nhello\n",
                    headers={"content-type": "text/html"},
                )
            if b"\r\n\r\nvtt\r\n" in request.content:
                return httpx.Response(
                    200,
                    content=b"WEBVTT\n\n00:00.000 --> 00:01.000\nhello\n",
                    headers={"content-type": "text/html"},
                )
            return httpx.Response(200, json={"text": "hello", "language": "en"})
        if "/augment/" in request.url.path:
            if b"oversized-response" in request.content:
                stream = TrackingAsyncByteStream(
                    json.dumps({"results": [{"content": "x" * 128}]}).encode()
                )
                response_streams.append(stream)
                return httpx.Response(200, stream=stream)
            if b"invalid-content-type" in request.content:
                return httpx.Response(
                    200, text="<script>alert(1)</script>", headers={"content-type": "text/html"}
                )
            if request.url.path.endswith("/search"):
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {"title": "Result", "url": "https://example.com", "content": "x"}
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={"url": "https://example.com", "content": "page", "format": "markdown"},
            )
        if "/image/" in request.url.path:
            assert request.headers["authorization"] == "Bearer venice-secret"
            payload = json.loads(request.content)
            expected_read_timeout = (
                0.001
                if payload["prompt"] in {"force total timeout", "force body timeout"}
                else 360.0
            )
            assert request.extensions["timeout"] == {
                "connect": 10,
                "read": expected_read_timeout,
                "write": 30,
                "pool": 10,
            }
            assert payload["model"] == "chroma"
            if payload["prompt"] == "force total timeout":
                await asyncio.sleep(0.01)
                raise AssertionError("total timeout did not cancel the upstream request")
            if payload["prompt"] == "force timeout":
                raise httpx.ReadTimeout("image generation timed out", request=request)
            if payload["prompt"] == "force body timeout":
                return httpx.Response(
                    200,
                    stream=DelayedAsyncByteStream(b'{"images":[]}', 0.02),
                )
            if payload["prompt"] == "oversized response":
                stream = TrackingAsyncByteStream(json.dumps({"images": ["x" * 128]}).encode())
                response_streams.append(stream)
                return httpx.Response(200, stream=stream)
            if payload["prompt"] == "reject as text":
                return httpx.Response(400, text="bad model")
            if payload["prompt"] == "fail as text":
                return httpx.Response(503, text="unavailable")
            assert payload["prompt"] == "a red bicycle"
            return httpx.Response(200, json={"images": ["aW1hZ2U="]})
        if "/video/" in request.url.path:
            assert request.headers["authorization"] == "Bearer venice-secret"
            payload = json.loads(request.content)
            expected_read_timeout = 0.03 if payload.get("queue_id") == "slow-json" else 360.0
            assert request.extensions["timeout"] == {
                "connect": 10,
                "read": expected_read_timeout,
                "write": 30,
                "pool": 10,
            }
            if request.url.path.endswith("/quote"):
                if payload["model"] == "oversized-json":
                    return httpx.Response(
                        200,
                        content=b"{}",
                        headers={"content-type": "application/json", "content-length": "101"},
                    )
                return httpx.Response(200, json={"quote": 0.42})
            if request.url.path.endswith("/queue"):
                assert payload["model"] in {
                    "wan-2.2-a14b-text-to-video",
                    "wan-2.6-image-to-video",
                }
                return httpx.Response(
                    200,
                    json={"queue_id": "video-job-1", "download_url": None},
                )
            queue_id = payload["queue_id"]
            if queue_id == "processing":
                return httpx.Response(
                    200,
                    json={
                        "status": "PROCESSING",
                        "average_execution_time": 120000,
                        "execution_duration": 30000,
                    },
                )
            if queue_id == "slow-json":
                await asyncio.sleep(0.02)
                return httpx.Response(
                    200,
                    stream=DelayedAsyncByteStream(b'{"status":"PROCESSING"}', 0.02),
                    headers={"content-type": "application/json"},
                )
            if queue_id == "expired":
                return httpx.Response(404, text="expired")
            if queue_id == "oversized":
                return httpx.Response(
                    200,
                    content=b"video",
                    headers={"content-type": "video/mp4", "content-length": "101"},
                )
            if queue_id == "invalid-content-type":
                return httpx.Response(
                    200,
                    content=b"<script>bad()</script>",
                    headers={"content-type": "text/html"},
                )
            assert queue_id == "completed"
            return httpx.Response(
                200,
                content=b"video-bytes",
                headers={"content-type": "video/mp4"},
            )
        payload = json.loads(request.content)
        payloads.append(payload)
        oversized_status = payload.get("metadata", {}).get("oversized_upstream_status")
        if oversized_status:
            stream = TrackingAsyncByteStream(
                json.dumps(
                    {
                        "error" if oversized_status >= 400 else "id": "oversized",
                        "padding": "x" * 1_024,
                    }
                ).encode()
            )
            response_streams.append(stream)
            return httpx.Response(
                oversized_status,
                headers={
                    "content-type": "application/json",
                    "x-receipt-id": "receipt-oversized",
                    "x-aci-identity": "workload-1",
                    "x-aci-keyset-digest": "digest-1",
                },
                stream=stream,
            )
        forced_status = payload.get("metadata", {}).get("force_primary_status")
        if forced_status and payload["model"] != "backup/private-model":
            return httpx.Response(
                forced_status,
                json={
                    "error": {
                        "message": "request rejected by provider",
                        "type": "invalid_request_error",
                        "code": f"forced_{forced_status}",
                    }
                },
            )
        if payload.get("metadata", {}).get("force_primary_400") and payload["model"] != (
            "backup/private-model"
        ):
            return httpx.Response(400, json={"error": "model-specific schema rejection"})
        if request.url.host == "phala.test" and payload["model"] != "backup/private-model":
            return httpx.Response(503, json={"error": "temporarily unavailable"})
        return httpx.Response(
            200,
            headers={
                "x-venice-tee-signature": "signed-proof",
                "x-receipt-id": "receipt-1",
                "x-aci-identity": "workload-1",
                "x-aci-keyset-digest": "digest-1",
            },
            json={
                "id": "gen-private-1",
                "model": payload["model"],
                "choices": [{"message": {"role": "assistant", "content": "private"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            },
        )

    app = create_app()
    with TestClient(app) as client:
        old_client = app.state.http
        app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        client.headers["Authorization"] = f"Bearer {API_KEY}"
        client.app.state.test_calls = calls
        client.app.state.test_payloads = payloads
        client.app.state.test_response_streams = response_streams
        yield client
        anyio.run(old_client.aclose)


def test_endpoint_config_and_price_sorting(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG)
    config = GatewayConfig.load(path)
    policy = parse_routing_policy(
        {
            "model": "openai/gpt-oss-20b",
            "provider": {"sort": "price"},
            "privacy": {"minimum": "tee", "require_zero_retention": True},
        },
        None,
    )

    endpoints = route_endpoints(config, "openai/gpt-oss-20b", policy, EndpointHealth())

    assert [endpoint.provider for endpoint in endpoints] == ["phala", "venice"]
    assert endpoints[0].input_price_microusd_per_million == 40_000


@pytest.mark.asyncio
async def test_circuit_breaker_excludes_open_endpoint_and_bounds_half_open_probe(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG)
    config = GatewayConfig.load(path)
    policy = parse_routing_policy(
        {"model": "openai/gpt-oss-20b", "privacy": {"minimum": "tee"}},
        None,
    )
    now = [100.0]
    health = EndpointHealth(
        failure_threshold=1,
        cooldown_seconds=10,
        half_open_probe_seconds=2,
        clock=lambda: now[0],
    )

    await health.failure("phala-gpt-oss-20b")
    assert [
        endpoint.id for endpoint in route_endpoints(config, "openai/gpt-oss-20b", policy, health)
    ] == ["venice-gpt-oss-20b"]

    now[0] += 10
    first_probe = route_endpoints(config, "openai/gpt-oss-20b", policy, health)
    concurrent_route = route_endpoints(config, "openai/gpt-oss-20b", policy, health)
    assert [endpoint.id for endpoint in first_probe] == [
        "phala-gpt-oss-20b",
        "venice-gpt-oss-20b",
    ]
    assert [endpoint.id for endpoint in concurrent_route] == ["venice-gpt-oss-20b"]

    now[0] += 2
    retried_probe = route_endpoints(config, "openai/gpt-oss-20b", policy, health)
    assert [endpoint.id for endpoint in retried_probe] == [
        "phala-gpt-oss-20b",
        "venice-gpt-oss-20b",
    ]

    await health.failure("phala-gpt-oss-20b")
    assert [
        endpoint.id for endpoint in route_endpoints(config, "openai/gpt-oss-20b", policy, health)
    ] == ["venice-gpt-oss-20b"]

    now[0] += 10
    recovered_probe = route_endpoints(config, "openai/gpt-oss-20b", policy, health)
    assert recovered_probe[0].id == "phala-gpt-oss-20b"
    await health.success("phala-gpt-oss-20b", 0.1)
    recovered_route = route_endpoints(config, "openai/gpt-oss-20b", policy, health)
    assert recovered_route[0].id == "phala-gpt-oss-20b"


def test_routed_request_skips_endpoint_while_circuit_is_open(
    routed_client: TestClient,
) -> None:
    routed_client.app.state.endpoint_health = EndpointHealth(
        failure_threshold=1,
        cooldown_seconds=60,
    )

    first = routed_client.post(
        "/v1/chat/completions",
        json={
            "model": "openai/gpt-oss-20b",
            "messages": [{"role": "user", "content": "first"}],
        },
    )
    second = routed_client.post(
        "/v1/chat/completions",
        json={
            "model": "openai/gpt-oss-20b",
            "messages": [{"role": "user", "content": "second"}],
        },
    )

    assert first.status_code == 200
    assert second.status_code == 200
    inference_urls = [
        url for url in routed_client.app.state.test_calls if url.endswith("/v1/chat/completions")
    ]
    assert sum("phala.test" in url for url in inference_urls) == 1
    assert sum("venice.test" in url for url in inference_urls) == 2


@pytest.mark.asyncio
async def test_routing_fails_when_every_eligible_circuit_is_open(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG)
    config = GatewayConfig.load(path)
    policy = parse_routing_policy(
        {"model": "openai/gpt-oss-20b", "privacy": {"minimum": "tee"}},
        None,
    )
    health = EndpointHealth(failure_threshold=1, cooldown_seconds=60)
    await health.failure("phala-gpt-oss-20b")
    await health.failure("venice-gpt-oss-20b")

    with pytest.raises(RoutingError) as raised:
        route_endpoints(config, "openai/gpt-oss-20b", policy, health)

    assert raised.value.code == "all_endpoints_unavailable"


def test_auto_cost_slider_selects_cheapest_eligible_model(routed_client: TestClient) -> None:
    response = routed_client.post(
        "/v1/chat/completions",
        json={
            "model": "open-software/auto",
            "auto": {"cost_quality": 0},
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert response.status_code == 200
    assert response.headers["x-os-auto-selected-model"] == "backup/private-model"
    assert response.json()["model"] == "backup/private-model"


def test_auto_quality_slider_selects_highest_quality_model(routed_client: TestClient) -> None:
    response = routed_client.post(
        "/v1/chat/completions",
        json={
            "model": "auto",
            "cost_quality": 1,
            "messages": [],
        },
    )

    assert response.status_code == 200
    assert response.headers["x-os-auto-selected-model"] == "openai/gpt-oss-20b"


def test_auto_defaults_to_highest_quality_model(routed_client: TestClient) -> None:
    response = routed_client.post(
        "/v1/chat/completions",
        json={
            "model": "open-software/auto",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert response.status_code == 200
    assert response.headers["x-os-auto-selected-model"] == "openai/gpt-oss-20b"


def test_auto_filters_models_that_lack_required_tools(routed_client: TestClient) -> None:
    response = routed_client.post(
        "/v1/chat/completions",
        json={
            "model": "auto",
            "auto": {"cost_quality": 0},
            "messages": [],
            "tools": [{"type": "function", "function": {"name": "lookup"}}],
        },
    )

    assert response.status_code == 200
    assert response.headers["x-os-auto-selected-model"] == "openai/gpt-oss-20b"


@pytest.mark.parametrize(
    "image_part",
    [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aW1hZ2U="}},
        {"type": "input_image", "image_url": "data:image/png;base64,aW1hZ2U="},
    ],
)
def test_auto_image_input_selects_only_verified_vision_route(image_part: dict[str, object]) -> None:
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")
    payload = {
        "model": "open-software/auto",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image."},
                    image_part,
                ],
            }
        ],
        # Image compatibility remains mandatory because it is an input
        # modality, even when optional parameter filtering is disabled.
        "provider": {"require_parameters": False},
    }
    policy = parse_routing_policy(payload, None)

    models = select_auto_models(config, policy, EndpointHealth(), payload)
    endpoints = route_endpoints(config, models[0], policy, EndpointHealth())

    assert policy.required_capabilities == frozenset({"vision"})
    assert models == ["moonshotai/kimi-k2.6"]
    assert [endpoint.id for endpoint in endpoints] == ["venice-kimi-k2.6-private"]


def test_explicit_vision_model_selects_only_vision_endpoint() -> None:
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")
    payload = {
        "model": "moonshotai/kimi-k2.6",
        "messages": [
            {
                "role": "user",
                "content": {
                    "type": "image_url",
                    "image_url": {"url": "https://example.com/image.png"},
                },
            }
        ],
    }
    policy = parse_routing_policy(payload, None)

    endpoints = route_endpoints(config, payload["model"], policy, EndpointHealth())

    assert policy.required_capabilities == frozenset({"vision"})
    assert [endpoint.id for endpoint in endpoints] == ["venice-kimi-k2.6-private"]


def test_auto_bare_image_content_uses_same_routing_and_billing_detection() -> None:
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")
    content = {
        "type": "image_url",
        "image_url": {"url": "https://example.com/image.png"},
    }
    messages = [{"role": "user", "content": content}]
    payload = {"model": "open-software/auto", "messages": messages}

    _enforce_non_text_output_cap(payload)
    policy = parse_routing_policy(payload, None)
    models = select_auto_models(config, policy, EndpointHealth(), payload)
    endpoints = route_endpoints(config, models[0], policy, EndpointHealth())

    assert payload["messages"] is messages
    assert payload["messages"][0]["content"] is content
    assert payload["max_completion_tokens"] == DEFAULT_NON_TEXT_MAX_COMPLETION_TOKENS
    assert policy.required_capabilities == frozenset({"vision"})
    assert models == ["moonshotai/kimi-k2.6"]
    assert [endpoint.id for endpoint in endpoints] == ["venice-kimi-k2.6-private"]


def test_auto_responses_input_image_uses_vision_routing_and_billing_detection() -> None:
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")
    payload = {
        "model": "open-software/auto",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Describe this image."},
                    {
                        "type": "input_image",
                        "image_url": "https://example.com/image.png",
                    },
                ],
            }
        ],
    }

    _enforce_non_text_output_cap(payload)
    policy = parse_routing_policy(payload, None)
    models = select_auto_models(config, policy, EndpointHealth(), payload)
    endpoints = route_endpoints(config, models[0], policy, EndpointHealth())

    assert payload["max_completion_tokens"] == DEFAULT_NON_TEXT_MAX_COMPLETION_TOKENS
    assert policy.required_capabilities == frozenset({"vision"})
    assert models == ["moonshotai/kimi-k2.6"]
    assert [endpoint.id for endpoint in endpoints] == ["venice-kimi-k2.6-private"]


def test_auto_responses_input_file_fails_closed(routed_client: TestClient) -> None:
    response = routed_client.post(
        "/v1/chat/completions",
        json={
            "model": "open-software/auto",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_file", "file_id": "file-1"},
                    ],
                }
            ],
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "auto_route_unavailable"
    assert "required capabilities (file_input)" in response.json()["error"]["message"]


def test_non_text_output_cap_is_forwarded_without_rewriting_media(
    routed_client: TestClient,
) -> None:
    config = routed_client.app.state.config
    model_id = "openai/gpt-oss-20b"
    model = config.models[model_id]
    config.models[model_id] = replace(
        model,
        capabilities=model.capabilities.union({"vision"}),
        input_modalities=("text", "image"),
    )
    endpoint_id = "venice-gpt-oss-20b"
    endpoint = config.endpoints[endpoint_id]
    config.endpoints[endpoint_id] = replace(
        endpoint,
        capabilities=endpoint.capabilities.union({"vision"}),
    )
    content = {
        "type": "image_url",
        "image_url": {"url": "https://example.com/image.png", "detail": "high"},
    }
    messages = [{"role": "user", "content": content}]

    response = routed_client.post(
        "/v1/chat/completions",
        json={"model": "open-software/auto", "messages": messages},
    )

    assert response.status_code == 200
    forwarded = routed_client.app.state.test_payloads[-1]
    assert forwarded["model"] == endpoint.upstream_model
    assert forwarded["max_completion_tokens"] == DEFAULT_NON_TEXT_MAX_COMPLETION_TOKENS
    assert forwarded["messages"] == messages


def test_text_request_without_output_limit_preserves_provider_default(
    routed_client: TestClient,
) -> None:
    response = routed_client.post(
        "/v1/chat/completions",
        json={
            "model": "openai/gpt-oss-20b",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert response.status_code == 200
    forwarded = routed_client.app.state.test_payloads[-1]
    assert "max_completion_tokens" not in forwarded
    assert "max_tokens" not in forwarded


def test_text_dual_output_limits_forward_only_the_larger_bound(
    routed_client: TestClient,
) -> None:
    response = routed_client.post(
        "/v1/chat/completions",
        json={
            "model": "openai/gpt-oss-20b",
            "messages": [{"role": "user", "content": "hello"}],
            "max_completion_tokens": 1,
            "max_tokens": 4_000,
        },
    )

    assert response.status_code == 200
    forwarded = routed_client.app.state.test_payloads[-1]
    assert forwarded["max_completion_tokens"] == 4_000
    assert "max_tokens" not in forwarded


def test_legacy_completions_without_output_limit_preserves_provider_default(
    routed_client: TestClient,
) -> None:
    response = routed_client.post(
        "/v1/completions",
        json={"model": "openai/gpt-oss-20b", "prompt": "hello"},
    )

    assert response.status_code == 200
    forwarded = routed_client.app.state.test_payloads[-1]
    assert "max_tokens" not in forwarded
    assert "max_completion_tokens" not in forwarded


def test_text_null_output_limits_are_removed_without_adding_a_default(
    routed_client: TestClient,
) -> None:
    response = routed_client.post(
        "/v1/chat/completions",
        json={
            "model": "openai/gpt-oss-20b",
            "messages": [{"role": "user", "content": "hello"}],
            "max_completion_tokens": None,
            "max_tokens": None,
        },
    )

    assert response.status_code == 200
    forwarded = routed_client.app.state.test_payloads[-1]
    assert "max_completion_tokens" not in forwarded
    assert "max_tokens" not in forwarded


def test_legacy_completions_dual_limits_forward_larger_as_max_tokens(
    routed_client: TestClient,
) -> None:
    response = routed_client.post(
        "/v1/completions",
        json={
            "model": "openai/gpt-oss-20b",
            "prompt": "hello",
            "max_completion_tokens": 4_000,
            "max_tokens": 1,
        },
    )

    assert response.status_code == 200
    forwarded = routed_client.app.state.test_payloads[-1]
    assert forwarded["max_tokens"] == 4_000
    assert "max_completion_tokens" not in forwarded


@pytest.mark.parametrize("field", ["max_completion_tokens", "max_tokens"])
def test_text_output_limit_rejects_non_positive_or_non_integer_values(
    routed_client: TestClient,
    field: str,
) -> None:
    payload_count = len(routed_client.app.state.test_payloads)

    response = routed_client.post(
        "/v1/chat/completions",
        json={
            "model": "openai/gpt-oss-20b",
            "messages": [{"role": "user", "content": "hello"}],
            field: "2048",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_output_limit"
    assert len(routed_client.app.state.test_payloads) == payload_count


def test_non_text_dual_output_limits_use_larger_bound_everywhere(
    routed_client: TestClient,
) -> None:
    config = routed_client.app.state.config
    model_id = "openai/gpt-oss-20b"
    model = config.models[model_id]
    config.models[model_id] = replace(
        model,
        capabilities=model.capabilities.union({"vision"}),
        input_modalities=("text", "image"),
    )
    endpoint_id = "venice-gpt-oss-20b"
    endpoint = config.endpoints[endpoint_id]
    config.endpoints[endpoint_id] = replace(
        endpoint,
        capabilities=endpoint.capabilities.union({"vision"}),
    )
    messages = [
        {
            "role": "user",
            "content": {
                "type": "image_url",
                "image_url": {"url": "https://example.com/image.png"},
            },
        }
    ]

    response = routed_client.post(
        "/v1/chat/completions",
        json={
            "model": "open-software/auto",
            "auto": {"cost_quality": 0},
            "messages": messages,
            "max_completion_tokens": 1,
            "max_tokens": 40_000,
        },
    )

    assert response.status_code == 200
    assert response.headers["x-os-auto-selected-model"] == model_id
    forwarded = routed_client.app.state.test_payloads[-1]
    assert forwarded["model"] == endpoint.upstream_model
    assert forwarded["max_completion_tokens"] == 40_000
    assert "max_tokens" not in forwarded
    assert forwarded["messages"] == messages
    reservation = _estimate_reservation_cost(
        config.endpoints[endpoint_id],
        forwarded,
        len(json.dumps(forwarded).encode()),
        config.models[model_id].max_context_length,
        config.models[model_id].max_output_length,
    )
    assert reservation == _actual_cost(
        config.endpoints[endpoint_id],
        {"prompt_tokens": 131_072, "completion_tokens": 40_000},
    )


@pytest.mark.parametrize(
    ("max_completion_tokens", "max_tokens"),
    [
        ("1", 2),
        (1, False),
        (0, 1),
        (1, -1),
        (1, 1.5),
    ],
)
def test_non_text_invalid_dual_output_limits_fail_closed(
    routed_client: TestClient,
    max_completion_tokens: object,
    max_tokens: object,
) -> None:
    response = routed_client.post(
        "/v1/chat/completions",
        json={
            "model": "open-software/auto",
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/image.png"},
                    },
                }
            ],
            "max_completion_tokens": max_completion_tokens,
            "max_tokens": max_tokens,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_output_limit"
    assert "must be a positive integer" in response.json()["error"]["message"]


@pytest.mark.parametrize("name", ["max_completion_tokens", "max_tokens"])
@pytest.mark.parametrize("value", [-1, 0, False, 1.5, "1"])
def test_non_text_invalid_single_output_limit_fails_without_forwarding(
    routed_client: TestClient,
    name: str,
    value: object,
) -> None:
    forwarded_before = len(routed_client.app.state.test_payloads)

    response = routed_client.post(
        "/v1/chat/completions",
        json={
            "model": "open-software/auto",
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/image.png"},
                    },
                }
            ],
            name: value,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_output_limit"
    assert response.json()["error"]["message"] == f"{name} must be a positive integer"
    assert len(routed_client.app.state.test_payloads) == forwarded_before


@pytest.mark.parametrize(
    ("limits", "expected"),
    [
        ({"max_tokens": None}, {"max_completion_tokens": 2_048}),
        (
            {"max_completion_tokens": None, "max_tokens": 100},
            {"max_completion_tokens": 100},
        ),
        (
            {"max_completion_tokens": 100, "max_tokens": None},
            {"max_completion_tokens": 100},
        ),
    ],
)
def test_non_text_null_output_limits_are_removed_before_forwarding(
    routed_client: TestClient,
    limits: dict[str, object],
    expected: dict[str, int],
) -> None:
    response = routed_client.post(
        "/v1/chat/completions",
        json={
            "model": "openai/gpt-oss-20b",
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/image.png"},
                    },
                }
            ],
            **limits,
        },
    )

    assert response.status_code == 200
    forwarded = routed_client.app.state.test_payloads[-1]
    assert {
        name: forwarded[name]
        for name in ("max_completion_tokens", "max_tokens")
        if name in forwarded
    } == expected


def test_auto_image_input_returns_actionable_error_without_vision_route(
    routed_client: TestClient,
) -> None:
    response = routed_client.post(
        "/v1/chat/completions",
        json={
            "model": "open-software/auto",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}
                    ],
                }
            ],
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "auto_route_unavailable"
    assert "required capabilities (vision)" in response.json()["error"]["message"]


def test_auto_large_image_data_url_does_not_consume_text_context() -> None:
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")
    data_url = (
        "data:image/png;base64,"
        + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 1_000_000).decode()
    )
    payload = {
        "model": "open-software/auto",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image."},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    }
    policy = parse_routing_policy(payload, None)

    models = select_auto_models(config, policy, EndpointHealth(), payload)

    assert models == ["moonshotai/kimi-k2.6"]
    assert payload["messages"][0]["content"][1]["image_url"]["url"] == data_url


def test_non_text_estimate_is_size_independent_and_multiplies_per_input() -> None:
    def payload(urls: list[str]) -> dict[str, object]:
        return {
            "model": "open-software/auto",
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": url}} for url in urls],
                }
            ],
        }

    small_url = "data:image/png;base64,eA=="
    large_url = "data:image/png;base64," + base64.b64encode(b"x" * 1_000_000).decode()
    small = estimate_bounded_prompt_units(payload([small_url]))
    large = estimate_bounded_prompt_units(payload([large_url]))
    two_inputs = estimate_bounded_prompt_units(payload([small_url, large_url]))

    assert large == small
    assert two_inputs >= small + NON_TEXT_INPUT_HEURISTIC_ALLOWANCE


def test_attachment_estimate_multiplies_per_attachment() -> None:
    one = estimate_bounded_prompt_units(
        {"model": "open-software/auto", "attachments": [{"file_id": "file-1"}]}
    )
    two = estimate_bounded_prompt_units(
        {
            "model": "open-software/auto",
            "attachments": [{"file_id": "file-1"}, {"file_id": "file-2"}],
        }
    )

    assert two >= one + NON_TEXT_INPUT_HEURISTIC_ALLOWANCE


def _image_payload(url: str, model: str) -> dict[str, object]:
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": url}}],
            }
        ],
    }


@pytest.mark.parametrize("model", ["open-software/auto", "moonshotai/kimi-k2.6"])
def test_remote_image_billing_hold_covers_full_model_context(model: str) -> None:
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")
    route = config.endpoints["venice-kimi-k2.6-private"]
    context_limit = config.models[route.model_id].max_context_length
    max_output_length = config.models[route.model_id].max_output_length
    assert context_limit is not None
    assert max_output_length is not None
    payload = _image_payload("https://example.com/image.png", model)
    _enforce_non_text_output_cap(payload)
    request_bytes = len(json.dumps(payload).encode())

    reservation = _estimate_reservation_cost(
        route,
        payload,
        request_bytes,
        context_limit,
        max_output_length,
    )
    maximum_prompt_cost = _actual_cost(
        route,
        {
            "prompt_tokens": context_limit,
            "completion_tokens": DEFAULT_NON_TEXT_MAX_COMPLETION_TOKENS,
        },
    )

    assert request_bytes < context_limit
    assert payload["max_completion_tokens"] == DEFAULT_NON_TEXT_MAX_COMPLETION_TOKENS
    assert reservation >= maximum_prompt_cost


def test_inline_image_billing_hold_keeps_larger_raw_request_bound() -> None:
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")
    route = config.endpoints["venice-kimi-k2.6-private"]
    context_limit = config.models[route.model_id].max_context_length
    max_output_length = config.models[route.model_id].max_output_length
    assert context_limit is not None
    assert max_output_length is not None
    large_url = "data:image/png;base64," + base64.b64encode(b"x" * 1_000_000).decode()
    payload = _image_payload(large_url, "open-software/auto")
    _enforce_non_text_output_cap(payload)
    request_bytes = len(json.dumps(payload).encode())

    estimate = _estimate_reservation_cost(
        route,
        payload,
        request_bytes,
        context_limit,
        max_output_length,
    )
    expected = max(
        1,
        (
            request_bytes * route.input_price_microusd_per_million
            + DEFAULT_NON_TEXT_MAX_COMPLETION_TOKENS * route.output_price_microusd_per_million
            + 999_999
        )
        // 1_000_000,
    )

    assert request_bytes > context_limit
    assert estimate == expected


def test_non_text_billing_hold_clamps_explicit_output_limit() -> None:
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")
    route = config.endpoints["venice-kimi-k2.6-private"]
    model = config.models[route.model_id]
    assert model.max_context_length is not None
    assert model.max_output_length is not None
    payload = {
        **_image_payload("https://example.com/image.png", route.model_id),
        "max_tokens": model.max_output_length * 2,
    }
    _enforce_non_text_output_cap(payload)
    request_bytes = len(json.dumps(payload).encode())

    reservation = _estimate_reservation_cost(
        route,
        payload,
        request_bytes,
        model.max_context_length,
        model.max_output_length,
    )
    configured_maximum_cost = _actual_cost(
        route,
        {
            "prompt_tokens": model.max_context_length,
            "completion_tokens": model.max_output_length,
        },
    )

    assert reservation == configured_maximum_cost
    assert payload["max_completion_tokens"] == model.max_output_length * 2
    assert "max_tokens" not in payload


class _CapturingSettlementRepository:
    def __init__(self) -> None:
        self.actual_microusd: int | None = None
        self.releases: list[tuple[str, str, str]] = []

    async def settle(
        self,
        key_id: str,
        billing_id: str,
        actual_microusd: int,
        event: dict[str, object],
    ) -> None:
        self.actual_microusd = actual_microusd

    async def release(self, key_id: str, billing_id: str, reason: str) -> None:
        self.releases.append((key_id, billing_id, reason))


class _AcceptingSettlementAccounts:
    def __init__(self) -> None:
        self.charged_microusd: int | None = None

    async def charge_wallet(
        self,
        grant: object,
        actual_microusd: int,
        billing_id: str,
        request_id: str,
    ) -> bool:
        self.charged_microusd = actual_microusd
        return True


def _settlement_request(
    repository: _CapturingSettlementRepository,
    accounts: _AcceptingSettlementAccounts,
) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(managed_keys=repository, accounts=accounts),
        ),
        state=SimpleNamespace(request_id="req-no-usage"),
    )


@pytest.mark.anyio
async def test_remote_image_missing_usage_charges_fallback_not_reservation() -> None:
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")
    route = config.endpoints["venice-kimi-k2.6-private"]
    model = config.models[route.model_id]
    payload = _image_payload("https://example.com/image.png", "open-software/auto")
    _enforce_non_text_output_cap(payload)
    request_bytes = len(json.dumps(payload).encode())
    reservation = _estimate_reservation_cost(
        route,
        payload,
        request_bytes,
        model.max_context_length,
        model.max_output_length,
    )
    fallback_charge = _estimate_fallback_charge(
        route,
        payload,
        request_bytes,
        model.max_context_length,
        model.max_output_length,
    )
    repository = _CapturingSettlementRepository()
    accounts = _AcceptingSettlementAccounts()
    request = _settlement_request(repository, accounts)
    billing = BillingContext("key-1", "bill-1", reservation, fallback_charge, None)

    await _settle_billing(
        request,
        billing,
        route,
        route.model_id,
        "no-retention",
        route.id,
        usage=None,
    )

    assert fallback_charge < reservation
    assert repository.actual_microusd == fallback_charge
    assert accounts.charged_microusd == fallback_charge


@pytest.mark.anyio
@pytest.mark.parametrize(
    "usage",
    [
        {"total_tokens": 123},
        {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    ],
)
async def test_unbillable_usage_warns_and_charges_fallback(
    usage: dict[str, int], caplog: pytest.LogCaptureFixture
) -> None:
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")
    route = config.endpoints["venice-kimi-k2.6-private"]
    repository = _CapturingSettlementRepository()
    accounts = _AcceptingSettlementAccounts()
    request = _settlement_request(repository, accounts)
    billing = BillingContext("key-1", "bill-1", 100_000, 5_000, None)

    await _settle_billing(
        request,
        billing,
        route,
        route.model_id,
        "no-retention",
        route.id,
        usage=usage,
    )

    assert repository.actual_microusd == billing.fallback_charge_microusd
    assert accounts.charged_microusd == billing.fallback_charge_microusd
    assert "billing_usage_fallback" in caplog.text


@pytest.mark.anyio
async def test_stream_without_usage_charges_fallback_not_reservation() -> None:
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")
    route = config.endpoints["venice-kimi-k2.6-private"]
    repository = _CapturingSettlementRepository()
    accounts = _AcceptingSettlementAccounts()
    request = _settlement_request(repository, accounts)
    billing = BillingContext("key-1", "bill-1", 100_000, 5_000, None)
    health = EndpointHealth()
    upstream = httpx.Response(
        200,
        content=b'data: {"choices":[]}\n\ndata: [DONE]\n\n',
    )

    chunks = [
        chunk
        async for chunk in _metered_stream(
            request,
            upstream,
            billing,
            route,
            route.model_id,
            "no-retention",
            route.id,
            endpoint_health=health,
            latency=0.1,
        )
    ]

    assert chunks
    assert repository.actual_microusd == billing.fallback_charge_microusd
    assert accounts.charged_microusd == billing.fallback_charge_microusd
    assert repository.releases == []
    stats = health.get(route.id)
    assert stats.attempts == 1
    assert stats.consecutive_failures == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("environment", "maximum", "content", "message"),
    [
        (
            "MAX_SSE_LINE_BYTES",
            32,
            b"data: " + b"x" * 64,
            "SSE line exceeds the size limit",
        ),
        (
            "MAX_SSE_RESPONSE_BYTES",
            32,
            b"data: {}\n" * 8,
            "SSE response exceeds the size limit",
        ),
    ],
)
async def test_metered_stream_bounds_line_and_total_size_and_closes_upstream(
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
    maximum: int,
    content: bytes,
    message: str,
) -> None:
    monkeypatch.setenv("MAX_SSE_LINE_BYTES", "1024")
    monkeypatch.setenv("MAX_SSE_RESPONSE_BYTES", "1024")
    monkeypatch.setenv(environment, str(maximum))
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")
    route = config.endpoints["venice-kimi-k2.6-private"]
    repository = _CapturingSettlementRepository()
    accounts = _AcceptingSettlementAccounts()
    request = _settlement_request(repository, accounts)
    billing = BillingContext("key-1", "bill-1", 100_000, 5_000, None)
    health = EndpointHealth()
    stream = TrackingAsyncByteStream(content)
    upstream = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=stream,
    )

    with pytest.raises(ValueError, match=message):
        _ = [
            chunk
            async for chunk in _metered_stream(
                request,
                upstream,
                billing,
                route,
                route.model_id,
                "no-retention",
                route.id,
                endpoint_health=health,
                latency=0.1,
            )
        ]

    assert stream.closed is True
    assert repository.actual_microusd is None
    assert repository.releases == [("key-1", "bill-1", "upstream_stream_too_large")]
    stats = health.get(route.id)
    assert stats.attempts == 1
    assert stats.consecutive_failures == 1


@pytest.mark.anyio
async def test_metered_stream_releases_billing_and_marks_truncated_upstream_failed() -> None:
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")
    route = config.endpoints["venice-kimi-k2.6-private"]
    repository = _CapturingSettlementRepository()
    accounts = _AcceptingSettlementAccounts()
    request = _settlement_request(repository, accounts)
    billing = BillingContext("key-1", "bill-1", 100_000, 5_000, None)
    health = EndpointHealth()
    stream = TrackingAsyncByteStream(b'data: {"choices":[]}\n\n')
    upstream = httpx.Response(200, stream=stream)

    with pytest.raises(httpx.RemoteProtocolError, match=r"before \[DONE\]"):
        _ = [
            chunk
            async for chunk in _metered_stream(
                request,
                upstream,
                billing,
                route,
                route.model_id,
                "no-retention",
                route.id,
                endpoint_health=health,
                latency=0.1,
            )
        ]

    assert stream.closed is True
    assert repository.actual_microusd is None
    assert repository.releases == [("key-1", "bill-1", "upstream_stream_invalid")]
    stats = health.get(route.id)
    assert stats.attempts == 1
    assert stats.consecutive_failures == 1


@pytest.mark.anyio
async def test_client_disconnect_settles_without_health_penalty() -> None:
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")
    route = config.endpoints["venice-kimi-k2.6-private"]
    repository = _CapturingSettlementRepository()
    accounts = _AcceptingSettlementAccounts()
    request = _settlement_request(repository, accounts)
    billing = BillingContext("key-1", "bill-1", 100_000, 5_000, None)
    health = EndpointHealth()
    stream = TrackingAsyncByteStream(b'data: {"choices":[]}\n\ndata: [DONE]\n\n')
    upstream = httpx.Response(200, stream=stream)
    metered = _metered_stream(
        request,
        upstream,
        billing,
        route,
        route.model_id,
        "no-retention",
        route.id,
        endpoint_health=health,
        latency=0.1,
    )

    assert await anext(metered)
    await metered.aclose()

    assert stream.closed is True
    assert repository.actual_microusd == billing.fallback_charge_microusd
    assert repository.releases == []
    stats = health.get(route.id)
    assert stats.attempts == 0
    assert stats.consecutive_failures == 0


def test_text_missing_usage_fallback_is_lower_than_conservative_reservation() -> None:
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")
    route = config.endpoints["venice-kimi-k2.6-private"]
    model = config.models[route.model_id]
    payload = {
        "model": route.model_id,
        "messages": [{"role": "user", "content": "Summarize this meeting."}],
    }
    request_bytes = len(json.dumps(payload).encode())

    reservation = _estimate_reservation_cost(
        route,
        payload,
        request_bytes,
        model.max_context_length,
        model.max_output_length,
        reserve_omitted_output_maximum=True,
    )
    fallback_charge = _estimate_fallback_charge(
        route,
        payload,
        request_bytes,
        model.max_context_length,
    )

    configured_output_maximum = min(
        limit for limit in (model.max_context_length, model.max_output_length) if limit is not None
    )
    assert reservation == _actual_cost(
        route,
        {
            "prompt_tokens": request_bytes,
            "completion_tokens": configured_output_maximum,
        },
    )
    assert fallback_charge < reservation


@pytest.mark.anyio
async def test_managed_text_without_limit_fails_when_model_has_no_billing_bound() -> None:
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")
    route = config.endpoints["venice-kimi-k2.6-private"]
    config.models[route.model_id] = replace(
        config.models[route.model_id],
        max_context_length=None,
        max_output_length=None,
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                managed_keys=object(),
                config=config,
            )
        ),
        state=SimpleNamespace(
            principal=Principal(
                tenant_id="tenant-1",
                key_fingerprint="fingerprint",
                key_id="key-1",
                user_id="user-1",
            )
        ),
    )
    payload = {
        "model": route.model_id,
        "messages": [{"role": "user", "content": "hello"}],
    }

    response = await _prepare_billing(
        request,
        route,
        payload,
        len(json.dumps(payload).encode()),
        reserve_omitted_output_maximum=True,
    )

    assert response is not None
    assert response.status_code == 409
    assert json.loads(response.body)["error"]["code"] == "billing_bound_unavailable"


@pytest.mark.parametrize(
    ("request_input", "required_capability"),
    [
        ({"attachments": [{"file_id": "file-1"}]}, "attachments"),
        (
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_file", "file_data": "data:text/plain;base64,eA=="}
                        ],
                    }
                ]
            },
            "file_input",
        ),
        (
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "file", "file": {"file_id": "file-1"}}],
                    }
                ]
            },
            "file_input",
        ),
        (
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {"data": "YXVkaW8=", "format": "wav"},
                            }
                        ],
                    }
                ]
            },
            "audio_input",
        ),
        (
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "audio_url", "audio_url": {"url": "https://example.com/a"}}
                        ],
                    }
                ]
            },
            "audio_input",
        ),
    ],
)
def test_auto_unsupported_input_modalities_fail_closed(
    request_input: dict[str, object], required_capability: str
) -> None:
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")
    payload = {
        "model": "open-software/auto",
        "provider": {"require_parameters": False},
        **request_input,
    }
    policy = parse_routing_policy(payload, None)

    with pytest.raises(RoutingError) as raised:
        select_auto_models(config, policy, EndpointHealth(), payload)

    assert required_capability in policy.required_capabilities
    assert raised.value.code == "auto_route_unavailable"
    assert required_capability in str(raised.value)


@pytest.mark.parametrize(
    "request_input",
    [
        {"attachments": [{"file_id": "file-1"}]},
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}
                    ],
                }
            ]
        },
        {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "input_file", "file_id": "file-1"}],
                }
            ]
        },
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {"data": "YXVkaW8=", "format": "wav"},
                        }
                    ],
                }
            ]
        },
    ],
)
def test_explicit_model_non_text_input_preserves_passthrough(
    routed_client: TestClient, request_input: dict[str, object]
) -> None:
    response = routed_client.post(
        "/v1/chat/completions",
        json={"model": "openai/gpt-oss-20b", **request_input},
    )

    assert response.status_code == 200
    assert response.json()["model"] == "openai/gpt-oss-20b"


def test_auto_image_and_tools_require_both_capabilities() -> None:
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")
    payload = {
        "model": "open-software/auto",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Read the image and save a note."},
                    {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
                ],
            }
        ],
        "tools": [{"type": "function", "function": {"name": "save_note"}}],
    }
    policy = parse_routing_policy(payload, None)

    models = select_auto_models(config, policy, EndpointHealth(), payload)
    endpoints = route_endpoints(config, models[0], policy, EndpointHealth())

    assert policy.required_capabilities == frozenset({"tools", "vision"})
    assert models == ["moonshotai/kimi-k2.6"]
    assert [endpoint.id for endpoint in endpoints] == ["venice-kimi-k2.6-private"]


def test_auto_forwards_hermes_shape_with_selected_model(routed_client: TestClient) -> None:
    response = routed_client.post(
        "/v1/chat/completions",
        json={
            "model": "open-software/auto",
            "auto": {"cost_quality": 1},
            "messages": [
                {"role": "system", "content": "Use tools when needed."},
                {"role": "user", "content": "Save a note."},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "save_note",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "max_completion_tokens": 32768,
        },
    )

    assert response.status_code == 200
    assert response.headers["x-os-auto-selected-model"] == "openai/gpt-oss-20b"


@pytest.mark.parametrize("status_code", [400, 401, 403, 413, 422])
def test_auto_preserves_customer_error_without_retry_or_health_penalty(
    routed_client: TestClient,
    status_code: int,
) -> None:
    payload_count = len(routed_client.app.state.test_payloads)
    response = routed_client.post(
        "/v1/chat/completions",
        json={
            "model": "open-software/auto",
            "auto": {"cost_quality": 1},
            "messages": [{"role": "user", "content": "hello"}],
            "metadata": {"force_primary_status": status_code},
        },
    )

    assert response.status_code == status_code
    assert response.json() == {
        "error": {
            "message": "request rejected by provider",
            "type": "invalid_request_error",
            "code": f"forced_{status_code}",
        }
    }
    assert len(routed_client.app.state.test_payloads) == payload_count + 1
    stats = routed_client.app.state.endpoint_health.get("phala-gpt-oss-20b")
    assert stats.attempts == 0
    assert stats.consecutive_failures == 0


@pytest.mark.parametrize("status_code", [200, 400])
def test_routed_inference_bounds_non_stream_upstream_body(
    routed_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    monkeypatch.setenv("MAX_UPSTREAM_JSON_RESPONSE_BYTES", "64")
    monkeypatch.setenv("MAX_UPSTREAM_ERROR_RESPONSE_BYTES", "64")

    response = routed_client.post(
        "/v1/chat/completions",
        json={
            "model": "openai/gpt-oss-20b",
            "messages": [{"role": "user", "content": "hello"}],
            "metadata": {"oversized_upstream_status": status_code},
        },
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_response_too_large"
    assert routed_client.app.state.test_response_streams[-1].closed is True


def test_explicit_model_preserves_provider_400(routed_client: TestClient) -> None:
    response = routed_client.post(
        "/v1/chat/completions",
        json={
            "model": "openai/gpt-oss-20b",
            "messages": [{"role": "user", "content": "hello"}],
            "metadata": {"force_primary_400": True},
        },
    )

    assert response.status_code == 400
    assert response.json() == {"error": "model-specific schema rejection"}


def test_auto_preserves_provider_400_when_fallbacks_are_disabled(
    routed_client: TestClient,
) -> None:
    response = routed_client.post(
        "/v1/chat/completions",
        json={
            "model": "open-software/auto",
            "auto": {"cost_quality": 1},
            "messages": [{"role": "user", "content": "hello"}],
            "metadata": {"force_primary_400": True},
            "provider": {"allow_fallbacks": False},
        },
    )

    assert response.status_code == 400
    assert response.json() == {"error": "model-specific schema rejection"}


@pytest.mark.parametrize("value", [-0.01, 1.01, "quality"])
def test_auto_rejects_invalid_slider(routed_client: TestClient, value: object) -> None:
    response = routed_client.post(
        "/v1/chat/completions",
        json={"model": "auto", "auto": {"cost_quality": value}, "messages": []},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_auto_policy"


def test_auto_context_filter_uses_conservative_utf8_bound(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG)
    config = GatewayConfig.load(path)
    payload = {
        "model": "auto",
        "messages": [{"role": "user", "content": "界" * 12_000}],
        "max_tokens": 1,
    }
    policy = parse_routing_policy(payload, None)

    models = select_auto_models(config, policy, EndpointHealth(), payload)

    assert "backup/private-model" not in models
    assert models == ["openai/gpt-oss-20b"]


def test_auto_excludes_model_when_requested_output_exceeds_model_limit(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        CONFIG.replace(
            "max_context_length: 131072",
            "max_context_length: 131072\n    max_output_length: 32768",
        ).replace(
            "max_context_length: 32768",
            "max_context_length: 65536\n    max_output_length: 65536",
        )
    )
    config = GatewayConfig.load(path)
    payload = {
        "model": "auto",
        "messages": [{"role": "user", "content": "short"}],
        "max_tokens": 40_000,
    }
    policy = parse_routing_policy(payload, None)

    models = select_auto_models(config, policy, EndpointHealth(), payload)

    assert models == ["backup/private-model"]


def test_fallback_preserves_privacy_and_reports_provider(routed_client: TestClient) -> None:
    response = routed_client.post(
        "/v1/chat/completions",
        json={
            "model": "openai/gpt-oss-20b",
            "messages": [{"role": "user", "content": "hello"}],
            "privacy": {"minimum": "tee", "require_response_proof": True},
            "provider": {"sort": "price"},
        },
    )

    assert response.status_code == 200
    assert response.headers["x-os-provider"] == "venice"
    assert response.headers["x-os-privacy-level"] == "tee"
    assert response.headers["x-os-attestation-status"] == "present"
    assert len(routed_client.app.state.test_calls) == 2
    receipt = routed_client.get("/v1/privacy/receipts/gen-private-1")
    assert receipt.status_code == 200
    assert receipt.json()["provider"] == "venice"
    generation = routed_client.get("/v1/generation?id=gen-private-1")
    assert generation.status_code == 200
    assert generation.json()["data"]["endpoint"] == "venice-gpt-oss-20b"


@pytest.mark.parametrize(
    ("legacy_id", "canonical_id"),
    [
        ("zai-org-glm-5-2", "openai/gpt-oss-20b"),
        ("nvidia-nemotron-3-nano-30b-a3b", "backup/private-model"),
    ],
)
def test_legacy_june_model_aliases_are_resolved(
    routed_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    legacy_id: str,
    canonical_id: str,
) -> None:
    from sovereign_gateway import app as app_module

    monkeypatch.setitem(app_module.MODEL_ALIASES, legacy_id, canonical_id)
    response = routed_client.post(
        "/v1/chat/completions",
        json={"model": legacy_id, "messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 200
    assert response.json()["model"] == canonical_id


def test_unknown_saved_model_refreshes_live_catalog_before_aliasing(
    routed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIVE_PROVIDER_CATALOG_ENABLED", "true")
    routed_client.app.state.provider_catalog.http = routed_client.app.state.http
    routed_client.app.state.provider_catalog._last_attempt = 0
    routed_client.app.state.provider_catalog._expires_at = 0

    response = routed_client.post(
        "/v1/chat/completions",
        json={
            "model": "zai-org-glm-5-2",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert response.status_code == 200
    assert response.json()["model"] == "zai-org-glm-5-2"
    assert any(url.endswith("/v1/models") for url in routed_client.app.state.test_calls)


def test_e2ee_endpoint_requires_client_encryption_headers() -> None:
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")
    health = EndpointHealth()
    without_encryption = parse_routing_policy(
        {"model": "openai/gpt-oss-20b", "privacy": {"minimum": "e2ee-tee"}},
        None,
    )
    with pytest.raises(RoutingError, match="no endpoint satisfying"):
        route_endpoints(config, "openai/gpt-oss-20b", without_encryption, health)

    encrypted = parse_routing_policy(
        {"model": "openai/gpt-oss-20b", "privacy": {"minimum": "e2ee-tee"}},
        None,
        client_encrypted=True,
    )
    endpoints = route_endpoints(config, "openai/gpt-oss-20b", encrypted, health)
    assert [endpoint.id for endpoint in endpoints] == ["venice-gpt-oss-20b-e2ee"]

    independently_verified = parse_routing_policy(
        {"model": "openai/gpt-oss-20b", "privacy": {"minimum": "verified-tee"}},
        None,
        client_encrypted=True,
    )
    with pytest.raises(RoutingError, match="no endpoint satisfying"):
        route_endpoints(config, "openai/gpt-oss-20b", independently_verified, health)


@pytest.mark.parametrize(
    ("model", "venice_private", "phala"),
    [
        ("openai/gpt-oss-120b", "venice-gpt-oss-120b-private", "phala-gpt-oss-120b"),
        ("google/gemma-3-27b-it", "venice-gemma-3-27b-private", "phala-gemma-3-27b"),
        ("z-ai/glm-5.2", "venice-glm-5.2-private", "phala-glm-5.2"),
        ("qwen/qwen3.6-27b", "venice-qwen3.6-27b-private", "phala-qwen3.6-27b"),
        ("moonshotai/kimi-k2.6", "venice-kimi-k2.6-private", "phala-kimi-k2.6"),
    ],
)
def test_preferred_privacy_uses_venice_private_then_phala(
    model: str, venice_private: str, phala: str
) -> None:
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")
    payload = {"model": model}
    preferred = parse_routing_policy(payload, None)
    required = parse_routing_policy(payload, "required")

    preferred_endpoints = route_endpoints(config, model, preferred, EndpointHealth())
    required_endpoints = route_endpoints(config, model, required, EndpointHealth())

    assert [endpoint.id for endpoint in preferred_endpoints] == [venice_private, phala]
    assert [endpoint.id for endpoint in required_endpoints] == [phala]


def test_default_privacy_requires_zero_retention_but_not_tee() -> None:
    preferred = parse_routing_policy({"model": "openai/gpt-oss-120b"}, None)
    required = parse_routing_policy({"model": "openai/gpt-oss-120b"}, "required")

    assert preferred.privacy.minimum == "no-retention"
    assert preferred.privacy.allow_downgrade is False
    assert required.privacy.minimum == "tee"
    assert required.privacy.allow_downgrade is False


@pytest.mark.parametrize(
    ("section", "field", "error_code"),
    [
        ("privacy", "allow_downgrade", "invalid_privacy_policy"),
        ("privacy", "require_zero_retention", "invalid_privacy_policy"),
        ("privacy", "require_response_proof", "invalid_privacy_policy"),
        ("provider", "allow_fallbacks", "invalid_provider_policy"),
        ("provider", "require_parameters", "invalid_provider_policy"),
    ],
)
@pytest.mark.parametrize("value", ["false", 0, None])
def test_policy_boolean_fields_reject_non_booleans(
    section: str,
    field: str,
    error_code: str,
    value: object,
) -> None:
    with pytest.raises(RoutingError) as raised:
        parse_routing_policy(
            {"model": "openai/gpt-oss-20b", section: {field: value}},
            None,
        )

    assert raised.value.code == error_code
    assert str(raised.value) == f"{section}.{field} must be a boolean"


def test_venice_private_response_headers_report_non_tee_privacy() -> None:
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")
    endpoint = config.endpoints["venice-glm-5.2-private"]

    headers = _endpoint_headers(endpoint, None)

    assert headers["x-compute-privacy"] == "standard"
    assert headers["x-os-privacy-level"] == "no-retention"


def test_production_auto_slider_is_monotonic_for_quality_and_primary_cost() -> None:
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")
    health = EndpointHealth()
    observed: list[tuple[float, int]] = []

    for value in (0, 0.1, 0.25, 0.5, 0.75, 0.9, 1):
        payload = {
            "model": "open-software/auto",
            "messages": [{"role": "user", "content": "Summarize this meeting."}],
            "max_tokens": 1_000,
            "auto": {"cost_quality": value},
        }
        policy = parse_routing_policy(payload, None)
        model_id = select_auto_models(config, policy, health, payload)[0]
        endpoint = route_endpoints(config, model_id, policy, health)[0]
        request_cost = (
            endpoint.input_price_microusd_per_million
            + 1_000 * endpoint.output_price_microusd_per_million
        )
        observed.append((config.models[model_id].quality_score or 0, request_cost))

    assert [quality for quality, _ in observed] == sorted(quality for quality, _ in observed)
    assert [cost for _, cost in observed] == sorted(cost for _, cost in observed)


def test_provider_controls_can_disable_fallback(routed_client: TestClient) -> None:
    response = routed_client.post(
        "/v1/chat/completions",
        json={
            "model": "openai/gpt-oss-20b",
            "messages": [],
            "provider": {"only": ["phala"], "allow_fallbacks": False},
        },
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "all_endpoints_unavailable"


def test_preopened_endpoint_circuit_returns_service_unavailable(
    routed_client: TestClient,
) -> None:
    health = routed_client.app.state.endpoint_health
    for _ in range(health.failure_threshold):
        anyio.run(health.failure, "phala-gpt-oss-20b")
    payload_count = len(routed_client.app.state.test_payloads)

    response = routed_client.post(
        "/v1/chat/completions",
        json={
            "model": "openai/gpt-oss-20b",
            "messages": [],
            "provider": {"only": ["phala"], "allow_fallbacks": False},
        },
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "all_endpoints_unavailable"
    assert len(routed_client.app.state.test_payloads) == payload_count


def test_explicit_model_fallback_preserves_policy(routed_client: TestClient) -> None:
    response = routed_client.post(
        "/v1/chat/completions",
        json={
            "models": ["openai/gpt-oss-20b", "backup/private-model"],
            "messages": [],
            "provider": {"only": ["phala"]},
            "privacy": {"minimum": "tee", "require_zero_retention": True},
        },
    )

    assert response.status_code == 200
    assert response.headers["x-os-endpoint"] == "phala-backup"
    assert response.json()["model"] == "backup/private-model"


def test_model_and_endpoint_catalogs_expose_privacy(routed_client: TestClient) -> None:
    models = routed_client.get("/v1/models").json()["data"]
    assert models[0]["id"] == "open-software/auto"
    model = next(item for item in models if item["id"] == "openai/gpt-oss-20b")
    assert model["providers"] == ["phala", "venice"]
    assert model["privacy_levels"] == ["tee"]

    endpoints = routed_client.get("/v1/models/openai/gpt-oss-20b/endpoints")
    assert endpoints.status_code == 200
    assert len(endpoints.json()["data"]) == 2


def test_venice_catalog_shape_supports_june_bootstrap(routed_client: TestClient) -> None:
    text_models = routed_client.get("/v1/models?type=text")
    asr_models = routed_client.get("/v1/models?type=asr")

    assert text_models.status_code == 200
    auto = next(item for item in text_models.json()["data"] if item["id"] == "open-software/auto")
    assert auto["model_spec"]["pricing"]["input"]["usd"] > 0
    assert auto["model_spec"]["pricing"]["input"]["usd"] == 0.05
    assert auto["model_spec"]["pricing"]["output"]["usd"] == 0.19
    assert auto["model_spec"]["capabilities"]["supportsFunctionCalling"] is True
    assert auto["model_spec"]["availableContextTokens"] == 131072
    text = next(item for item in text_models.json()["data"] if item["id"] == "openai/gpt-oss-20b")
    assert text["type"] == "text"
    assert text["model_spec"]["pricing"]["input"]["usd"] == 0.04
    assert text["model_spec"]["availableContextTokens"] == 131072
    assert text["model_spec"]["capabilities"]["supportsFunctionCalling"] is True
    assert asr_models.status_code == 200
    assert asr_models.json()["data"][0]["type"] == "asr"
    assert asr_models.json()["data"][0]["model_spec"]["pricing"]["per_audio_second"]


def test_production_auto_catalog_advertises_guaranteed_image_input() -> None:
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")

    auto = _auto_catalog_model(config, created=0)
    kimi = config.models["moonshotai/kimi-k2.6"]

    assert auto["architecture"]["input_modalities"] == ["text", "image"]
    assert auto["model_spec"]["capabilities"]["supportsVision"] is True
    assert kimi.input_modalities == ("text", "image")
    assert "vision" in kimi.capabilities
    assert "vision" not in config.endpoints["phala-kimi-k2.6"].capabilities
    assert "vision" in config.endpoints["venice-kimi-k2.6-private"].capabilities


def test_audio_transcription_matches_venice_upstream_contract(routed_client: TestClient) -> None:
    response = routed_client.post(
        "/v1/audio/transcriptions",
        data={"model": "nvidia/parakeet-tdt-0.6b-v3", "response_format": "json"},
        files={"file": ("private-name.wav", b"RIFF-audio", "audio/wav")},
    )

    assert response.status_code == 200
    assert response.json() == {"text": "hello", "language": "en"}
    assert response.headers["x-os-billing-owner"] == "caller"
    assert response.headers["x-os-provider"] == "venice"


@pytest.mark.parametrize(
    ("response_format", "expected_body", "expected_content_type"),
    [
        ("text", b"hello world\n", "text/plain"),
        (
            "srt",
            b"1\n00:00:00,000 --> 00:00:01,000\nhello\n",
            "application/x-subrip",
        ),
        (
            "vtt",
            b"WEBVTT\n\n00:00.000 --> 00:01.000\nhello\n",
            "text/vtt",
        ),
    ],
)
def test_audio_transcription_supports_bounded_text_formats_with_safe_media_types(
    routed_client: TestClient,
    response_format: str,
    expected_body: bytes,
    expected_content_type: str,
) -> None:
    response = routed_client.post(
        "/v1/audio/transcriptions",
        data={
            "model": "nvidia/parakeet-tdt-0.6b-v3",
            "response_format": response_format,
        },
        files={"file": ("audio.wav", b"RIFF-audio", "audio/wav")},
    )

    assert response.status_code == 200
    assert response.content == expected_body
    assert response.headers["content-type"].startswith(expected_content_type)
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-os-provider"] == "venice"


def test_audio_transcription_rejects_unknown_response_format(
    routed_client: TestClient,
) -> None:
    call_count = len(routed_client.app.state.test_calls)

    response = routed_client.post(
        "/v1/audio/transcriptions",
        data={
            "model": "nvidia/parakeet-tdt-0.6b-v3",
            "response_format": "html",
        },
        files={"file": ("audio.wav", b"RIFF-audio", "audio/wav")},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_response_format"
    assert len(routed_client.app.state.test_calls) == call_count


def test_audio_transcription_rejects_unpriced_model(routed_client: TestClient) -> None:
    response = routed_client.post(
        "/v1/audio/transcriptions",
        data={"model": "unknown"},
        files={"file": ("audio.wav", b"RIFF-audio", "audio/wav")},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


def test_audio_transcription_rejects_oversized_body_before_parsing(
    routed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_AUDIO_BYTES", "8")
    response = routed_client.post(
        "/v1/audio/transcriptions",
        data={"model": "nvidia/parakeet-tdt-0.6b-v3"},
        files={"file": ("audio.wav", b"x" * 70_000, "audio/wav")},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


def test_audio_transcription_caps_chunked_body_while_parsing(
    routed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_AUDIO_BYTES", "8")
    boundary = "chunked-test"
    body = (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="model"\r\n\r\n'
            "nvidia/parakeet-tdt-0.6b-v3\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
            "Content-Type: audio/wav\r\n\r\n"
        ).encode()
        + b"x" * 70_000
        + f"\r\n--{boundary}--\r\n".encode()
    )

    response = routed_client.post(
        "/v1/audio/transcriptions",
        headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        content=(chunk for chunk in (body[:100], body[100:])),
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


def test_augment_search_matches_venice_upstream_contract(routed_client: TestClient) -> None:
    response = routed_client.post(
        "/v1/augment/search",
        json={"query": "private inference", "limit": 99, "search_provider": "brave"},
    )

    assert response.status_code == 200
    assert response.json()["results"][0]["url"] == "https://example.com"
    assert response.headers["x-os-billing-owner"] == "caller"


def test_augment_rejects_oversized_json_before_parsing(
    routed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_AUGMENT_REQUEST_BYTES", "32")
    response = routed_client.post("/v1/augment/search", json={"query": "x" * 100})

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


def test_augment_caps_chunked_json_while_reading(
    routed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_AUGMENT_REQUEST_BYTES", "32")
    body = b'{"query":"' + b"x" * 100 + b'"}'

    response = routed_client.post(
        "/v1/augment/search",
        headers={"content-type": "application/json"},
        content=(chunk for chunk in (body[:16], body[16:])),
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


def test_augment_never_relays_active_upstream_content_type(routed_client: TestClient) -> None:
    response = routed_client.post("/v1/augment/search", json={"query": "invalid-content-type"})

    assert response.status_code == 502
    assert response.headers["content-type"].startswith("application/json")
    assert "<script>" not in response.text


def test_augment_scrape_matches_venice_upstream_contract(routed_client: TestClient) -> None:
    response = routed_client.post("/v1/augment/scrape", json={"url": "https://93.184.216.34/page"})

    assert response.status_code == 200
    assert response.json()["format"] == "markdown"


@pytest.mark.parametrize(
    "target",
    [
        "http://example.com",
        "https://localhost/admin",
        "https://127.0.0.1/private",
        "https://169.254.169.254/metadata",
        "file:///etc/passwd",
    ],
)
def test_augment_scrape_blocks_ssrf_targets(routed_client: TestClient, target: str) -> None:
    response = routed_client.post("/v1/augment/scrape", json={"url": target})

    assert response.status_code == 400


def test_image_generation_matches_venice_upstream_contract(
    routed_client: TestClient,
) -> None:
    response = routed_client.post(
        "/v1/image/generate",
        json={"model": "chroma", "prompt": "a red bicycle", "format": "png"},
    )

    assert response.status_code == 200
    assert response.json() == {"images": ["aW1hZ2U="]}
    assert response.headers["x-os-billing-owner"] == "caller"
    assert response.headers["x-os-provider"] == "venice"


@pytest.mark.parametrize(
    ("path", "request_kwargs"),
    [
        (
            "/v1/audio/transcriptions",
            {
                "data": {
                    "model": "nvidia/parakeet-tdt-0.6b-v3",
                    "response_format": "text",
                    "prompt": "oversized-response",
                },
                "files": {"file": ("audio.wav", b"RIFF-audio", "audio/wav")},
            },
        ),
        (
            "/v1/augment/search",
            {"json": {"query": "oversized-response"}},
        ),
        (
            "/v1/image/generate",
            {"json": {"model": "chroma", "prompt": "oversized response"}},
        ),
    ],
)
def test_compat_json_responses_are_bounded_and_closed(
    routed_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    request_kwargs: dict[str, object],
) -> None:
    monkeypatch.setenv("MAX_UPSTREAM_JSON_RESPONSE_BYTES", "32")

    response = routed_client.post(path, **request_kwargs)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_response_too_large"
    assert response.headers["x-os-provider"] == "venice"
    assert "xxxxxxxx" not in response.text
    assert routed_client.app.state.test_response_streams[-1].closed is True


def test_image_rejects_oversized_body_before_parsing(
    routed_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_IMAGE_GENERATE_REQUEST_BYTES", "32")

    response = routed_client.post(
        "/v1/image/generate",
        json={"prompt": "x" * 100},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


def test_image_generation_rejects_binary_response_mode(routed_client: TestClient) -> None:
    response = routed_client.post(
        "/v1/image/generate",
        json={
            "model": "chroma",
            "prompt": "a red bicycle",
            "return_binary": True,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_image_generation_bounds_upstream_timeout(routed_client: TestClient) -> None:
    response = routed_client.post(
        "/v1/image/generate",
        json={"model": "chroma", "prompt": "force timeout"},
    )

    assert response.status_code == 502
    assert response.json()["error"]["message"] == "Image provider failed"


def test_image_generation_bounds_total_elapsed_time(
    routed_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_GENERATE_TIMEOUT_SECONDS", "0.001")

    response = routed_client.post(
        "/v1/image/generate",
        json={"model": "chroma", "prompt": "force total timeout"},
    )

    assert response.status_code == 502
    assert response.json()["error"]["message"] == "Image provider failed"


def test_image_generation_timeout_covers_streamed_response_body(
    routed_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_GENERATE_TIMEOUT_SECONDS", "0.001")

    response = routed_client.post(
        "/v1/image/generate",
        json={"model": "chroma", "prompt": "force body timeout"},
    )

    assert response.status_code == 502
    assert response.json()["error"]["message"] == "Image provider failed"


def test_image_generation_preserves_plaintext_rejection_status(
    routed_client: TestClient,
) -> None:
    response = routed_client.post(
        "/v1/image/generate",
        json={"model": "chroma", "prompt": "reject as text"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "image_generation_rejected"
    assert response.headers["x-os-billing-owner"] == "caller"


def test_image_generation_rejects_plaintext_provider_failure(
    routed_client: TestClient,
) -> None:
    response = routed_client.post(
        "/v1/image/generate",
        json={"model": "chroma", "prompt": "fail as text"},
    )

    assert response.status_code == 502
    assert response.json()["error"]["message"] == "Image provider returned invalid JSON"
    assert response.headers["x-os-billing-owner"] == "caller"
    assert response.headers["x-os-provider"] == "venice"


def test_video_quote_matches_venice_upstream_contract(routed_client: TestClient) -> None:
    assert routed_client.app.state.video_queue_semaphore._value == 4
    response = routed_client.post(
        "/v1/video/quote",
        json={
            "model": "wan-2.2-a14b-text-to-video",
            "duration": "5s",
            "resolution": "720p",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"quote": 0.42}
    assert response.headers["x-os-billing-owner"] == "caller"
    assert response.headers["x-os-provider"] == "venice"


def test_video_queue_forwards_text_and_animation_requests(routed_client: TestClient) -> None:
    text = routed_client.post(
        "/v1/video/queue",
        json={
            "model": "wan-2.2-a14b-text-to-video",
            "prompt": "waves on a moonlit shore",
            "duration": "5s",
        },
    )
    animation = routed_client.post(
        "/v1/video/queue",
        json={
            "model": "wan-2.6-image-to-video",
            "prompt": "the scene comes alive",
            "duration": "5s",
            "image_url": "data:image/png;base64,aW1hZ2U=",
        },
    )

    assert text.status_code == 200
    assert text.json()["queue_id"] == "video-job-1"
    assert animation.status_code == 200
    assert animation.json()["queue_id"] == "video-job-1"


def test_video_retrieve_relays_json_progress(routed_client: TestClient) -> None:
    response = routed_client.post(
        "/v1/video/retrieve",
        json={"model": "wan-2.2-a14b-text-to-video", "queue_id": "processing"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "PROCESSING"
    assert response.headers["x-os-billing-owner"] == "caller"


def test_video_retrieve_streams_completed_mp4(routed_client: TestClient) -> None:
    response = routed_client.post(
        "/v1/video/retrieve",
        json={"model": "wan-2.2-a14b-text-to-video", "queue_id": "completed"},
    )

    assert response.status_code == 200
    assert response.content == b"video-bytes"
    assert response.headers["content-type"].startswith("video/mp4")
    assert response.headers["x-os-provider"] == "venice"


def test_video_retrieve_preserves_plaintext_not_found(routed_client: TestClient) -> None:
    response = routed_client.post(
        "/v1/video/retrieve",
        json={"model": "wan-2.2-a14b-text-to-video", "queue_id": "expired"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "video_provider_error"


def test_video_rejects_oversized_request_before_parsing(
    routed_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_VIDEO_QUEUE_REQUEST_BYTES", "32")

    response = routed_client.post(
        "/v1/video/queue",
        json={"prompt": "x" * 100},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


def test_video_queue_allows_animation_payload_above_small_json_cap(
    routed_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_VIDEO_REQUEST_BYTES", "1048576")
    encoded_image = "a" * 1_100_000

    queue = routed_client.post(
        "/v1/video/queue",
        json={
            "model": "wan-2.6-image-to-video",
            "prompt": "animate",
            "duration": "5s",
            "image_url": f"data:image/png;base64,{encoded_image}",
        },
    )
    quote = routed_client.post(
        "/v1/video/quote",
        json={
            "model": "wan-2.2-a14b-text-to-video",
            "duration": "5s",
            "padding": encoded_image,
        },
    )

    assert queue.status_code == 200
    assert quote.status_code == 413


def test_video_queue_saturation_times_out_before_reading_or_forwarding(
    routed_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDEO_REQUEST_TIMEOUT_SECONDS", "0.01")
    routed_client.app.state.video_queue_semaphore = asyncio.Semaphore(0)
    calls_before = list(routed_client.app.state.test_calls)

    response = routed_client.post(
        "/v1/video/queue",
        json={
            "model": "wan-2.2-a14b-text-to-video",
            "prompt": "never forwarded",
            "duration": "5s",
        },
    )

    assert response.status_code == 502
    assert response.headers["x-os-billing-owner"] == "caller"
    assert routed_client.app.state.test_calls == calls_before


def test_video_rejects_oversized_response_before_streaming(
    routed_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_VIDEO_RESPONSE_BYTES", "100")

    response = routed_client.post(
        "/v1/video/retrieve",
        json={"model": "wan-2.2-a14b-text-to-video", "queue_id": "oversized"},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "video_too_large"


def test_video_rejects_unexpected_success_content_type(routed_client: TestClient) -> None:
    response = routed_client.post(
        "/v1/video/retrieve",
        json={
            "model": "wan-2.2-a14b-text-to-video",
            "queue_id": "invalid-content-type",
        },
    )

    assert response.status_code == 502
    assert response.headers["content-type"].startswith("application/json")
    assert "script" not in response.text


def test_video_caps_json_provider_responses(
    routed_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_VIDEO_JSON_RESPONSE_BYTES", "100")

    response = routed_client.post(
        "/v1/video/quote",
        json={"model": "oversized-json", "duration": "5s"},
    )

    assert response.status_code == 502
    assert response.json()["error"]["message"] == "Video provider response is too large"


def test_video_retrieve_uses_one_deadline_for_headers_and_json_body(
    routed_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDEO_REQUEST_TIMEOUT_SECONDS", "0.03")

    response = routed_client.post(
        "/v1/video/retrieve",
        json={"model": "wan-2.2-a14b-text-to-video", "queue_id": "slow-json"},
    )

    assert response.status_code == 502
    assert response.json()["error"]["message"] == "Video provider failed"


async def test_video_stream_caps_chunked_body_and_closes_upstream() -> None:
    upstream = httpx.Response(200, content=b"oversized-video")
    deadline = asyncio.get_running_loop().time() + 1

    with pytest.raises(ValueError, match="Video response is too large"):
        _ = [chunk async for chunk in _stream_video_response(upstream, 4, deadline)]

    assert upstream.is_closed


def test_endpoint_cost_accounts_for_cache_prices(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        CONFIG.replace(
            "output_per_million_usd: 0.15",
            "output_per_million_usd: 0.15\n      cache_read_per_million_usd: 0.01",
            1,
        )
    )
    endpoint = GatewayConfig.load(path).endpoints["phala-gpt-oss-20b"]

    cost = _actual_cost(
        endpoint,
        {
            "prompt_tokens": 1_000_000,
            "cached_tokens": 500_000,
            "completion_tokens": 1_000_000,
        },
    )

    assert cost == 175_000  # $0.02 uncached + $0.005 cache read + $0.15 output


async def test_venice_e2ee_key_is_bound_to_fresh_attestation() -> None:
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")
    endpoint = config.endpoints["venice-gpt-oss-20b-e2ee"]
    public_key = "04" + "ab" * 64

    def attestation(request: httpx.Request) -> httpx.Response:
        nonce = request.url.params["nonce"]
        return httpx.Response(
            200,
            json={"verified": True, "nonce": nonce, "signing_key": public_key[2:]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(attestation)) as http:
        assert await _validate_client_encryption_key(
            http,
            "https://venice.test/api",
            "secret",
            endpoint,
            {"x-venice-tee-model-pub-key": public_key},
        )


async def test_venice_e2ee_key_binding_fails_closed_on_nonce_mismatch() -> None:
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")
    endpoint = config.endpoints["venice-gpt-oss-20b-e2ee"]

    def stale_attestation(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"verified": True, "nonce": "stale", "signing_key": "ab" * 64},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(stale_attestation)) as http:
        assert not await _validate_client_encryption_key(
            http,
            "https://venice.test/api",
            "secret",
            endpoint,
            {"x-venice-tee-model-pub-key": "04" + "ab" * 64},
        )


async def test_venice_e2ee_key_binding_caps_and_closes_attestation_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")
    endpoint = config.endpoints["venice-gpt-oss-20b-e2ee"]
    public_key = "ab" * 64
    monkeypatch.setenv("MAX_UPSTREAM_JSON_RESPONSE_BYTES", "32")
    stream = TrackingAsyncByteStream(
        json.dumps(
            {
                "verified": True,
                "nonce": "will-be-replaced",
                "signing_key": public_key,
            }
        ).encode()
    )

    def attestation(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(attestation)) as http:
        assert not await _validate_client_encryption_key(
            http,
            "https://venice.test/api",
            "secret",
            endpoint,
            {"x-venice-tee-model-pub-key": "04" + public_key},
        )

    assert stream.closed is True


async def test_venice_e2ee_key_binding_fails_closed_on_non_object_json() -> None:
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")
    endpoint = config.endpoints["venice-gpt-oss-20b-e2ee"]

    def attestation(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(attestation)) as http:
        assert not await _validate_client_encryption_key(
            http,
            "https://venice.test/api",
            "secret",
            endpoint,
            {"x-venice-tee-model-pub-key": "04" + "ab" * 64},
        )
