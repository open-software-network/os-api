from __future__ import annotations

import asyncio
import math
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI

from .config import GatewayConfig
from .routing import EndpointHealth


@dataclass(frozen=True)
class RequestSample:
    recorded_at: float
    operation: str
    status_code: int
    duration_ms: float


class HealthMonitor:
    """Small, process-local telemetry window for the private health dashboard.

    Samples contain only an operation category, status, latency, and timestamp.
    Request URLs, identities, prompts, and responses are deliberately excluded.
    """

    def __init__(self, retention_seconds: int = 3_600, max_samples: int = 50_000) -> None:
        self.retention_seconds = retention_seconds
        self.samples: deque[RequestSample] = deque(maxlen=max_samples)
        self.started_monotonic = time.monotonic()
        self.started_at = datetime.now(UTC)

    def record(self, path: str, status_code: int, duration_ms: float) -> None:
        if not path.startswith("/v1/"):
            return
        now = time.monotonic()
        self.samples.append(
            RequestSample(now, _operation(path), status_code, round(duration_ms, 2))
        )
        self._prune(now)

    def snapshot(
        self,
        *,
        config: GatewayConfig,
        endpoint_health: EndpointHealth,
        readiness_ok: bool,
        components: dict[str, str],
        version: str,
    ) -> dict[str, Any]:
        now = time.monotonic()
        self._prune(now)
        samples = list(self.samples)
        recent = [sample for sample in samples if sample.recorded_at >= now - 300]
        hourly = [sample for sample in samples if sample.recorded_at >= now - 3_600]
        providers = _provider_health(config, endpoint_health)
        recent_stats = _request_stats(recent)
        hourly_stats = _request_stats(hourly)

        degraded = any(value == "degraded" for value in components.values())
        degraded = degraded or any(
            provider["status"] not in {"operational", "disabled"} for provider in providers
        )
        if recent_stats["requests"] >= 5 and recent_stats["error_rate"] >= 0.05:
            degraded = True
        if not readiness_ok:
            overall = "outage"
        elif degraded:
            overall = "degraded"
        else:
            overall = "operational"

        return {
            "status": overall,
            "checked_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "service": {
                "name": "os-api",
                "version": version,
                "region": os.getenv("GATEWAY_REGION", "local"),
                "started_at": self.started_at.isoformat().replace("+00:00", "Z"),
                "uptime_seconds": round(now - self.started_monotonic),
            },
            "readiness": {
                "status": "ready" if readiness_ok else "not_ready",
                "components": _display_components(config, components),
            },
            "requests": {
                "last_5_minutes": recent_stats,
                "last_hour": hourly_stats,
                "series": _minute_series(hourly, now, minutes=30),
            },
            "providers": providers,
        }

    def _prune(self, now: float) -> None:
        cutoff = now - self.retention_seconds
        while self.samples and self.samples[0].recorded_at < cutoff:
            self.samples.popleft()


async def check_dependencies(app: FastAPI) -> tuple[bool, dict[str, str]]:
    """Run the same dependency checks used by readiness and the dashboard."""

    components: dict[str, str] = {}
    required_ok = True
    config: GatewayConfig = app.state.config
    missing_provider_keys = sorted(
        provider.id
        for provider in config.providers.values()
        if provider.enabled and provider.api_key_env and not provider.api_key
    )
    if missing_provider_keys:
        # The private dashboard shows the affected provider separately, while
        # the public readiness response exposes only the aggregate state.
        components["provider_credentials"] = "unavailable"
        required_ok = False
    if app.state.managed_keys:
        try:
            await app.state.managed_keys.healthcheck()
        except Exception:
            components["firestore"] = "unavailable"
            required_ok = False
        else:
            components["firestore"] = "ready"
    if app.state.redis:
        try:
            await asyncio.wait_for(app.state.redis.ping(), timeout=2)
        except Exception:
            components["redis"] = "degraded"
        else:
            components["redis"] = "ready"
    return required_ok, components


def _operation(path: str) -> str:
    relative = path.removeprefix("/v1/")
    if relative.startswith("chat/completions"):
        return "chat"
    if relative.startswith("completions"):
        return "completions"
    if relative.startswith("embeddings"):
        return "embeddings"
    if relative.startswith("models"):
        return "models"
    if relative.startswith("audio/"):
        return "audio"
    if relative.startswith("image/"):
        return "images"
    if relative.startswith("attestation"):
        return "attestation"
    return "other"


def _request_stats(samples: list[RequestSample]) -> dict[str, Any]:
    durations = sorted(sample.duration_ms for sample in samples)
    failures = sum(sample.status_code >= 500 for sample in samples)
    operations: dict[str, int] = defaultdict(int)
    for sample in samples:
        operations[sample.operation] += 1
    return {
        "requests": len(samples),
        "errors": failures,
        "error_rate": round(failures / len(samples), 4) if samples else 0.0,
        "availability": round(1 - failures / len(samples), 4) if samples else None,
        "p95_latency_ms": _percentile(durations, 0.95),
        "operations": dict(sorted(operations.items())),
    }


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    index = max(0, math.ceil(len(values) * percentile) - 1)
    return round(values[index], 2)


def _minute_series(samples: list[RequestSample], now: float, minutes: int) -> list[dict[str, Any]]:
    buckets: list[list[RequestSample]] = [[] for _ in range(minutes)]
    for sample in samples:
        age_minutes = int((now - sample.recorded_at) // 60)
        if 0 <= age_minutes < minutes:
            buckets[minutes - age_minutes - 1].append(sample)
    points: list[dict[str, Any]] = []
    current_wall = datetime.now(UTC).timestamp()
    for index, bucket in enumerate(buckets):
        minutes_ago = minutes - index - 1
        points.append(
            {
                "at": datetime.fromtimestamp(
                    current_wall - minutes_ago * 60, UTC
                ).isoformat().replace("+00:00", "Z"),
                "requests": len(bucket),
                "errors": sum(sample.status_code >= 500 for sample in bucket),
                "average_latency_ms": (
                    round(sum(sample.duration_ms for sample in bucket) / len(bucket), 2)
                    if bucket
                    else None
                ),
            }
        )
    return points


def _display_components(
    config: GatewayConfig, components: dict[str, str]
) -> list[dict[str, str]]:
    result = [
        {
            "id": "gateway",
            "name": "Gateway",
            "status": "operational",
            "detail": "Process is responding",
        }
    ]
    configured = {
        "provider_credentials": ("Provider credentials", "Upstream authentication"),
        "firestore": ("Firestore", "Keys, spend limits, and audit data"),
        "redis": ("Redis", "Rate limits and admission control"),
    }
    for component_id, (name, detail) in configured.items():
        state = components.get(component_id)
        if state is None:
            if component_id == "provider_credentials" and not config.providers:
                continue
            if component_id in {"firestore", "redis"}:
                continue
            state = "ready"
        status = {
            "ready": "operational",
            "degraded": "degraded",
            "unavailable": "outage",
        }.get(state, "outage")
        result.append({"id": component_id, "name": name, "status": status, "detail": detail})
    return result


def _provider_health(config: GatewayConfig, health: EndpointHealth) -> list[dict[str, Any]]:
    providers: list[dict[str, Any]] = []
    for provider in config.providers.values():
        endpoints = []
        for endpoint in config.endpoints.values():
            if endpoint.provider != provider.id or not endpoint.enabled:
                continue
            stats = health.get(endpoint.id)
            if not stats.healthy:
                status = "outage"
            elif stats.attempts >= 5 and stats.success_rate < 0.95:
                status = "degraded"
            else:
                status = "operational"
            endpoints.append(
                {
                    "id": endpoint.id,
                    "model": endpoint.model_id,
                    "privacy": endpoint.privacy.level,
                    "status": status,
                    "attempts": stats.attempts,
                    "success_rate": round(stats.success_rate, 4) if stats.attempts else None,
                    "average_latency_ms": (
                        None if math.isinf(stats.latency) else round(stats.latency * 1_000, 2)
                    ),
                }
            )
        if not provider.enabled:
            status = "disabled"
        elif endpoints and all(endpoint["status"] == "outage" for endpoint in endpoints):
            status = "outage"
        elif any(endpoint["status"] != "operational" for endpoint in endpoints):
            status = "degraded"
        else:
            status = "operational"
        providers.append(
            {
                "id": provider.id,
                "name": provider.name,
                "status": status,
                "regions": list(provider.regions),
                "endpoints": endpoints,
            }
        )
    return providers
