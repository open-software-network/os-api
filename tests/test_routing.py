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
    select_auto_models,
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
    monkeypatch.delenv("REDIS_URL", raising=False)
    calls: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith("/audio/transcriptions"):
            assert request.headers["authorization"] == "Bearer venice-secret"
            assert b'name="model"' in request.content
            assert b"nvidia/parakeet-tdt-0.6b-v3" in request.content
            assert b'filename="audio.wav"' in request.content
            assert b"private-name.wav" not in request.content
            return httpx.Response(200, json={"text": "hello", "language": "en"})
        if "/augment/" in request.url.path:
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


def test_preferred_privacy_appends_zero_retention_venice_fallback() -> None:
    config = GatewayConfig.load(Path(__file__).parents[1] / "config/models.yaml")
    payload = {"model": "openai/gpt-oss-120b", "privacy": {"require_zero_retention": True}}
    preferred = parse_routing_policy(payload, None)
    required = parse_routing_policy(payload, "required")

    preferred_endpoints = route_endpoints(
        config, "openai/gpt-oss-120b", preferred, EndpointHealth()
    )
    required_endpoints = route_endpoints(config, "openai/gpt-oss-120b", required, EndpointHealth())

    assert [endpoint.id for endpoint in preferred_endpoints] == [
        "phala-gpt-oss-120b",
        "venice-gpt-oss-120b-private",
    ]
    assert [endpoint.id for endpoint in required_endpoints] == ["phala-gpt-oss-120b"]


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
