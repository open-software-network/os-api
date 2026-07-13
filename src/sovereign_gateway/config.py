from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigurationError(ValueError):
    pass


PRIVACY_LEVELS = {
    "standard": 0,
    "no-retention": 1,
    "tee": 2,
    "verified-tee": 3,
    "signed-output": 4,
    "e2ee-tee": 5,
}
PROVIDER_ADAPTERS = {"openai", "phala", "venice"}


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    base_url: str
    api_key_env: str | None = None
    adapter: str = "openai"
    enabled: bool = True
    regions: tuple[str, ...] = ()

    @property
    def api_key(self) -> str | None:
        return os.getenv(self.api_key_env) if self.api_key_env else None


@dataclass(frozen=True)
class EndpointPrivacy:
    level: str = "standard"
    execution: str = "standard"
    retention: str = "unknown"
    attestation: str = "none"
    transport: str = "tls"

    @property
    def confidential(self) -> bool:
        return PRIVACY_LEVELS[self.level] >= PRIVACY_LEVELS["tee"]

    def public_dict(self) -> dict[str, str]:
        return {
            "level": self.level,
            "execution": self.execution,
            "retention": self.retention,
            "attestation": self.attestation,
            "transport": self.transport,
        }


@dataclass(frozen=True)
class Endpoint:
    id: str
    model_id: str
    provider: str
    upstream_model: str
    privacy: EndpointPrivacy
    input_price_microusd_per_million: int
    output_price_microusd_per_million: int
    cache_read_price_microusd_per_million: int = 0
    cache_write_price_microusd_per_million: int = 0
    revision: str | None = None
    max_concurrency: int | None = None
    priority: int = 100
    enabled: bool = True
    capabilities: frozenset[str] = frozenset()
    regions: tuple[str, ...] = ()
    base_url: str = ""
    api_key_env: str | None = None
    requires_client_encryption: bool = False

    @property
    def confidential(self) -> bool:
        return self.privacy.confidential

    @property
    def name(self) -> str:
        return self.id

    @property
    def api_key(self) -> str | None:
        return os.getenv(self.api_key_env) if self.api_key_env else None


@dataclass(frozen=True)
class ModelRoute:
    id: str
    endpoint_ids: tuple[str, ...]
    owned_by: str = "private-network"
    max_context_length: int | None = None
    max_output_length: int | None = None
    capabilities: frozenset[str] = frozenset()
    input_modalities: tuple[str, ...] = ("text",)
    output_modalities: tuple[str, ...] = ("text",)
    created: int | None = None
    quality_score: float | None = None
    name: str | None = None
    description: str | None = None
    traits: tuple[str, ...] = ()
    provider_privacy: str | None = None

    # Compatibility fields used by the existing web UI and tests. Pricing is
    # populated with the cheapest enabled endpoint for the model.
    input_price_microusd_per_million: int = 0
    output_price_microusd_per_million: int = 0
    upstream_model: str = ""
    standard_backend: str | None = None
    confidential_backend: str | None = None

    @property
    def privacy_modes(self) -> list[str]:
        # GatewayConfig enriches model serialization with endpoint data. This
        # fallback keeps old templates safe for manually constructed routes.
        return ["confidential"]


# Backwards-compatible name for callers that imported Backend. A backend is
# now a provider endpoint rather than a model-global target.
Backend = Endpoint


@dataclass(frozen=True)
class GatewayConfig:
    providers: dict[str, Provider]
    endpoints: dict[str, Endpoint]
    models: dict[str, ModelRoute]
    # Legacy lookup retained for the existing attestation endpoint and rollout
    # tooling. It maps endpoint IDs to endpoints.
    backends: dict[str, Endpoint] = field(default_factory=dict)

    def endpoints_for(self, model_id: str) -> list[Endpoint]:
        route = self.models[model_id]
        return [
            self.endpoints[endpoint_id]
            for endpoint_id in route.endpoint_ids
            if self.endpoints[endpoint_id].enabled
            and self.providers[self.endpoints[endpoint_id].provider].enabled
        ]

    def privacy_modes_for(self, model_id: str) -> list[str]:
        endpoints = self.endpoints_for(model_id)
        modes: list[str] = []
        if any(not endpoint.confidential for endpoint in endpoints):
            modes.append("standard")
        if any(endpoint.confidential for endpoint in endpoints):
            modes.append("confidential")
        return modes

    @classmethod
    def load(cls, path: str | Path) -> GatewayConfig:
        config_path = Path(path)
        try:
            raw = yaml.safe_load(config_path.read_text()) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigurationError(f"cannot load model configuration: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigurationError("configuration root must be an object")
        endpoint_schema = "endpoints" in raw or "providers" in raw
        allowed = (
            {"providers", "models", "endpoints", "catalog_includes"}
            if endpoint_schema
            else {"backends", "models"}
        )
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ConfigurationError(f"unknown configuration keys: {unknown}")
        raw = _merge_catalog_includes(raw, config_path.parent)
        if endpoint_schema:
            return _load_endpoint_config(raw)
        return _load_legacy_config(raw)


def _load_endpoint_config(raw: dict[str, Any]) -> GatewayConfig:
    provider_items = _require_mapping(raw, "providers")
    endpoint_items = _require_mapping(raw, "endpoints")
    model_items = _require_mapping(raw, "models")

    providers: dict[str, Provider] = {}
    for provider_id, item in provider_items.items():
        if not isinstance(item, dict) or not item.get("base_url"):
            raise ConfigurationError(f"provider {provider_id!r} requires base_url")
        providers[provider_id] = Provider(
            id=provider_id,
            name=str(item.get("name", provider_id)),
            base_url=str(item["base_url"]).rstrip("/"),
            api_key_env=item.get("api_key_env"),
            adapter=str(item.get("adapter", "openai")),
            enabled=bool(item.get("enabled", True)),
            regions=_string_tuple(item.get("regions", []), f"provider {provider_id} regions"),
        )
        if providers[provider_id].adapter not in PROVIDER_ADAPTERS:
            raise ConfigurationError(
                f"provider {provider_id!r} has unsupported adapter "
                f"{providers[provider_id].adapter!r}"
            )

    endpoints: dict[str, Endpoint] = {}
    model_endpoint_ids: dict[str, list[str]] = {str(model_id): [] for model_id in model_items}
    for endpoint_id, item in endpoint_items.items():
        if not isinstance(item, dict):
            raise ConfigurationError(f"endpoint {endpoint_id!r} must be an object")
        model_id = str(item.get("model", ""))
        provider_id = str(item.get("provider", ""))
        if model_id not in model_items:
            raise ConfigurationError(
                f"endpoint {endpoint_id!r} references unknown model {model_id!r}"
            )
        if provider_id not in providers:
            raise ConfigurationError(
                f"endpoint {endpoint_id!r} references unknown provider {provider_id!r}"
            )
        privacy = _parse_privacy(endpoint_id, item.get("privacy", {}))
        pricing = item.get("pricing", {})
        if not isinstance(pricing, dict):
            raise ConfigurationError(f"endpoint {endpoint_id!r} pricing must be an object")
        max_concurrency = item.get("max_concurrency")
        if max_concurrency is not None and (
            not isinstance(max_concurrency, int) or max_concurrency < 1
        ):
            raise ConfigurationError(
                f"endpoint {endpoint_id!r} max_concurrency must be a positive integer"
            )
        endpoint = Endpoint(
            id=endpoint_id,
            model_id=model_id,
            provider=provider_id,
            upstream_model=str(item.get("upstream_model", model_id)),
            privacy=privacy,
            input_price_microusd_per_million=_price(pricing, "input", endpoint_id),
            output_price_microusd_per_million=_price(pricing, "output", endpoint_id),
            cache_read_price_microusd_per_million=_price(
                pricing, "cache_read", endpoint_id, required=False
            ),
            cache_write_price_microusd_per_million=_price(
                pricing, "cache_write", endpoint_id, required=False
            ),
            revision=item.get("revision"),
            max_concurrency=max_concurrency,
            priority=int(item.get("priority", 100)),
            enabled=bool(item.get("enabled", True)),
            capabilities=frozenset(
                _string_tuple(item.get("capabilities", []), f"endpoint {endpoint_id} capabilities")
            ),
            regions=_string_tuple(item.get("regions", []), f"endpoint {endpoint_id} regions"),
            base_url=providers[provider_id].base_url,
            api_key_env=providers[provider_id].api_key_env,
            requires_client_encryption=bool(item.get("requires_client_encryption", False)),
        )
        if endpoint.privacy.level == "e2ee-tee" and not endpoint.requires_client_encryption:
            raise ConfigurationError(
                f"endpoint {endpoint_id!r} is e2ee-tee but does not require client encryption"
            )
        if endpoint.privacy.level in {"verified-tee", "signed-output"}:
            raise ConfigurationError(
                f"endpoint {endpoint_id!r} claims {endpoint.privacy.level} but no independent "
                "measurement-policy verifier is configured"
            )
        if endpoint.requires_client_encryption and endpoint.privacy.transport != "client-e2ee":
            raise ConfigurationError(
                f"endpoint {endpoint_id!r} requires client encryption "
                "but transport is not client-e2ee"
            )
        if PRIVACY_LEVELS[endpoint.privacy.level] >= PRIVACY_LEVELS["signed-output"] and (
            endpoint.privacy.attestation == "none"
        ):
            raise ConfigurationError(
                f"endpoint {endpoint_id!r} claims signed output without an evidence mechanism"
            )
        endpoints[endpoint_id] = endpoint
        model_endpoint_ids[model_id].append(endpoint_id)

    models: dict[str, ModelRoute] = {}
    for model_id, item in model_items.items():
        if not isinstance(item, dict):
            raise ConfigurationError(f"model {model_id!r} must be an object")
        endpoint_ids = tuple(item.get("endpoints", model_endpoint_ids[model_id]))
        if not endpoint_ids:
            raise ConfigurationError(f"model {model_id!r} has no endpoints")
        unknown = [endpoint_id for endpoint_id in endpoint_ids if endpoint_id not in endpoints]
        if unknown:
            raise ConfigurationError(f"model {model_id!r} references unknown endpoints {unknown}")
        prices = [
            endpoints[endpoint_id] for endpoint_id in endpoint_ids if endpoints[endpoint_id].enabled
        ]
        if not prices:
            raise ConfigurationError(f"model {model_id!r} has no enabled endpoints")
        models[model_id] = ModelRoute(
            id=model_id,
            endpoint_ids=endpoint_ids,
            owned_by=str(item.get("owned_by", "private-network")),
            max_context_length=_optional_positive_int(item.get("max_context_length"), model_id),
            max_output_length=_optional_positive_int(item.get("max_output_length"), model_id),
            capabilities=frozenset(
                _string_tuple(item.get("capabilities", []), f"model {model_id} capabilities")
            ),
            input_modalities=_string_tuple(
                item.get("input_modalities", ["text"]), f"model {model_id} input modalities"
            ),
            output_modalities=_string_tuple(
                item.get("output_modalities", ["text"]), f"model {model_id} output modalities"
            ),
            created=item.get("created"),
            quality_score=_optional_score(item.get("quality_score"), model_id),
            name=str(item["name"]) if item.get("name") is not None else None,
            description=(str(item["description"]) if item.get("description") is not None else None),
            traits=_string_tuple(item.get("traits", []), f"model {model_id} traits"),
            provider_privacy=(
                str(item["provider_privacy"]) if item.get("provider_privacy") is not None else None
            ),
            input_price_microusd_per_million=min(
                endpoint.input_price_microusd_per_million for endpoint in prices
            ),
            output_price_microusd_per_million=min(
                endpoint.output_price_microusd_per_million for endpoint in prices
            ),
            upstream_model=model_id,
        )
    return GatewayConfig(providers, endpoints, models, dict(endpoints))


def _merge_catalog_includes(raw: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    """Merge deterministic generated model/endpoint snapshots into the hand-curated catalog."""
    includes = raw.get("catalog_includes", [])
    if not isinstance(includes, list) or any(not isinstance(item, str) for item in includes):
        raise ConfigurationError("catalog_includes must be a list of paths")
    if not includes:
        return raw
    merged = dict(raw)
    merged_models = dict(_require_mapping(raw, "models"))
    merged_endpoints = dict(_require_mapping(raw, "endpoints"))
    for relative in includes:
        include_path = (base_dir / relative).resolve()
        try:
            included = yaml.safe_load(include_path.read_text()) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigurationError(f"cannot load catalog include {relative!r}: {exc}") from exc
        if not isinstance(included, dict):
            raise ConfigurationError(f"catalog include {relative!r} must be an object")
        for model_id, item in _require_mapping(included, "models").items():
            if model_id in merged_models:
                raise ConfigurationError(
                    f"catalog include {relative!r} duplicates model {model_id!r}"
                )
            merged_models[model_id] = item
        for endpoint_id, item in _require_mapping(included, "endpoints").items():
            if endpoint_id in merged_endpoints:
                raise ConfigurationError(
                    f"catalog include {relative!r} duplicates endpoint {endpoint_id!r}"
                )
            merged_endpoints[endpoint_id] = item
    merged["models"] = merged_models
    merged["endpoints"] = merged_endpoints
    return merged


def _load_legacy_config(raw: dict[str, Any]) -> GatewayConfig:
    backend_items = _require_mapping(raw, "backends")
    model_items = _require_mapping(raw, "models")
    providers: dict[str, Provider] = {}
    legacy: dict[str, dict[str, Any]] = {}
    for name, item in backend_items.items():
        if not isinstance(item, dict) or not item.get("base_url"):
            raise ConfigurationError(f"backend {name!r} requires base_url")
        privacy = str(item.get("privacy", "standard"))
        if privacy not in {"standard", "confidential"}:
            raise ConfigurationError(f"backend {name!r} has invalid privacy mode")
        max_concurrency = item.get("max_concurrency")
        if max_concurrency is not None and (
            not isinstance(max_concurrency, int) or max_concurrency < 1
        ):
            raise ConfigurationError(f"backend {name!r} max_concurrency must be a positive integer")
        revision = item.get("revision")
        if revision is not None and not isinstance(revision, str):
            raise ConfigurationError(f"backend {name!r} revision must be a string")
        providers[name] = Provider(
            id=name,
            name=name,
            base_url=str(item["base_url"]).rstrip("/"),
            api_key_env=item.get("api_key_env"),
            adapter="openai",
        )
        legacy[name] = {**item, "privacy": privacy}

    endpoints: dict[str, Endpoint] = {}
    models: dict[str, ModelRoute] = {}
    for model_id, item in model_items.items():
        if not isinstance(item, dict):
            raise ConfigurationError(f"model {model_id!r} must be an object")
        standard = item.get("standard_backend")
        confidential = item.get("confidential_backend")
        if not standard and not confidential:
            raise ConfigurationError(f"model {model_id!r} has no backend")
        if standard and standard not in legacy:
            raise ConfigurationError(f"model {model_id!r} references unknown backend {standard!r}")
        if confidential and confidential not in legacy:
            raise ConfigurationError(
                f"model {model_id!r} references unknown backend {confidential!r}"
            )
        if standard and legacy[standard]["privacy"] != "standard":
            raise ConfigurationError(f"model {model_id!r} standard backend is not standard")
        if confidential and legacy[confidential]["privacy"] != "confidential":
            raise ConfigurationError(f"model {model_id!r} confidential backend is not confidential")
        endpoint_ids: list[str] = []
        for backend_name, confidential_mode in ((standard, False), (confidential, True)):
            if not backend_name:
                continue
            source = legacy[backend_name]
            endpoint_id = f"{backend_name}:{model_id}"
            endpoints[endpoint_id] = Endpoint(
                id=endpoint_id,
                model_id=model_id,
                provider=backend_name,
                upstream_model=str(item.get("upstream_model", model_id)),
                privacy=EndpointPrivacy(
                    level="verified-tee" if confidential_mode else "standard",
                    execution="gpu-tee" if confidential_mode else "standard",
                    retention="zero" if confidential_mode else "unknown",
                    attestation="gateway" if confidential_mode else "none",
                ),
                input_price_microusd_per_million=int(
                    item.get("input_price_microusd_per_million", 100_000)
                ),
                output_price_microusd_per_million=int(
                    item.get("output_price_microusd_per_million", 200_000)
                ),
                revision=source.get("revision"),
                max_concurrency=source.get("max_concurrency"),
                priority=50 if confidential_mode else 100,
                base_url=providers[backend_name].base_url,
                api_key_env=providers[backend_name].api_key_env,
            )
            endpoint_ids.append(endpoint_id)
        models[model_id] = ModelRoute(
            id=model_id,
            endpoint_ids=tuple(endpoint_ids),
            owned_by=str(item.get("owned_by", "self-hosted")),
            max_context_length=item.get("max_context_length"),
            input_price_microusd_per_million=int(
                item.get("input_price_microusd_per_million", 100_000)
            ),
            output_price_microusd_per_million=int(
                item.get("output_price_microusd_per_million", 200_000)
            ),
            upstream_model=str(item.get("upstream_model", model_id)),
            standard_backend=f"{standard}:{model_id}" if standard else None,
            confidential_backend=f"{confidential}:{model_id}" if confidential else None,
        )
    return GatewayConfig(providers, endpoints, models, dict(endpoints))


def _parse_privacy(endpoint_id: str, raw: Any) -> EndpointPrivacy:
    if not isinstance(raw, dict):
        raise ConfigurationError(f"endpoint {endpoint_id!r} privacy must be an object")
    level = str(raw.get("level", "standard"))
    if level not in PRIVACY_LEVELS:
        raise ConfigurationError(f"endpoint {endpoint_id!r} has invalid privacy level {level!r}")
    return EndpointPrivacy(
        level=level,
        execution=str(raw.get("execution", "standard")),
        retention=str(raw.get("retention", "unknown")),
        attestation=str(raw.get("attestation", "none")),
        transport=str(raw.get("transport", "tls")),
    )


def _price(pricing: dict[str, Any], key: str, endpoint_id: str, required: bool = True) -> int:
    value = pricing.get(f"{key}_microusd_per_million")
    if value is None:
        usd = pricing.get(f"{key}_per_million_usd")
        value = round(float(usd) * 1_000_000) if usd is not None else None
    if value is None:
        if required:
            raise ConfigurationError(f"endpoint {endpoint_id!r} requires {key} pricing")
        return 0
    parsed = int(value)
    if parsed < 0:
        raise ConfigurationError(f"endpoint {endpoint_id!r} {key} price cannot be negative")
    return parsed


def _optional_positive_int(value: Any, model_id: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or value < 1:
        raise ConfigurationError(f"model {model_id!r} length limits must be positive integers")
    return value


def _optional_score(value: Any, model_id: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"model {model_id!r} quality_score must be a number")
    score = float(value)
    if not 0 <= score <= 100:
        raise ConfigurationError(f"model {model_id!r} quality_score must be between 0 and 100")
    return score


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ConfigurationError(f"{field_name} must be a list of strings")
    return tuple(value)


def _require_mapping(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict) or not value:
        raise ConfigurationError(f"{key!r} must be a non-empty mapping")
    return value
