from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from sovereign_gateway.catalog import CatalogRefreshError, ProviderCatalog
from sovereign_gateway.config import GatewayConfig

CONFIG = """
providers:
  phala:
    base_url: https://phala.test
    api_key_env: PHALA_API_KEY
    adapter: phala
  venice:
    base_url: https://venice.test/api
    api_key_env: VENICE_API_KEY
    adapter: venice
models:
  reviewed/auto-model:
    quality_score: 90
endpoints:
  reviewed:
    model: reviewed/auto-model
    provider: phala
    pricing:
      input_per_million_usd: 1
      output_per_million_usd: 2
"""


def _venice_model(model_id: str, *, priced: bool = True) -> dict[str, object]:
    pricing = {"input": {"usd": 1.4}, "output": {"usd": 4.4}} if priced else {}
    return {
        "id": model_id,
        "type": "text",
        "owned_by": "venice.ai",
        "context_length": 100000,
        "model_spec": {
            "name": "Live Venice Model",
            "description": "Fetched from Venice",
            "privacy": "private",
            "pricing": pricing,
            "availableContextTokens": 100000,
            "maxCompletionTokens": 32000,
            "capabilities": {"supportsFunctionCalling": True},
            "traits": [],
        },
    }


def _phala_model(model_id: str) -> dict[str, object]:
    return {
        "id": model_id,
        "name": "Live Phala Model",
        "is_tee": True,
        "context_length": 64000,
        "max_output_length": 8000,
        "pricing": {"prompt": "0.0000002", "completion": "0.0000009"},
        "supported_parameters": ["tools", "tool_choice"],
        "input_modalities": ["text"],
        "output_modalities": ["text"],
    }


def _config(tmp_path: Path) -> GatewayConfig:
    path = tmp_path / "models.yaml"
    path.write_text(CONFIG)
    return GatewayConfig.load(path)


@pytest.mark.anyio
async def test_live_provider_catalog_adds_native_models_and_exact_prices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VENICE_API_KEY", "venice-secret")
    monkeypatch.setenv("PHALA_API_KEY", "phala-secret")

    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"].endswith("-secret")
        if request.url.host == "venice.test":
            return httpx.Response(200, json={"data": [_venice_model("venice-native-id")]})
        return httpx.Response(200, json={"data": [_phala_model("phala/native-id")]})

    config = _config(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        catalog = ProviderCatalog(config, http)
        assert await catalog.refresh(force=True) is True

    assert set(config.models) == {
        "reviewed/auto-model",
        "venice-native-id",
        "phala/native-id",
    }
    venice = config.endpoints_for("venice-native-id")[0]
    phala = config.endpoints_for("phala/native-id")[0]
    assert venice.upstream_model == "venice-native-id"
    assert venice.input_price_microusd_per_million == 1_400_000
    assert venice.privacy.retention == "zero"
    assert phala.input_price_microusd_per_million == 200_000
    assert phala.privacy.level == "tee"
    assert config.models["venice-native-id"].quality_score is None


@pytest.mark.anyio
async def test_refresh_keeps_last_good_catalog_when_providers_fail(tmp_path: Path) -> None:
    failing = False

    def upstream(request: httpx.Request) -> httpx.Response:
        if failing:
            return httpx.Response(503, json={"error": "unavailable"})
        data = (
            [_venice_model("saved-venice-id")]
            if request.url.host == "venice.test"
            else [_phala_model("phala/saved-id")]
        )
        return httpx.Response(200, json={"data": data})

    config = _config(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        catalog = ProviderCatalog(config, http)
        await catalog.refresh(force=True)
        failing = True
        catalog._last_attempt = 0
        with pytest.raises(CatalogRefreshError):
            await catalog.refresh(force=True)

    assert "saved-venice-id" in config.models
    assert "phala/saved-id" in config.models


@pytest.mark.anyio
async def test_missing_live_price_never_creates_a_free_route(tmp_path: Path) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.host == "venice.test":
            return httpx.Response(
                200, json={"data": [_venice_model("unpriced-model", priced=False)]}
            )
        return httpx.Response(503, json={"error": "unavailable"})

    config = _config(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        catalog = ProviderCatalog(config, http)
        with pytest.raises(CatalogRefreshError):
            await catalog.refresh(force=True)

    assert "unpriced-model" not in config.models


@pytest.mark.anyio
async def test_empty_catalog_cannot_replace_last_good_models(tmp_path: Path) -> None:
    empty = False

    def upstream(request: httpx.Request) -> httpx.Response:
        if empty:
            return httpx.Response(200, content=json.dumps({"data": []}))
        data = (
            [_venice_model("kept-venice-id")]
            if request.url.host == "venice.test"
            else [_phala_model("phala/kept-id")]
        )
        return httpx.Response(200, json={"data": data})

    config = _config(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        catalog = ProviderCatalog(config, http)
        await catalog.refresh(force=True)
        empty = True
        catalog._last_attempt = 0
        with pytest.raises(CatalogRefreshError):
            await catalog.refresh(force=True)

    assert "kept-venice-id" in config.models
    assert "phala/kept-id" in config.models
