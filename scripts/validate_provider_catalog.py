#!/usr/bin/env python3
"""Fail deployment when configured hosted endpoints drift from provider catalogs."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx

from sovereign_gateway.config import Endpoint, GatewayConfig


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/models.yaml")
    parser.add_argument("--timeout", type=float, default=20)
    args = parser.parse_args()
    config = GatewayConfig.load(Path(args.config))
    errors: list[str] = []
    async with httpx.AsyncClient(timeout=args.timeout) as client:
        catalogs = await asyncio.gather(
            _catalog(client, "https://inference.phala.com/v1/models"),
            _catalog(client, "https://api.venice.ai/api/v1/models"),
        )
    live = {"phala": catalogs[0], "venice": catalogs[1]}
    for endpoint in config.endpoints.values():
        if endpoint.provider not in live or not endpoint.enabled:
            continue
        model = live[endpoint.provider].get(endpoint.upstream_model)
        if not model:
            errors.append(
                f"{endpoint.id}: {endpoint.upstream_model} is absent from {endpoint.provider}"
            )
            continue
        if endpoint.provider == "phala":
            if not model.get("is_tee"):
                errors.append(f"{endpoint.id}: provider no longer marks endpoint as TEE")
            prompt = _per_token_price(model, "prompt")
            completion = _per_token_price(model, "completion")
            cache_read = _per_token_price(model, "input_cache_read")
            context_length = int(model.get("context_length", 0))
            supported_parameters = set(model.get("supported_parameters", []))
            live_capabilities = {
                "tools": "tools" in supported_parameters,
                "tool_choice": "tool_choice" in supported_parameters,
                "structured_outputs": bool(
                    {"response_format", "structured_outputs"}.intersection(supported_parameters)
                ),
                "reasoning": bool(
                    {"reasoning", "reasoning_effort", "include_reasoning"}.intersection(
                        supported_parameters
                    )
                ),
            }
        else:
            capabilities = model.get("model_spec", {}).get("capabilities", {})
            if endpoint.confidential and not capabilities.get("supportsTeeAttestation"):
                errors.append(f"{endpoint.id}: provider no longer reports TEE attestation")
            if endpoint.requires_client_encryption and not capabilities.get("supportsE2EE"):
                errors.append(f"{endpoint.id}: provider no longer reports E2EE support")
            pricing = model.get("model_spec", {}).get("pricing", {})
            prompt = _usd_price(pricing, "input")
            completion = _usd_price(pricing, "output")
            cache_read = _usd_price(pricing, "cache_input")
            context_length = int(model.get("model_spec", {}).get("availableContextTokens", 0))
            live_capabilities = {
                "vision": bool(capabilities.get("supportsVision")),
                "tools": bool(capabilities.get("supportsFunctionCalling")),
                "tool_choice": bool(capabilities.get("supportsFunctionCalling")),
                "structured_outputs": bool(capabilities.get("supportsResponseSchema")),
                "reasoning": bool(capabilities.get("supportsReasoning")),
            }
        configured_model = config.models[endpoint.model_id]
        if configured_model.max_context_length and (
            not context_length or configured_model.max_context_length > context_length
        ):
            errors.append(
                f"{endpoint.id}: canonical context={configured_model.max_context_length} "
                f"exceeds live context={context_length}"
            )
        for capability in endpoint.capabilities:
            if capability in live_capabilities and not live_capabilities[capability]:
                errors.append(f"{endpoint.id}: provider no longer reports {capability}")
        _check_price(errors, endpoint, "input", prompt)
        _check_price(errors, endpoint, "output", completion)
        _check_price(errors, endpoint, "cache_read", cache_read)
    if errors:
        print("Provider catalog validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"Validated {len(config.endpoints)} configured endpoints against live catalogs")
    return 0


async def _catalog(client: httpx.AsyncClient, url: str) -> dict[str, dict[str, Any]]:
    response = await client.get(url)
    response.raise_for_status()
    return {str(item["id"]): item for item in response.json()["data"]}


def _per_token_price(model: dict[str, Any], key: str) -> int:
    return round(float(model.get("pricing", {}).get(key, 0)) * 1_000_000 * 1_000_000)


def _usd_price(pricing: dict[str, Any], key: str) -> int:
    return round(float(pricing.get(key, {}).get("usd", 0)) * 1_000_000)


def _check_price(errors: list[str], endpoint: Endpoint, kind: str, live_microusd: int) -> None:
    configured = (
        endpoint.input_price_microusd_per_million
        if kind == "input"
        else (
            endpoint.output_price_microusd_per_million
            if kind == "output"
            else endpoint.cache_read_price_microusd_per_million
        )
    )
    # Undercharging is a release blocker. Overcharging is also blocked to keep
    # the public pass-through pricing promise exact.
    if configured != live_microusd:
        errors.append(
            f"{endpoint.id}: {kind} price configured={configured} live={live_microusd} microusd/M"
        )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
