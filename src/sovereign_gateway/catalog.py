from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any

import httpx

from .config import Endpoint, EndpointPrivacy, GatewayConfig, ModelRoute


class CatalogRefreshError(RuntimeError):
    pass


class ProviderCatalog:
    """Live Venice/Phala catalog merged into the router with stale-on-error caching."""

    def __init__(self, config: GatewayConfig, http: httpx.AsyncClient, ttl_seconds: int = 300):
        self.config = config
        self.http = http
        self.ttl_seconds = ttl_seconds
        self._expires_at = 0.0
        self._last_attempt = 0.0
        self._lock = asyncio.Lock()
        self._dynamic_models: dict[str, set[str]] = {}
        self._dynamic_endpoints: dict[str, set[str]] = {}

    async def refresh(self, *, force: bool = False) -> bool:
        now = time.monotonic()
        if (not force and now < self._expires_at) or (force and now - self._last_attempt < 10):
            return False
        async with self._lock:
            now = time.monotonic()
            if (not force and now < self._expires_at) or (force and now - self._last_attempt < 10):
                return False
            self._last_attempt = now
            providers = [
                provider
                for provider in self.config.providers.values()
                if provider.enabled and provider.adapter in {"venice", "phala"}
            ]
            if not providers:
                self._expires_at = time.monotonic() + self.ttl_seconds
                return False
            results = await asyncio.gather(
                *(self._fetch(provider.id) for provider in providers),
                return_exceptions=True,
            )
            successes = 0
            errors: list[str] = []
            for provider, result in zip(providers, results, strict=True):
                if isinstance(result, BaseException):
                    errors.append(f"{provider.id}: {result}")
                    continue
                models, endpoints = result
                self._replace_provider(provider.id, models, endpoints)
                successes += 1
            if successes == 0:
                self._expires_at = time.monotonic() + 10
                raise CatalogRefreshError("; ".join(errors) or "no provider catalogs available")
            self._expires_at = time.monotonic() + self.ttl_seconds
            return True

    async def _fetch(self, provider_id: str) -> tuple[dict[str, ModelRoute], dict[str, Endpoint]]:
        provider = self.config.providers[provider_id]
        headers = {"accept": "application/json"}
        if provider.api_key:
            headers["authorization"] = f"Bearer {provider.api_key}"
        response = await self.http.get(
            f"{provider.base_url}/v1/models", headers=headers, timeout=20
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not data:
            raise CatalogRefreshError("provider returned an empty or invalid catalog")
        if provider.adapter == "venice":
            return self._parse_venice(provider_id, data)
        return self._parse_phala(provider_id, data)

    def _parse_venice(
        self, provider_id: str, data: list[Any]
    ) -> tuple[dict[str, ModelRoute], dict[str, Endpoint]]:
        models: dict[str, ModelRoute] = {}
        endpoints: dict[str, Endpoint] = {}
        for raw in data:
            if not isinstance(raw, dict) or raw.get("type") != "text":
                continue
            model_id = str(raw.get("id", ""))
            if not model_id or model_id in self._curated_model_ids():
                continue
            spec = raw.get("model_spec")
            pricing = spec.get("pricing") if isinstance(spec, dict) else None
            if not isinstance(pricing, dict):
                raise CatalogRefreshError(f"{model_id}: missing pricing")
            input_price = _positive_price(pricing, "input", model_id)
            output_price = _positive_price(pricing, "output", model_id)
            capabilities_raw = spec.get("capabilities", {})
            capabilities = _venice_capabilities(capabilities_raw)
            privacy = str(spec.get("privacy", "anonymized"))
            endpoint = Endpoint(
                id=_endpoint_id(provider_id, model_id),
                model_id=model_id,
                provider=provider_id,
                upstream_model=model_id,
                privacy=EndpointPrivacy(
                    level="no-retention" if privacy == "private" else "standard",
                    execution="standard",
                    retention="zero" if privacy == "private" else "anonymized",
                    attestation="none",
                    transport="tls",
                ),
                input_price_microusd_per_million=input_price,
                output_price_microusd_per_million=output_price,
                cache_read_price_microusd_per_million=_optional_price(
                    pricing, "cache_input", model_id
                ),
                priority=100,
                capabilities=capabilities,
                base_url=self.config.providers[provider_id].base_url,
                api_key_env=self.config.providers[provider_id].api_key_env,
            )
            input_modalities = (
                ("text", "image") if capabilities_raw.get("supportsVision") else ("text",)
            )
            models[model_id] = ModelRoute(
                id=model_id,
                endpoint_ids=(endpoint.id,),
                owned_by=str(raw.get("owned_by", "venice.ai")),
                max_context_length=_optional_int(
                    spec.get("availableContextTokens") or raw.get("context_length")
                ),
                max_output_length=_optional_int(spec.get("maxCompletionTokens")),
                capabilities=capabilities,
                input_modalities=input_modalities,
                created=_optional_int(raw.get("created")),
                name=str(spec.get("name", model_id)),
                description=_optional_string(spec.get("description")),
                traits=tuple(str(item) for item in spec.get("traits", []) if isinstance(item, str)),
                provider_privacy=privacy,
                input_price_microusd_per_million=input_price,
                output_price_microusd_per_million=output_price,
                upstream_model=model_id,
            )
            endpoints[endpoint.id] = endpoint
        if not models:
            raise CatalogRefreshError("Venice returned no usable text models")
        return models, endpoints

    def _parse_phala(
        self, provider_id: str, data: list[Any]
    ) -> tuple[dict[str, ModelRoute], dict[str, Endpoint]]:
        models: dict[str, ModelRoute] = {}
        endpoints: dict[str, Endpoint] = {}
        for raw in data:
            if not isinstance(raw, dict) or raw.get("is_tee") is not True:
                continue
            model_id = str(raw.get("id", ""))
            if not model_id or model_id in self._curated_model_ids():
                continue
            pricing = raw.get("pricing")
            if not isinstance(pricing, dict):
                raise CatalogRefreshError(f"{model_id}: missing pricing")
            input_price = _positive_per_token_price(pricing, "prompt", model_id)
            output_price = _positive_per_token_price(pricing, "completion", model_id)
            capabilities = _parameter_capabilities(raw.get("supported_parameters", []))
            endpoint = Endpoint(
                id=_endpoint_id(provider_id, model_id),
                model_id=model_id,
                provider=provider_id,
                upstream_model=model_id,
                privacy=EndpointPrivacy(
                    level="tee",
                    execution="gpu-tee",
                    retention="zero",
                    attestation="phala-aci-receipt",
                    transport="tee-terminated-tls",
                ),
                input_price_microusd_per_million=input_price,
                output_price_microusd_per_million=output_price,
                cache_read_price_microusd_per_million=_optional_per_token_price(
                    pricing, "input_cache_read", model_id
                ),
                priority=10,
                capabilities=capabilities,
                base_url=self.config.providers[provider_id].base_url,
                api_key_env=self.config.providers[provider_id].api_key_env,
            )
            models[model_id] = ModelRoute(
                id=model_id,
                endpoint_ids=(endpoint.id,),
                owned_by=model_id.split("/", 1)[0],
                max_context_length=_optional_int(raw.get("context_length")),
                max_output_length=_optional_int(raw.get("max_output_length")),
                capabilities=capabilities,
                input_modalities=_string_tuple(raw.get("input_modalities"), ("text",)),
                output_modalities=_string_tuple(raw.get("output_modalities"), ("text",)),
                created=_optional_int(raw.get("created")),
                name=str(raw.get("name", model_id)),
                description=_optional_string(raw.get("description")),
                provider_privacy="private",
                input_price_microusd_per_million=input_price,
                output_price_microusd_per_million=output_price,
                upstream_model=model_id,
            )
            endpoints[endpoint.id] = endpoint
        if not models:
            raise CatalogRefreshError("Phala returned no new usable TEE models")
        return models, endpoints

    def _curated_model_ids(self) -> set[str]:
        dynamic = set().union(*self._dynamic_models.values()) if self._dynamic_models else set()
        return set(self.config.models) - dynamic

    def _replace_provider(
        self, provider_id: str, models: dict[str, ModelRoute], endpoints: dict[str, Endpoint]
    ) -> None:
        for model_id in self._dynamic_models.get(provider_id, set()):
            self.config.models.pop(model_id, None)
        for endpoint_id in self._dynamic_endpoints.get(provider_id, set()):
            self.config.endpoints.pop(endpoint_id, None)
            self.config.backends.pop(endpoint_id, None)
        self.config.models.update(models)
        self.config.endpoints.update(endpoints)
        self.config.backends.update(endpoints)
        self._dynamic_models[provider_id] = set(models)
        self._dynamic_endpoints[provider_id] = set(endpoints)


def _endpoint_id(provider: str, model_id: str) -> str:
    return f"{provider}-live-{hashlib.sha256(model_id.encode()).hexdigest()[:12]}"


def _positive_price(pricing: dict[str, Any], key: str, model_id: str) -> int:
    raw = pricing.get(key)
    value = raw.get("usd") if isinstance(raw, dict) else None
    try:
        price = round(float(value) * 1_000_000)
    except (TypeError, ValueError) as exc:
        raise CatalogRefreshError(f"{model_id}: invalid {key} price") from exc
    if price <= 0:
        raise CatalogRefreshError(f"{model_id}: invalid {key} price")
    return price


def _optional_price(pricing: dict[str, Any], key: str, model_id: str) -> int:
    return _positive_price(pricing, key, model_id) if key in pricing else 0


def _positive_per_token_price(pricing: dict[str, Any], key: str, model_id: str) -> int:
    try:
        price = round(float(pricing[key]) * 1_000_000 * 1_000_000)
    except (KeyError, TypeError, ValueError) as exc:
        raise CatalogRefreshError(f"{model_id}: invalid {key} price") from exc
    if price <= 0:
        raise CatalogRefreshError(f"{model_id}: invalid {key} price")
    return price


def _optional_per_token_price(pricing: dict[str, Any], key: str, model_id: str) -> int:
    return _positive_per_token_price(pricing, key, model_id) if key in pricing else 0


def _venice_capabilities(raw: Any) -> frozenset[str]:
    if not isinstance(raw, dict):
        return frozenset()
    result = set()
    if raw.get("supportsVision"):
        result.add("vision")
    if raw.get("supportsFunctionCalling"):
        result.update({"tools", "tool_choice"})
    if raw.get("supportsResponseSchema"):
        result.add("structured_outputs")
    if raw.get("supportsReasoning"):
        result.add("reasoning")
    return frozenset(result)


def _parameter_capabilities(raw: Any) -> frozenset[str]:
    values = set(raw) if isinstance(raw, list) else set()
    result = set()
    if "tools" in values:
        result.add("tools")
    if "tool_choice" in values:
        result.add("tool_choice")
    if {"response_format", "structured_outputs"} & values:
        result.add("structured_outputs")
    if {"reasoning", "reasoning_effort", "include_reasoning"} & values:
        result.add("reasoning")
    return frozenset(result)


def _optional_int(raw: Any) -> int | None:
    return int(raw) if isinstance(raw, (int, float)) and raw > 0 else None


def _optional_string(raw: Any) -> str | None:
    return str(raw) if raw is not None else None


def _string_tuple(raw: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        return default
    return tuple(raw)
