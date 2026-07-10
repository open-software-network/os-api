from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class Backend:
    name: str
    base_url: str
    api_key_env: str | None = None
    privacy: str = "standard"
    revision: str | None = None

    @property
    def api_key(self) -> str | None:
        return os.getenv(self.api_key_env) if self.api_key_env else None


@dataclass(frozen=True)
class ModelRoute:
    id: str
    upstream_model: str
    standard_backend: str | None
    confidential_backend: str | None
    owned_by: str = "self-hosted"
    max_context_length: int | None = None
    input_price_microusd_per_million: int = 100_000
    output_price_microusd_per_million: int = 200_000

    @property
    def privacy_modes(self) -> list[str]:
        modes: list[str] = []
        if self.standard_backend:
            modes.append("standard")
        if self.confidential_backend:
            modes.append("confidential")
        return modes


@dataclass(frozen=True)
class GatewayConfig:
    backends: dict[str, Backend]
    models: dict[str, ModelRoute]

    @classmethod
    def load(cls, path: str | Path) -> GatewayConfig:
        config_path = Path(path)
        try:
            raw = yaml.safe_load(config_path.read_text()) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigurationError(f"cannot load model configuration: {exc}") from exc

        backend_items = _require_mapping(raw, "backends")
        model_items = _require_mapping(raw, "models")

        backends: dict[str, Backend] = {}
        for name, item in backend_items.items():
            if not isinstance(item, dict) or not item.get("base_url"):
                raise ConfigurationError(f"backend {name!r} requires base_url")
            privacy = item.get("privacy", "standard")
            if privacy not in {"standard", "confidential"}:
                raise ConfigurationError(f"backend {name!r} has invalid privacy mode")
            revision = item.get("revision")
            if revision is not None and not isinstance(revision, str):
                raise ConfigurationError(f"backend {name!r} revision must be a string")
            backends[name] = Backend(
                name=name,
                base_url=str(item["base_url"]).rstrip("/"),
                api_key_env=item.get("api_key_env"),
                privacy=privacy,
                revision=revision,
            )

        models: dict[str, ModelRoute] = {}
        for model_id, item in model_items.items():
            if not isinstance(item, dict):
                raise ConfigurationError(f"model {model_id!r} must be an object")
            standard = item.get("standard_backend")
            confidential = item.get("confidential_backend")
            if not standard and not confidential:
                raise ConfigurationError(f"model {model_id!r} has no backend")
            for backend_name in (standard, confidential):
                if backend_name and backend_name not in backends:
                    raise ConfigurationError(
                        f"model {model_id!r} references unknown backend {backend_name!r}"
                    )
            if standard and backends[standard].privacy != "standard":
                raise ConfigurationError(f"model {model_id!r} standard backend is not standard")
            if confidential and backends[confidential].privacy != "confidential":
                raise ConfigurationError(
                    f"model {model_id!r} confidential backend is not confidential"
                )
            models[model_id] = ModelRoute(
                id=model_id,
                upstream_model=item.get("upstream_model", model_id),
                standard_backend=standard,
                confidential_backend=confidential,
                owned_by=item.get("owned_by", "self-hosted"),
                max_context_length=item.get("max_context_length"),
                input_price_microusd_per_million=int(
                    item.get("input_price_microusd_per_million", 100_000)
                ),
                output_price_microusd_per_million=int(
                    item.get("output_price_microusd_per_million", 200_000)
                ),
            )
        return cls(backends=backends, models=models)


def _require_mapping(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict) or not value:
        raise ConfigurationError(f"{key!r} must be a non-empty mapping")
    return value
