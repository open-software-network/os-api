from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import secrets
import time
import uuid
from collections.abc import AsyncIterator
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

from .accounts import AccountsClient, WalletGrant
from .auth import ApiKeyStore, Principal
from .config import Backend, GatewayConfig, ModelRoute
from .errors import openai_error
from .managed_keys import (
    KeyUnavailable,
    ManagedKeyRepository,
    SpendLimitExceeded,
    build_managed_repository,
)
from .rate_limit import LocalFixedWindowLimiter, RateLimiter, RedisFixedWindowLimiter
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
        redis_options: dict[str, Any] = {"decode_responses": True}
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
    app.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10, read=600, write=30, pool=10),
        limits=httpx.Limits(max_connections=500, max_keepalive_connections=100),
    )
    app.state.accounts = AccountsClient(app.state.http)
    yield
    await app.state.http.aclose()
    if redis:
        await redis.aclose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="OpenSoftware API",
        version="0.5.0",
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
            response.headers["x-ratelimit-reset-requests"] = str(
                request.state.rate_limit_reset
            )
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
        if request.app.state.redis:
            try:
                await request.app.state.redis.ping()
            except Exception:
                components["redis"] = "unavailable"
            else:
                components["redis"] = "ready"
        if request.app.state.managed_keys:
            try:
                await request.app.state.managed_keys.healthcheck()
            except Exception:
                components["firestore"] = "unavailable"
            else:
                components["firestore"] = "ready"
        status = "ready" if all(value == "ready" for value in components.values()) else "not_ready"
        return JSONResponse(
            status_code=200 if status == "ready" else 503,
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
                        "privacy_modes": model.privacy_modes,
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

    @app.post("/v1/{proxy_path:path}")
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

        billing = await _prepare_billing(request, route, payload, len(raw))
        if isinstance(billing, Response):
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
                ),
                status_code=upstream.status_code,
                media_type=content_type,
                headers=response_headers,
            )
        content = await upstream.aread()
        await upstream.aclose()
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
        logger.exception(
            json.dumps(
                {
                    "event": "rate_limiter_unavailable",
                    "request_id": request.state.request_id,
                }
            )
        )
        return openai_error(
            503,
            "Rate-limit service is unavailable",
            error_type="server_error",
            code="rate_limiter_unavailable",
        )
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
    route: ModelRoute,
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
    route: ModelRoute,
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
    route: ModelRoute,
    model: str,
    privacy: str,
    backend: str,
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
            yield chunk
    finally:
        if pending:
            usage = _usage_from_sse_line(pending) or usage
        await upstream.aclose()
        await _settle_billing(request, billing, route, model, privacy, backend, usage)


def _estimate_cost(route: ModelRoute, payload: dict[str, Any], request_bytes: int) -> int:
    prompt_upper_bound = max(1, request_bytes)
    requested_output = payload.get("max_completion_tokens", payload.get("max_tokens"))
    try:
        output_tokens = int(requested_output) if requested_output is not None else 2_048
    except (TypeError, ValueError):
        output_tokens = 2_048
    if route.max_context_length:
        output_tokens = min(max(1, output_tokens), route.max_context_length)
    return max(
        1,
        (
            prompt_upper_bound * route.input_price_microusd_per_million
            + output_tokens * route.output_price_microusd_per_million
            + 999_999
        )
        // 1_000_000,
    )


def _actual_cost(route: ModelRoute, usage: dict[str, Any]) -> int:
    prompt = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)))
    completion = int(usage.get("completion_tokens", usage.get("output_tokens", 0)))
    return max(
        1,
        (
            prompt * route.input_price_microusd_per_million
            + completion * route.output_price_microusd_per_million
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
    ):
        value = usage.get(key)
        if isinstance(value, int) and value >= 0:
            clean[key] = value
    return clean


def _select_backend(
    config: GatewayConfig,
    route: ModelRoute,
    preference: PrivacyPreference,
) -> tuple[Backend, str] | Response:
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
