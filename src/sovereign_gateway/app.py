from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import re
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, ValidationError
from redis.asyncio import Redis

from . import __version__
from .accounts import AccountsClient, WalletGrant
from .auth import ApiKeyStore, Principal
from .config import Endpoint, GatewayConfig, ModelRoute
from .errors import openai_error
from .managed_keys import (
    KeyUnavailable,
    ManagedKeyRepository,
    SpendLimitExceeded,
    build_managed_repository,
)
from .providers import PrivacyEvidence, adapter_for
from .rate_limit import (
    ConcurrencyLimiter,
    LocalConcurrencyLimiter,
    LocalFixedWindowLimiter,
    RateLimiter,
    RedisConcurrencyLimiter,
    RedisFixedWindowLimiter,
)
from .receipts import build_receipt_repository
from .routing import EndpointHealth, RoutingError, parse_routing_policy, route_endpoints
from .web import router as web_router

logger = logging.getLogger("sovereign_gateway")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(message)s")


class InferenceRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str
    stream: bool = False


@dataclass(frozen=True)
class BillingContext:
    key_id: str
    billing_id: str
    estimate_microusd: int
    wallet_grant: WalletGrant | None


PrivacyPreference = Literal["required", "preferred", "disabled"]
PROXY_PATHS = {"chat/completions", "completions", "embeddings"}
PACKAGE_DIR = Path(__file__).resolve().parent
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
SITE_AUTH_EXEMPT_PATHS = frozenset({"/healthz", "/readyz", "/v1"})


def _site_auth_protects_path(path: str) -> bool:
    return path not in SITE_AUTH_EXEMPT_PATHS and not path.startswith("/v1/")


def _site_basic_auth_error(request: Request) -> Response | None:
    expected_username = os.getenv("SITE_BASIC_AUTH_USERNAME", "")
    expected_password = os.getenv("SITE_BASIC_AUTH_PASSWORD", "")
    if not expected_username and not expected_password:
        return None
    if not _site_auth_protects_path(request.url.path):
        return None

    authorization = request.headers.get("authorization", "")
    scheme, _, encoded = authorization.partition(" ")
    if scheme.lower() == "basic" and encoded:
        try:
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            decoded = ""
        username, separator, password = decoded.partition(":")
        if (
            separator
            and expected_username
            and expected_password
            and secrets.compare_digest(username.encode(), expected_username.encode())
            and secrets.compare_digest(password.encode(), expected_password.encode())
        ):
            return None

    return Response(
        content="Authentication required\n",
        status_code=401,
        media_type="text/plain",
        headers={
            "www-authenticate": 'Basic realm="OpenSoftware API", charset="UTF-8"',
            "cache-control": "no-store",
        },
    )


def _settings() -> tuple[
    GatewayConfig,
    ApiKeyStore,
    ManagedKeyRepository | None,
    RateLimiter,
    Redis | None,
]:
    config = GatewayConfig.load(os.getenv("MODEL_CONFIG_PATH", "config/models.yaml"))
    managed_keys = build_managed_repository()
    keys = ApiKeyStore.from_environment(allow_empty=managed_keys is not None)
    rpm = int(os.getenv("REQUESTS_PER_MINUTE", "60"))
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        # Bound every Redis operation so a network partition degrades fast to
        # the fail-open path instead of hanging the request (and /readyz).
        redis_options: dict[str, Any] = {
            "decode_responses": True,
            "socket_connect_timeout": 1.0,
            "socket_timeout": 1.0,
        }
        redis_ca_cert = os.getenv("REDIS_CA_CERT_PATH")
        if redis_url.startswith("rediss://"):
            redis_options["ssl_cert_reqs"] = "required"
            if redis_ca_cert:
                redis_options["ssl_ca_certs"] = redis_ca_cert
        redis = Redis.from_url(redis_url, **redis_options)
        limiter: RateLimiter = RedisFixedWindowLimiter(redis, rpm)
        return config, keys, managed_keys, limiter, redis
    return config, keys, managed_keys, LocalFixedWindowLimiter(rpm), None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config, keys, managed_keys, limiter, redis = _settings()
    app.state.config = config
    app.state.keys = keys
    app.state.managed_keys = managed_keys
    app.state.limiter = limiter
    app.state.redis = redis
    concurrency: ConcurrencyLimiter = (
        RedisConcurrencyLimiter(redis) if redis else LocalConcurrencyLimiter()
    )
    app.state.concurrency = concurrency
    app.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10, read=600, write=30, pool=10),
        limits=httpx.Limits(max_connections=500, max_keepalive_connections=100),
    )
    app.state.accounts = AccountsClient(app.state.http)
    app.state.endpoint_health = EndpointHealth(
        failure_threshold=int(os.getenv("ENDPOINT_FAILURE_THRESHOLD", "3")),
        cooldown_seconds=int(os.getenv("ENDPOINT_CIRCUIT_COOLDOWN_SECONDS", "30")),
    )
    app.state.privacy_receipts = build_receipt_repository()
    yield
    await app.state.http.aclose()
    if redis:
        await redis.aclose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="OpenSoftware API",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

    @app.middleware("http")
    async def request_context(request: Request, call_next: Any) -> Response:
        supplied_request_id = request.headers.get("x-request-id", "")
        request_id = (
            supplied_request_id
            if REQUEST_ID_PATTERN.fullmatch(supplied_request_id)
            else str(uuid.uuid4())
        )
        request.state.request_id = request_id
        started = time.monotonic()
        response = _site_basic_auth_error(request)
        if response is None:
            try:
                response = await call_next(request)
            except Exception:
                logger.exception(
                    json.dumps(
                        {
                            "severity": "ERROR",
                            "event": "unhandled_exception",
                            "request_id": request_id,
                            "method": request.method,
                            "path": request.url.path,
                        },
                        separators=(",", ":"),
                    )
                )
                if request.url.path.startswith("/v1/"):
                    response = openai_error(
                        500,
                        "An internal error occurred",
                        error_type="server_error",
                        code="internal_error",
                    )
                else:
                    response = JSONResponse(
                        status_code=500,
                        content={"error": "internal_error", "request_id": request_id},
                    )
        response.headers["x-request-id"] = request_id
        response.headers["x-gateway-region"] = os.getenv("GATEWAY_REGION", "local")
        response.headers["x-content-type-options"] = "nosniff"
        response.headers["referrer-policy"] = "strict-origin-when-cross-origin"
        response.headers["permissions-policy"] = "camera=(), microphone=(), geolocation=()"
        if os.getenv("APP_ORIGIN", "").startswith("https://"):
            response.headers["strict-transport-security"] = "max-age=31536000; includeSubDomains"
        response.headers["content-security-policy"] = (
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self'; img-src 'self' data: https:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'"
        )
        site_auth_configured = bool(
            os.getenv("SITE_BASIC_AUTH_USERNAME") or os.getenv("SITE_BASIC_AUTH_PASSWORD")
        )
        if request.url.path.startswith(("/auth/", "/api/", "/dashboard")) or (
            site_auth_configured and _site_auth_protects_path(request.url.path)
        ):
            response.headers["cache-control"] = "no-store"
        if hasattr(request.state, "rate_limit_remaining"):
            response.headers["x-ratelimit-limit-requests"] = str(request.app.state.limiter.limit)
            response.headers["x-ratelimit-remaining-requests"] = str(
                request.state.rate_limit_remaining
            )
            response.headers["x-ratelimit-reset-requests"] = str(request.state.rate_limit_reset)
        logger.info(
            json.dumps(
                {
                    "severity": "INFO",
                    "event": "http_request",
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                },
                separators=(",", ":"),
            )
        )
        return response

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> Response:
        components: dict[str, str] = {}
        # Firestore is the system of record for keys, spend, and audit; it is
        # required for correct operation, so readiness fails when it is down.
        # Redis only backs rate limiting and admission control, which fail
        # open, so a Redis outage is reported as "degraded" but does not block
        # a region from starting or serving — otherwise the fail-open path
        # could never engage for a fresh instance.
        required_ok = True
        config: GatewayConfig = request.app.state.config
        missing_provider_keys = sorted(
            provider.id
            for provider in config.providers.values()
            if provider.enabled and provider.api_key_env and not provider.api_key
        )
        if missing_provider_keys:
            components["provider_credentials"] = "missing:" + ",".join(missing_provider_keys)
            required_ok = False
        if request.app.state.managed_keys:
            try:
                await request.app.state.managed_keys.healthcheck()
            except Exception:
                components["firestore"] = "unavailable"
                required_ok = False
            else:
                components["firestore"] = "ready"
        if request.app.state.redis:
            try:
                await asyncio.wait_for(request.app.state.redis.ping(), timeout=2)
            except Exception:
                components["redis"] = "degraded"
            else:
                components["redis"] = "ready"
        status = "ready" if required_ok else "not_ready"
        return JSONResponse(
            status_code=200 if required_ok else 503,
            content={"status": status, "components": components},
        )

    @app.get("/v1/models")
    async def models(request: Request) -> Response:
        auth_error = await _authorize(request)
        if auth_error:
            return auth_error
        config: GatewayConfig = request.app.state.config
        created = int(time.time())
        return JSONResponse(
            content={
                "object": "list",
                "data": [
                    {
                        "id": model.id,
                        "object": "model",
                        "created": created,
                        "owned_by": model.owned_by,
                        "privacy_modes": config.privacy_modes_for(model.id),
                        "privacy_levels": sorted(
                            {endpoint.privacy.level for endpoint in config.endpoints_for(model.id)}
                        ),
                        "providers": sorted(
                            {endpoint.provider for endpoint in config.endpoints_for(model.id)}
                        ),
                        "supported_parameters": sorted(
                            set(model.capabilities).union(
                                *(
                                    endpoint.capabilities
                                    for endpoint in config.endpoints_for(model.id)
                                )
                            )
                        ),
                        "architecture": {
                            "input_modalities": list(model.input_modalities),
                            "output_modalities": list(model.output_modalities),
                        },
                        "max_context_length": model.max_context_length,
                        "context_length": model.max_context_length,
                        "pricing": {
                            "input_per_million_usd": (
                                model.input_price_microusd_per_million / 1_000_000
                            ),
                            "output_per_million_usd": (
                                model.output_price_microusd_per_million / 1_000_000
                            ),
                        },
                    }
                    for model in config.models.values()
                ],
            }
        )

    @app.get("/v1/providers")
    async def providers(request: Request) -> Response:
        auth_error = await _authorize(request)
        if auth_error:
            return auth_error
        config: GatewayConfig = request.app.state.config
        return JSONResponse(
            content={
                "object": "list",
                "data": [
                    {
                        "id": provider.id,
                        "name": provider.name,
                        "enabled": provider.enabled,
                        "regions": list(provider.regions),
                        "models": sorted(
                            {
                                endpoint.model_id
                                for endpoint in config.endpoints.values()
                                if endpoint.provider == provider.id and endpoint.enabled
                            }
                        ),
                    }
                    for provider in config.providers.values()
                ],
            }
        )

    @app.get("/v1/models/{model_id:path}/endpoints")
    async def model_endpoints(model_id: str, request: Request) -> Response:
        auth_error = await _authorize(request)
        if auth_error:
            return auth_error
        config: GatewayConfig = request.app.state.config
        if model_id not in config.models:
            return openai_error(404, f"Model {model_id!r} does not exist", code="model_not_found")
        return JSONResponse(
            content={
                "object": "list",
                "data": [
                    _endpoint_public(endpoint, request.app.state.endpoint_health)
                    for endpoint in config.endpoints_for(model_id)
                ],
            }
        )

    @app.get("/v1/privacy/receipts/{generation_id}")
    async def privacy_receipt(generation_id: str, request: Request) -> Response:
        auth_error = await _authorize(request)
        if auth_error:
            return auth_error
        principal: Principal = request.state.principal
        receipt = await request.app.state.privacy_receipts.get(principal.tenant_id, generation_id)
        if not receipt:
            return openai_error(404, "Privacy receipt does not exist", code="receipt_not_found")
        return JSONResponse(content=receipt)

    @app.get("/v1/generation")
    async def generation(request: Request) -> Response:
        auth_error = await _authorize(request)
        if auth_error:
            return auth_error
        generation_id = request.query_params.get("id", "")
        if not generation_id:
            return openai_error(400, "id is required", code="invalid_request")
        principal: Principal = request.state.principal
        record = await request.app.state.privacy_receipts.get(principal.tenant_id, generation_id)
        if not record:
            return openai_error(404, "Generation does not exist", code="generation_not_found")
        return JSONResponse(content={"data": record})

    @app.post("/v1/attestation")
    async def attestation(request: Request) -> Response:
        auth_error = await _authorize(request)
        if auth_error:
            return auth_error
        agent_url = os.getenv("ATTESTATION_AGENT_URL", "").rstrip("/")
        if not agent_url:
            return openai_error(
                503,
                "Attestation evidence is not available in this deployment",
                error_type="server_error",
                code="attestation_unavailable",
            )
        try:
            payload = json.loads(await request.body() or b"{}")
        except json.JSONDecodeError:
            return openai_error(400, "Request body must be JSON", code="invalid_request")
        nonce = payload.get("nonce", "")
        if not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", nonce):
            return openai_error(
                400,
                "nonce must be 8-128 URL-safe characters",
                code="invalid_nonce",
            )
        config: GatewayConfig = request.app.state.config
        binding = {
            "nonce": nonce,
            "gateway_version": __version__,
            "gateway_region": os.getenv("GATEWAY_REGION", "local"),
            "confidential_backends": sorted(
                {
                    backend.provider: {
                        "backend": backend.provider,
                        "revision": backend.revision,
                    }
                    for backend in config.backends.values()
                    if backend.confidential
                }.values(),
                key=lambda item: item["backend"],
            ),
            "issued_at": int(time.time()),
        }
        binding_canonical = json.dumps(binding, separators=(",", ":"), sort_keys=True)
        report_data = hashlib.sha512(binding_canonical.encode()).hexdigest()
        try:
            upstream = await request.app.state.http.post(
                f"{agent_url}/quote",
                json={"report_data": report_data},
                timeout=30,
            )
        except httpx.HTTPError:
            upstream = None
        if upstream is None or upstream.status_code != 200:
            # Fail closed: never substitute unverifiable evidence.
            logger.error(
                json.dumps(
                    {
                        "severity": "ERROR",
                        "event": "attestation_unavailable",
                        "request_id": request.state.request_id,
                    },
                    separators=(",", ":"),
                )
            )
            return openai_error(
                503,
                "Attestation evidence is temporarily unavailable",
                error_type="server_error",
                code="attestation_unavailable",
            )
        evidence = upstream.json()
        return JSONResponse(
            content={
                "object": "attestation",
                "format": "tdx.quote.v4",
                "binding": binding,
                "binding_canonical": binding_canonical,
                "report_data_sha512": report_data,
                "quote_provider": evidence.get("provider"),
                "quote": evidence.get("quote"),
            }
        )

    @app.post("/v1/{proxy_path:path}")
    async def routed_inference(proxy_path: str, request: Request) -> Response:
        if proxy_path not in PROXY_PATHS:
            return openai_error(404, "Unknown API endpoint", code="not_found")
        auth_error = await _authorize(request)
        if auth_error:
            return auth_error
        max_request_bytes = int(os.getenv("MAX_REQUEST_BYTES", "10485760"))
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > max_request_bytes:
                    return openai_error(413, "Request body is too large", code="request_too_large")
            except ValueError:
                return openai_error(
                    400, "Content-Length must be an integer", code="invalid_request"
                )
        raw = await request.body()
        if len(raw) > max_request_bytes:
            return openai_error(413, "Request body is too large", code="request_too_large")
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict) and not payload.get("model"):
                fallback_models = payload.get("models")
                if (
                    isinstance(fallback_models, list)
                    and fallback_models
                    and isinstance(fallback_models[0], str)
                ):
                    payload["model"] = fallback_models[0]
            parsed = InferenceRequest.model_validate(payload)
        except (json.JSONDecodeError, ValidationError):
            return openai_error(400, "Request body must include a valid model")

        config: GatewayConfig = request.app.state.config
        requested_models_raw = payload.get("models")
        if requested_models_raw is None:
            requested_models = [parsed.model]
        elif (
            not isinstance(requested_models_raw, list)
            or not requested_models_raw
            or any(not isinstance(item, str) or not item for item in requested_models_raw)
        ):
            return openai_error(
                400, "models must be a non-empty list of model IDs", code="invalid_request"
            )
        else:
            requested_models = list(dict.fromkeys(requested_models_raw))
            if parsed.model not in requested_models:
                requested_models.insert(0, parsed.model)
        unknown_models = [
            model_id for model_id in requested_models if model_id not in config.models
        ]
        if unknown_models:
            return openai_error(
                404, f"Model {unknown_models[0]!r} does not exist", code="model_not_found"
            )
        try:
            policy = parse_routing_policy(
                payload,
                request.headers.get("x-confidential-compute"),
                client_encrypted=bool(
                    request.headers.get("x-venice-tee-client-pub-key")
                    and request.headers.get("x-venice-tee-model-pub-key")
                ),
            )
            candidates: list[Endpoint] = []
            routing_errors: list[RoutingError] = []
            for model_id in requested_models:
                try:
                    candidates.extend(
                        route_endpoints(config, model_id, policy, request.app.state.endpoint_health)
                    )
                except RoutingError as exc:
                    routing_errors.append(exc)
            candidates = list({endpoint.id: endpoint for endpoint in candidates}.values())
            if not policy.provider.allow_fallbacks:
                candidates = candidates[:1]
            if not candidates:
                raise routing_errors[0]
        except RoutingError as exc:
            status = 409 if "unavailable" in exc.code else 400
            # Preserve the original public error for old clients.
            code = (
                "confidential_compute_unavailable"
                if exc.code == "privacy_requirements_unavailable"
                and request.headers.get("x-confidential-compute", "").lower() == "required"
                else exc.code
            )
            return openai_error(status, str(exc), code=code)

        # Reserve against the most expensive eligible endpoint so fallback can
        # never bypass a key or wallet spend limit.
        estimate_endpoint = max(
            candidates,
            key=lambda item: (
                item.input_price_microusd_per_million + item.output_price_microusd_per_million
            ),
        )
        billing = await _prepare_billing(request, estimate_endpoint, payload, len(raw))
        if isinstance(billing, Response):
            return billing

        failures: list[dict[str, Any]] = []
        for endpoint in candidates:
            provider = config.providers[endpoint.provider]
            capacity_held = False
            if endpoint.max_concurrency:
                try:
                    capacity_held = await request.app.state.concurrency.acquire(
                        endpoint.id, endpoint.max_concurrency
                    )
                except Exception:
                    logger.exception(
                        json.dumps(
                            {
                                "severity": "WARNING",
                                "event": "redis_degraded",
                                "check": "concurrency",
                                "endpoint": endpoint.id,
                                "request_id": request.state.request_id,
                            }
                        )
                    )
                    capacity_held = True
                if not capacity_held:
                    failures.append({"endpoint": endpoint.id, "status": 429})
                    continue

            async def free_capacity(endpoint_id: str = endpoint.id) -> None:
                nonlocal capacity_held
                if capacity_held:
                    capacity_held = False
                    try:
                        await request.app.state.concurrency.release(endpoint_id)
                    except Exception:
                        logger.exception("endpoint concurrency release failed")

            outbound = dict(payload)
            if parsed.stream:
                stream_options = outbound.get("stream_options")
                if not isinstance(stream_options, dict):
                    stream_options = {}
                outbound["stream_options"] = {**stream_options, "include_usage": True}
            adapter = adapter_for(provider)
            if endpoint.requires_client_encryption:
                key_valid = await _validate_client_encryption_key(
                    request.app.state.http,
                    provider.base_url,
                    provider.api_key,
                    endpoint,
                    request.headers,
                )
                if not key_valid:
                    await free_capacity()
                    await request.app.state.endpoint_health.failure(endpoint.id)
                    failures.append(
                        {"endpoint": endpoint.id, "error": "e2ee_key_attestation_failed"}
                    )
                    continue
            upstream_request = adapter.build_request(
                request.app.state.http,
                provider,
                endpoint,
                proxy_path,
                outbound,
                request.state.request_id,
                request.headers,
            )
            started = time.monotonic()
            try:
                upstream = await request.app.state.http.send(upstream_request, stream=parsed.stream)
            except (httpx.TimeoutException, httpx.HTTPError) as exc:
                await free_capacity()
                await request.app.state.endpoint_health.failure(endpoint.id)
                failures.append({"endpoint": endpoint.id, "error": type(exc).__name__})
                continue

            latency = time.monotonic() - started
            if upstream.status_code in {408, 409, 425, 429} or upstream.status_code >= 500:
                content = await upstream.aread()
                await upstream.aclose()
                await free_capacity()
                await request.app.state.endpoint_health.failure(endpoint.id)
                failures.append(
                    {
                        "endpoint": endpoint.id,
                        "status": upstream.status_code,
                        "body_bytes": len(content),
                    }
                )
                continue
            if upstream.status_code >= 400:
                content = await upstream.aread()
                await upstream.aclose()
                await free_capacity()
                await _release_billing(request, billing, f"upstream_{upstream.status_code}")
                return Response(
                    content=content,
                    status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type", "application/json"),
                    headers=_endpoint_headers(endpoint, None),
                )

            evidence = adapter.evidence(upstream, endpoint)
            if policy.privacy.require_response_proof and evidence.status == "missing":
                await upstream.aclose()
                await free_capacity()
                await request.app.state.endpoint_health.failure(endpoint.id)
                failures.append({"endpoint": endpoint.id, "error": "privacy_evidence_missing"})
                continue

            privacy_label = (
                "standard-fallback"
                if not endpoint.confidential
                and policy.privacy.minimum != "standard"
                and policy.privacy.allow_downgrade
                else None
            )
            response_headers = _endpoint_headers(endpoint, evidence, privacy_label)
            content_type = upstream.headers.get("content-type", "application/json")
            generation_id = request.state.request_id
            await _store_privacy_receipt(request, generation_id, endpoint, evidence)
            if parsed.stream:
                await request.app.state.endpoint_health.success(endpoint.id, latency)
                return StreamingResponse(
                    _metered_stream(
                        request,
                        upstream,
                        billing,
                        endpoint,
                        endpoint.model_id,
                        endpoint.privacy.level,
                        endpoint.id,
                        on_close=free_capacity,
                    ),
                    status_code=upstream.status_code,
                    media_type=content_type,
                    headers=response_headers,
                )

            content = await upstream.aread()
            await upstream.aclose()
            await free_capacity()
            usage = _usage_from_json(content)
            output_tokens = int((usage or {}).get("completion_tokens", 0)) or None
            await request.app.state.endpoint_health.success(endpoint.id, latency, output_tokens)
            try:
                response_payload = json.loads(content)
                if isinstance(response_payload, dict):
                    response_payload["model"] = endpoint.model_id
                    content = json.dumps(response_payload, separators=(",", ":")).encode()
                    if response_payload.get("id"):
                        generation_id = str(response_payload["id"])
                        await _store_privacy_receipt(request, generation_id, endpoint, evidence)
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            await _settle_billing(
                request,
                billing,
                endpoint,
                endpoint.model_id,
                endpoint.privacy.level,
                endpoint.id,
                usage,
            )
            response_headers["x-os-generation-id"] = generation_id
            return Response(
                content=content,
                status_code=upstream.status_code,
                media_type=content_type,
                headers=response_headers,
            )

        await _release_billing(request, billing, "all_endpoints_failed")
        logger.error(
            json.dumps(
                {
                    "severity": "ERROR",
                    "event": "all_endpoints_failed",
                    "request_id": request.state.request_id,
                    "model": endpoint.model_id,
                    "attempts": failures,
                },
                separators=(",", ":"),
            )
        )
        overloaded = bool(failures) and all(item.get("status") == 429 for item in failures)
        return openai_error(
            503,
            (
                "The model is at capacity; retry after a short delay"
                if overloaded
                else "All eligible private inference endpoints are unavailable"
            ),
            error_type="server_error",
            code="model_overloaded" if overloaded else "all_endpoints_unavailable",
            headers={"retry-after": "10"},
        )

    async def inference(proxy_path: str, request: Request) -> Response:
        if proxy_path not in PROXY_PATHS:
            return openai_error(404, "Unknown API endpoint", code="not_found")
        auth_error = await _authorize(request)
        if auth_error:
            return auth_error

        max_request_bytes = int(os.getenv("MAX_REQUEST_BYTES", "10485760"))
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                declared_length = int(content_length)
            except ValueError:
                return openai_error(
                    400,
                    "Content-Length must be an integer",
                    code="invalid_request",
                )
            if declared_length < 0:
                return openai_error(
                    400,
                    "Content-Length cannot be negative",
                    code="invalid_request",
                )
            if declared_length > max_request_bytes:
                return openai_error(413, "Request body is too large", code="request_too_large")
        raw = await request.body()
        if len(raw) > max_request_bytes:
            return openai_error(413, "Request body is too large", code="request_too_large")
        try:
            payload = json.loads(raw)
            parsed = InferenceRequest.model_validate(payload)
        except (json.JSONDecodeError, ValidationError):
            return openai_error(400, "Request body must include a valid model")

        config: GatewayConfig = request.app.state.config
        route = config.models.get(parsed.model)
        if not route:
            return openai_error(
                404,
                f"Model {parsed.model!r} does not exist",
                code="model_not_found",
            )

        preference = request.headers.get("x-confidential-compute", "preferred").lower()
        if preference not in {"required", "preferred", "disabled"}:
            return openai_error(
                400,
                "X-Confidential-Compute must be required, preferred, or disabled",
                code="invalid_privacy_preference",
            )
        selected = _select_backend(config, route, preference)  # type: ignore[arg-type]
        if isinstance(selected, Response):
            return selected
        backend, privacy = selected

        capacity_held = False
        if backend.max_concurrency:
            try:
                admitted = await request.app.state.concurrency.acquire(
                    backend.name, backend.max_concurrency
                )
                capacity_held = admitted
            except Exception:
                # Fail open: losing admission control degrades to upstream
                # queueing, which beats rejecting all inference in the region.
                logger.exception(
                    json.dumps(
                        {
                            "severity": "WARNING",
                            "event": "redis_degraded",
                            "check": "concurrency",
                            "request_id": request.state.request_id,
                            "backend": backend.name,
                        },
                        separators=(",", ":"),
                    )
                )
                admitted = True
            if not admitted:
                logger.warning(
                    json.dumps(
                        {
                            "severity": "WARNING",
                            "event": "model_overloaded",
                            "request_id": request.state.request_id,
                            "backend": backend.name,
                            "max_concurrency": backend.max_concurrency,
                        },
                        separators=(",", ":"),
                    )
                )
                return openai_error(
                    503,
                    "The model is at capacity; retry after a short delay",
                    error_type="server_error",
                    code="model_overloaded",
                    headers={"retry-after": "10"},
                )

        async def _free_capacity() -> None:
            nonlocal capacity_held
            if capacity_held:
                capacity_held = False
                try:
                    await request.app.state.concurrency.release(backend.name)
                except Exception:
                    # The counter's expiry self-heals a missed release.
                    logger.exception(
                        json.dumps(
                            {
                                "severity": "WARNING",
                                "event": "redis_degraded",
                                "check": "concurrency_release",
                                "request_id": request.state.request_id,
                                "backend": backend.name,
                            },
                            separators=(",", ":"),
                        )
                    )

        billing = await _prepare_billing(request, route, payload, len(raw))
        if isinstance(billing, Response):
            await _free_capacity()
            return billing

        payload["model"] = route.upstream_model
        if parsed.stream:
            stream_options = payload.get("stream_options")
            if not isinstance(stream_options, dict):
                stream_options = {}
            payload["stream_options"] = {**stream_options, "include_usage": True}
        headers = {
            "content-type": "application/json",
            "x-request-id": request.state.request_id,
        }
        if backend.api_key:
            headers["authorization"] = f"Bearer {backend.api_key}"

        upstream_request = request.app.state.http.build_request(
            "POST",
            f"{backend.base_url}/v1/{proxy_path}",
            headers=headers,
            content=json.dumps(payload, separators=(",", ":")),
        )
        try:
            upstream = await request.app.state.http.send(upstream_request, stream=parsed.stream)
        except httpx.TimeoutException:
            await _free_capacity()
            await _release_billing(request, billing, "backend_timeout")
            logger.exception(
                json.dumps(
                    {
                        "severity": "ERROR",
                        "event": "upstream_timeout",
                        "request_id": request.state.request_id,
                        "backend": backend.name,
                    }
                )
            )
            return openai_error(
                504,
                "The selected inference backend timed out",
                error_type="server_error",
                code="backend_timeout",
            )
        except httpx.HTTPError:
            await _free_capacity()
            await _release_billing(request, billing, "backend_unavailable")
            logger.exception(
                json.dumps(
                    {
                        "severity": "ERROR",
                        "event": "upstream_unavailable",
                        "request_id": request.state.request_id,
                        "backend": backend.name,
                    }
                )
            )
            return openai_error(
                503,
                "The selected inference backend is unavailable",
                error_type="server_error",
                code="backend_unavailable",
            )

        response_headers = {
            "x-compute-privacy": privacy,
            "x-model-backend": backend.name,
        }
        if backend.revision:
            response_headers["x-model-revision"] = backend.revision
        content_type = upstream.headers.get("content-type", "application/json")
        if upstream.status_code >= 400:
            content = await upstream.aread()
            await upstream.aclose()
            await _free_capacity()
            await _release_billing(request, billing, f"upstream_{upstream.status_code}")
            return Response(
                content=content,
                status_code=upstream.status_code,
                media_type=content_type,
                headers=response_headers,
            )
        if parsed.stream:
            return StreamingResponse(
                _metered_stream(
                    request,
                    upstream,
                    billing,
                    route,
                    parsed.model,
                    privacy,
                    backend.name,
                    on_close=_free_capacity,
                ),
                status_code=upstream.status_code,
                media_type=content_type,
                headers=response_headers,
            )
        content = await upstream.aread()
        await upstream.aclose()
        await _free_capacity()
        usage = _usage_from_json(content)
        await _settle_billing(
            request,
            billing,
            route,
            parsed.model,
            privacy,
            backend.name,
            usage,
        )
        return Response(
            content=content,
            status_code=upstream.status_code,
            media_type=content_type,
            headers=response_headers,
        )

    app.include_router(web_router)
    return app


def _endpoint_headers(
    endpoint: Endpoint,
    evidence: PrivacyEvidence | None,
    privacy_label: str | None = None,
) -> dict[str, str]:
    headers = {
        "x-compute-privacy": privacy_label
        or ("confidential" if endpoint.confidential else "standard"),
        "x-model-backend": endpoint.provider if ":" in endpoint.id else endpoint.id,
        "x-os-provider": endpoint.provider,
        "x-os-endpoint": endpoint.id,
        "x-os-model": endpoint.model_id,
        "x-os-privacy-level": endpoint.privacy.level,
        "x-os-attestation-status": evidence.status if evidence else "not-requested",
    }
    if endpoint.revision:
        headers["x-model-revision"] = endpoint.revision
    if evidence and evidence.receipt_id:
        headers["x-os-receipt-id"] = evidence.receipt_id
    return headers


async def _validate_client_encryption_key(
    http: httpx.AsyncClient,
    provider_base_url: str,
    provider_api_key: str | None,
    endpoint: Endpoint,
    client_headers: Any,
) -> bool:
    if endpoint.provider != "venice":
        # New E2EE adapters must implement an explicit key-binding verifier;
        # an unknown encrypted transport fails closed.
        return False
    client_key = str(client_headers.get("x-venice-tee-model-pub-key", ""))
    if not client_key:
        return False
    nonce = secrets.token_hex(32)
    headers = {"authorization": f"Bearer {provider_api_key}"} if provider_api_key else {}
    try:
        response = await http.get(
            f"{provider_base_url}/v1/tee/attestation",
            params={"model": endpoint.upstream_model, "nonce": nonce},
            headers=headers,
            timeout=30,
        )
        if response.status_code != 200:
            return False
        evidence = response.json()
    except (httpx.HTTPError, ValueError):
        return False
    if evidence.get("verified") is not True or evidence.get("nonce") != nonce:
        return False
    attested_key = evidence.get("signing_key") or evidence.get("signing_public_key")
    if not isinstance(attested_key, str):
        return False
    return secrets.compare_digest(
        _normalize_public_key(client_key), _normalize_public_key(attested_key)
    )


def _normalize_public_key(value: str) -> str:
    normalized = value.lower().removeprefix("0x")
    if normalized.startswith("04") and len(normalized) == 130:
        normalized = normalized[2:]
    return normalized


def _endpoint_public(endpoint: Endpoint, health: EndpointHealth) -> dict[str, Any]:
    stats = health.get(endpoint.id)
    return {
        "id": endpoint.id,
        "model": endpoint.model_id,
        "provider": endpoint.provider,
        "enabled": endpoint.enabled,
        "privacy": endpoint.privacy.public_dict(),
        "pricing": {
            "input_per_million_usd": endpoint.input_price_microusd_per_million / 1_000_000,
            "output_per_million_usd": endpoint.output_price_microusd_per_million / 1_000_000,
            "cache_read_per_million_usd": (
                endpoint.cache_read_price_microusd_per_million / 1_000_000
            ),
            "cache_write_per_million_usd": (
                endpoint.cache_write_price_microusd_per_million / 1_000_000
            ),
        },
        "capabilities": sorted(endpoint.capabilities),
        "regions": list(endpoint.regions),
        "health": {
            "status": "healthy" if stats.healthy else "circuit_open",
            "attempts": stats.attempts,
            "success_rate": stats.success_rate,
            "latency_seconds": None if stats.latency == float("inf") else stats.latency,
            "throughput_tokens_per_second": stats.throughput or None,
        },
    }


async def _store_privacy_receipt(
    request: Request,
    generation_id: str,
    endpoint: Endpoint,
    evidence: PrivacyEvidence,
) -> None:
    # This cache is an immediate retrieval path. Durable generation metadata is
    # also written through billing settlement for managed keys.
    principal: Principal = request.state.principal
    receipt = {
        "object": "privacy.receipt",
        "generation_id": generation_id,
        "provider": endpoint.provider,
        "endpoint": endpoint.id,
        "model": endpoint.model_id,
        "privacy": endpoint.privacy.public_dict(),
        "evidence": evidence.public_dict(),
        "verified_at": int(time.time()),
    }
    try:
        await request.app.state.privacy_receipts.save(principal.tenant_id, generation_id, receipt)
    except Exception:
        # Receipt storage is an audit/readback feature. The response still
        # carries provider evidence headers, and a storage outage must not
        # strand the billing reservation after inference already succeeded.
        logger.exception(
            json.dumps(
                {
                    "severity": "ERROR",
                    "event": "privacy_receipt_storage_failed",
                    "request_id": request.state.request_id,
                    "generation_id": generation_id,
                    "endpoint": endpoint.id,
                },
                separators=(",", ":"),
            )
        )


async def _authorize(request: Request) -> Response | None:
    authorization = request.headers.get("authorization", "")
    scheme, _, raw_key = authorization.partition(" ")
    if scheme.lower() != "bearer" or not raw_key:
        return openai_error(
            401,
            "Missing bearer API key",
            error_type="authentication_error",
            code="invalid_api_key",
        )
    principal: Principal | None = request.app.state.keys.authenticate(raw_key)
    if not principal and request.app.state.managed_keys:
        principal = await request.app.state.managed_keys.authenticate(raw_key)
    if not principal:
        return openai_error(
            401,
            "Invalid API key",
            error_type="authentication_error",
            code="invalid_api_key",
        )
    try:
        allowed, remaining = await request.app.state.limiter.allow(principal.tenant_id)
    except Exception:
        # Fail open: hard spend limits stay enforced in Firestore and Cloud
        # Armor still caps per-IP rates at the edge, so availability wins over
        # a temporarily unenforced per-key RPM. The alert on this event is the
        # signal to fail the region away per the runbook.
        logger.exception(
            json.dumps(
                {
                    "severity": "WARNING",
                    "event": "redis_degraded",
                    "check": "rate_limit",
                    "request_id": request.state.request_id,
                },
                separators=(",", ":"),
            )
        )
        request.state.principal = principal
        return None
    reset_at = (int(time.time()) // 60 + 1) * 60
    request.state.rate_limit_remaining = remaining
    request.state.rate_limit_reset = reset_at
    if not allowed:
        response = openai_error(
            429,
            "Rate limit exceeded",
            error_type="rate_limit_error",
            code="rate_limit_exceeded",
        )
        response.headers["retry-after"] = str(max(1, reset_at - int(time.time())))
        return response
    request.state.principal = principal
    return None


async def _prepare_billing(
    request: Request,
    route: ModelRoute | Endpoint,
    payload: dict[str, Any],
    request_bytes: int,
) -> BillingContext | None | Response:
    principal: Principal = request.state.principal
    repository: ManagedKeyRepository | None = request.app.state.managed_keys
    if not principal.key_id or not principal.user_id or not repository:
        return None
    billing_id = str(uuid.uuid4())
    estimate = _estimate_cost(route, payload, request_bytes)
    try:
        await repository.reserve(principal.key_id, billing_id, estimate)
    except SpendLimitExceeded:
        return openai_error(
            402,
            "This API key has reached its spend limit",
            error_type="billing_error",
            code="spend_limit_exceeded",
        )
    except KeyUnavailable:
        return openai_error(
            401,
            "This API key is expired or revoked",
            error_type="authentication_error",
            code="invalid_api_key",
        )

    accounts: AccountsClient = request.app.state.accounts
    decision = await accounts.authorize_wallet(
        principal.user_id,
        estimate,
        request.state.request_id,
    )
    if decision.reason:
        unavailable = decision.reason == "accounts_unavailable"
        await repository.release(
            principal.key_id,
            billing_id,
            "billing_service_unavailable" if unavailable else "wallet_denied",
        )
        if unavailable:
            return openai_error(
                503,
                "The billing service is temporarily unavailable",
                error_type="server_error",
                code="billing_service_unavailable",
            )
        return openai_error(
            402,
            "Your OpenSoftware account does not have enough credits",
            error_type="billing_error",
            code="insufficient_credits",
        )
    return BillingContext(principal.key_id, billing_id, estimate, decision.grant)


async def _release_billing(
    request: Request,
    billing: BillingContext | None,
    reason: str,
) -> None:
    if not billing or not request.app.state.managed_keys:
        return
    try:
        await request.app.state.managed_keys.release(billing.key_id, billing.billing_id, reason)
    except Exception:
        logger.exception(
            json.dumps(
                {
                    "event": "billing_release_failed",
                    "request_id": request.state.request_id,
                    "billing_id": billing.billing_id,
                }
            )
        )


async def _settle_billing(
    request: Request,
    billing: BillingContext | None,
    route: ModelRoute | Endpoint,
    model: str,
    privacy: str,
    backend: str,
    usage: dict[str, Any] | None,
) -> None:
    if not billing or not request.app.state.managed_keys:
        return
    clean_usage = _clean_usage(usage)
    actual = _actual_cost(route, clean_usage) if clean_usage else billing.estimate_microusd
    event = {
        "request_id": request.state.request_id,
        "model": model,
        "privacy": privacy,
        "backend": backend,
        "provider": route.provider if isinstance(route, Endpoint) else None,
        "endpoint": route.id if isinstance(route, Endpoint) else backend,
        "upstream_inference_cost_microusd": actual,
        **clean_usage,
    }
    try:
        await request.app.state.managed_keys.settle(
            billing.key_id,
            billing.billing_id,
            actual,
            event,
        )
        accounts: AccountsClient = request.app.state.accounts
        charged = await accounts.charge_wallet(
            billing.wallet_grant,
            actual,
            billing.billing_id,
            request.state.request_id,
        )
        if not charged:
            logger.error(
                json.dumps(
                    {
                        "severity": "ERROR",
                        "event": "wallet_charge_failed",
                        "request_id": request.state.request_id,
                        "billing_id": billing.billing_id,
                    }
                )
            )
    except Exception:
        logger.exception(
            json.dumps(
                {
                    "event": "billing_settlement_failed",
                    "request_id": request.state.request_id,
                    "billing_id": billing.billing_id,
                }
            )
        )


async def _metered_stream(
    request: Request,
    upstream: httpx.Response,
    billing: BillingContext | None,
    route: ModelRoute | Endpoint,
    model: str,
    privacy: str,
    backend: str,
    on_close: Callable[[], Awaitable[None]] | None = None,
) -> AsyncIterator[bytes]:
    pending = b""
    usage: dict[str, Any] | None = None
    try:
        async for chunk in upstream.aiter_raw():
            pending += chunk
            lines = pending.split(b"\n")
            pending = lines.pop()
            for line in lines:
                parsed = _usage_from_sse_line(line)
                if parsed is not None:
                    usage = parsed
                yield _normalize_sse_line(line, model) + b"\n"
        if pending:
            usage = _usage_from_sse_line(pending) or usage
            yield _normalize_sse_line(pending, model)
    finally:
        await upstream.aclose()
        if on_close is not None:
            await on_close()
        await _settle_billing(request, billing, route, model, privacy, backend, usage)


def _estimate_cost(
    route: ModelRoute | Endpoint, payload: dict[str, Any], request_bytes: int
) -> int:
    prompt_upper_bound = max(1, request_bytes)
    requested_output = payload.get("max_completion_tokens", payload.get("max_tokens"))
    try:
        output_tokens = int(requested_output) if requested_output is not None else 2_048
    except (TypeError, ValueError):
        output_tokens = 2_048
    context_limit = route.max_context_length if isinstance(route, ModelRoute) else None
    if context_limit:
        output_tokens = min(max(1, output_tokens), context_limit)
    return max(
        1,
        (
            prompt_upper_bound * route.input_price_microusd_per_million
            + output_tokens * route.output_price_microusd_per_million
            + 999_999
        )
        // 1_000_000,
    )


def _actual_cost(route: ModelRoute | Endpoint, usage: dict[str, Any]) -> int:
    prompt = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)))
    completion = int(usage.get("completion_tokens", usage.get("output_tokens", 0)))
    cached = int(usage.get("cached_tokens", 0))
    cache_write = int(usage.get("cache_write_tokens", 0))
    if isinstance(route, Endpoint):
        prompt = max(0, prompt - cached - cache_write)
        cache_cost = (
            cached * route.cache_read_price_microusd_per_million
            + cache_write * route.cache_write_price_microusd_per_million
        )
    else:
        cache_cost = 0
    return max(
        1,
        (
            prompt * route.input_price_microusd_per_million
            + completion * route.output_price_microusd_per_million
            + cache_cost
            + 999_999
        )
        // 1_000_000,
    )


def _usage_from_json(content: bytes) -> dict[str, Any] | None:
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    usage = payload.get("usage") if isinstance(payload, dict) else None
    return usage if isinstance(usage, dict) else None


def _usage_from_sse_line(line: bytes) -> dict[str, Any] | None:
    if not line.startswith(b"data:"):
        return None
    data = line[5:].strip()
    if not data or data == b"[DONE]":
        return None
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    usage = payload.get("usage") if isinstance(payload, dict) else None
    return usage if isinstance(usage, dict) else None


def _normalize_sse_line(line: bytes, model: str) -> bytes:
    if not line.startswith(b"data:"):
        return line
    data = line[5:].strip()
    if not data or data == b"[DONE]":
        return line
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return line
    if not isinstance(payload, dict):
        return line
    payload["model"] = model
    return b"data: " + json.dumps(payload, separators=(",", ":")).encode()


def _clean_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    if not usage:
        return {}
    clean: dict[str, int] = {}
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "cache_write_tokens",
    ):
        value = usage.get(key)
        if isinstance(value, int) and value >= 0:
            clean[key] = value
    return clean


def _select_backend(
    config: GatewayConfig,
    route: ModelRoute,
    preference: PrivacyPreference,
) -> tuple[Endpoint, str] | Response:
    if preference == "required":
        if not route.confidential_backend:
            return openai_error(
                409,
                f"Model {route.id!r} is not available on confidential compute",
                code="confidential_compute_unavailable",
            )
        return config.backends[route.confidential_backend], "confidential"
    if preference == "disabled":
        if not route.standard_backend:
            return openai_error(
                409,
                f"Model {route.id!r} is only available on confidential compute",
                code="standard_compute_unavailable",
            )
        return config.backends[route.standard_backend], "standard"
    if route.confidential_backend:
        return config.backends[route.confidential_backend], "confidential"
    assert route.standard_backend
    return config.backends[route.standard_backend], "standard-fallback"


app = create_app()
