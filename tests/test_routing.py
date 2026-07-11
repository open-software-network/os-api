from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient

from sovereign_gateway.app import (
    _actual_cost,
    _validate_client_encryption_key,
    create_app,
)
from sovereign_gateway.config import GatewayConfig
from sovereign_gateway.routing import (
    EndpointHealth,
    RoutingError,
    parse_routing_policy,
    route_endpoints,
)

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
    capabilities: [tools, structured_outputs, reasoning]
  backup/private-model:
    max_context_length: 32768
endpoints:
  phala-gpt-oss-20b:
    model: openai/gpt-oss-20b
    provider: phala
    upstream_model: phala/gpt-oss-20b
    priority: 10
    capabilities: [tools, structured_outputs, reasoning]
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
    capabilities: [tools, structured_outputs, reasoning]
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
      input_per_million_usd: 0.10
      output_per_million_usd: 0.20
"""


@pytest.fixture
def routed_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    config = tmp_path / "providers.yaml"
    config.write_text(CONFIG)
    monkeypatch.setenv("MODEL_CONFIG_PATH", str(config))
    monkeypatch.setenv("API_KEY_HASHES", f"router={hashlib.sha256(API_KEY.encode()).hexdigest()}")
    monkeypatch.setenv("PHALA_API_KEY", "phala-secret")
    monkeypatch.setenv("VENICE_API_KEY", "venice-secret")
    monkeypatch.delenv("REDIS_URL", raising=False)
    calls: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        payload = json.loads(request.content)
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
    model = next(item for item in models if item["id"] == "openai/gpt-oss-20b")
    assert model["providers"] == ["phala", "venice"]
    assert model["privacy_levels"] == ["tee"]

    endpoints = routed_client.get("/v1/models/openai/gpt-oss-20b/endpoints")
    assert endpoints.status_code == 200
    assert len(endpoints.json()["data"]) == 2


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
