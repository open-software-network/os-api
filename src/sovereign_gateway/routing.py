from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from .config import PRIVACY_LEVELS, Endpoint, GatewayConfig, ModelRoute


class RoutingError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PrivacyPolicy:
    minimum: str = "no-retention"
    require_zero_retention: bool = False
    require_response_proof: bool = False
    allow_downgrade: bool = False
    confidential_disabled: bool = False


@dataclass(frozen=True)
class ProviderPolicy:
    order: tuple[str, ...] = ()
    only: tuple[str, ...] = ()
    ignore: tuple[str, ...] = ()
    allow_fallbacks: bool = True
    require_parameters: bool = True
    sort: Literal["price", "latency", "throughput", "priority"] = "priority"
    max_input_price_microusd_per_million: int | None = None
    max_output_price_microusd_per_million: int | None = None
    regions: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoutingPolicy:
    privacy: PrivacyPolicy
    provider: ProviderPolicy
    required_capabilities: frozenset[str] = frozenset()
    client_encrypted: bool = False
    automatic: bool = False


AUTO_MODEL_IDS = frozenset({"auto", "open-software/auto"})
INPUT_CAPABILITIES_BY_CONTENT_TYPE = {
    "image_url": "vision",
    "input_image": "vision",
    "input_file": "file_input",
    "file": "file_input",
    "input_audio": "audio_input",
    "audio_url": "audio_input",
}
INPUT_CAPABILITIES_BY_MODALITY = {
    "image": "vision",
    "audio": "audio_input",
    "file": "file_input",
}
MANDATORY_INPUT_CAPABILITIES = frozenset(
    {"attachments", *INPUT_CAPABILITIES_BY_CONTENT_TYPE.values()}
)
NON_TEXT_INPUT_PLACEHOLDER = "<non-text input omitted>"
# Auto uses UTF-8 bytes as a conservative tokenizer-independent bound for
# text. Non-text inputs add a fixed 4K heuristic allowance for routing and
# missing-usage fallback charges instead of treating raw base64 bytes as text
# tokens. This is not a billing authorization bound.
NON_TEXT_INPUT_HEURISTIC_ALLOWANCE = 4_096


def select_auto_models(
    config: GatewayConfig,
    policy: RoutingPolicy,
    health: EndpointHealth,
    payload: dict[str, Any],
) -> list[str]:
    """Rank privacy- and capability-eligible models on a cost/quality frontier."""
    raw = payload.get("auto", {}) or {}
    if not isinstance(raw, dict):
        raise RoutingError("invalid_auto_policy", "auto must be an object")
    value = raw.get("cost_quality", payload.get("cost_quality", 1.0))
    try:
        quality_weight = float(value)
    except (TypeError, ValueError) as exc:
        raise RoutingError("invalid_auto_policy", "auto.cost_quality must be a number") from exc
    if not 0 <= quality_weight <= 1:
        raise RoutingError("invalid_auto_policy", "auto.cost_quality must be between 0 and 1")

    # UTF-8 bytes are a conservative tokenizer-independent upper bound for
    # byte-fallback model tokenizers, including CJK and large tool schemas.
    prompt_tokens = estimate_bounded_prompt_units(payload)
    requested_output = payload.get("max_completion_tokens")
    if requested_output is None:
        requested_output = payload.get("max_tokens", 2_048)
    try:
        output_tokens = max(1, int(requested_output))
    except (TypeError, ValueError):
        output_tokens = 2048
    required_context = prompt_tokens + output_tokens
    ranked: list[tuple[str, float, float]] = []
    for model_id, model in config.models.items():
        if model.quality_score is None:
            continue
        if model.max_context_length is not None and model.max_context_length < required_context:
            continue
        if model.max_output_length is not None and model.max_output_length < output_tokens:
            continue
        try:
            # Scoring is read-only. The selected model's real routing pass is
            # responsible for claiming any half-open circuit probe.
            endpoints = _route_endpoints(
                config,
                model_id,
                policy,
                health,
                claim_half_open=False,
            )
        except RoutingError:
            continue
        # route_endpoints has already applied the caller's requested provider
        # ordering. Estimate the endpoint that will actually be attempted
        # first; using a cheaper fallback here would make the cost end of the
        # slider select a model whose normal execution costs more than scored.
        selected = endpoints[0]
        cost = (
            prompt_tokens * selected.input_price_microusd_per_million
            + output_tokens * selected.output_price_microusd_per_million
        )
        ranked.append((model_id, model.quality_score, float(cost)))
    if not ranked:
        capability_detail = ""
        if policy.required_capabilities:
            capability_detail = (
                f" with required capabilities ({', '.join(sorted(policy.required_capabilities))})"
            )
        raise RoutingError(
            "auto_route_unavailable",
            f"No scored model has an endpoint{capability_detail} satisfying the request policy "
            "and context",
        )
    qualities = [item[1] for item in ranked]
    costs = [item[2] for item in ranked]
    q_min, q_max = min(qualities), max(qualities)
    c_min, c_max = min(costs), max(costs)

    def score(item: tuple[str, float, float]) -> tuple[float, float, str]:
        model_id, quality, cost = item
        normalized_quality = 1 if q_max == q_min else (quality - q_min) / (q_max - q_min)
        normalized_cost = 0 if c_max == c_min else (cost - c_min) / (c_max - c_min)
        utility = quality_weight * normalized_quality - (1 - quality_weight) * normalized_cost
        return (-utility, cost, model_id)

    return [item[0] for item in sorted(ranked, key=score)]


@dataclass
class EndpointStats:
    attempts: int = 0
    successes: int = 0
    consecutive_failures: int = 0
    circuit_open_until: float = 0
    half_open_probe_until: float = 0
    latencies: deque[float] = field(default_factory=lambda: deque(maxlen=256))
    throughputs: deque[float] = field(default_factory=lambda: deque(maxlen=256))

    @property
    def healthy(self) -> bool:
        """Report only a fully closed circuit; half-open probes are not yet healthy."""

        return self.circuit_open_until == 0

    @property
    def success_rate(self) -> float:
        return self.successes / self.attempts if self.attempts else 1.0

    @property
    def latency(self) -> float:
        return sum(self.latencies) / len(self.latencies) if self.latencies else float("inf")

    @property
    def throughput(self) -> float:
        return sum(self.throughputs) / len(self.throughputs) if self.throughputs else 0.0


class EndpointHealth:
    """Process-local fast health state.

    Provider failures are also emitted as structured logs. This state prevents
    repeatedly selecting a known-bad endpoint within one gateway instance;
    managed deployments can replace it with shared Redis state later without
    changing routing semantics.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30,
        half_open_probe_seconds: float = 10,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be positive")
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds cannot be negative")
        if half_open_probe_seconds <= 0:
            raise ValueError("half_open_probe_seconds must be positive")
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.half_open_probe_seconds = half_open_probe_seconds
        self._clock = clock
        self._stats: dict[str, EndpointStats] = defaultdict(EndpointStats)
        # route_endpoints is synchronous while success/failure are async. A
        # process-local mutex keeps half-open claims atomic across event-loop
        # tasks and worker threads without requiring an await in the router.
        self._lock = threading.Lock()

    def get(self, endpoint_id: str) -> EndpointStats:
        with self._lock:
            stats = self._stats[endpoint_id]
            return EndpointStats(
                attempts=stats.attempts,
                successes=stats.successes,
                consecutive_failures=stats.consecutive_failures,
                circuit_open_until=stats.circuit_open_until,
                half_open_probe_until=stats.half_open_probe_until,
                latencies=deque(stats.latencies, maxlen=stats.latencies.maxlen),
                throughputs=deque(stats.throughputs, maxlen=stats.throughputs.maxlen),
            )

    def available_endpoint_ids(
        self,
        endpoint_ids: Iterable[str],
        *,
        claim_half_open: bool,
    ) -> set[str]:
        """Return closed endpoints and at most one leased probe per open circuit."""
        now = self._clock()
        available: set[str] = set()
        with self._lock:
            for endpoint_id in endpoint_ids:
                stats = self._stats[endpoint_id]
                if stats.circuit_open_until == 0:
                    available.add(endpoint_id)
                    continue
                if now < stats.circuit_open_until:
                    continue
                if stats.half_open_probe_until > now:
                    continue
                if claim_half_open:
                    stats.half_open_probe_until = now + self.half_open_probe_seconds
                available.add(endpoint_id)
        return available

    async def success(
        self, endpoint_id: str, latency_seconds: float, output_tokens: int | None = None
    ) -> None:
        with self._lock:
            stats = self._stats[endpoint_id]
            stats.attempts += 1
            stats.successes += 1
            stats.consecutive_failures = 0
            stats.circuit_open_until = 0
            stats.half_open_probe_until = 0
            stats.latencies.append(latency_seconds)
            if output_tokens and latency_seconds > 0:
                stats.throughputs.append(output_tokens / latency_seconds)

    async def failure(self, endpoint_id: str) -> None:
        with self._lock:
            stats = self._stats[endpoint_id]
            stats.attempts += 1
            stats.consecutive_failures += 1
            if stats.consecutive_failures >= self.failure_threshold:
                stats.circuit_open_until = self._clock() + self.cooldown_seconds
                stats.half_open_probe_until = 0


def parse_routing_policy(
    payload: dict[str, Any],
    confidential_header: str | None,
    client_encrypted: bool = False,
) -> RoutingPolicy:
    privacy_raw = payload.get("privacy", {})
    if privacy_raw is None:
        privacy_raw = {}
    if not isinstance(privacy_raw, dict):
        raise RoutingError("invalid_privacy_policy", "privacy must be an object")

    header = (confidential_header or "preferred").lower()
    if header not in {"required", "preferred", "disabled"}:
        raise RoutingError(
            "invalid_privacy_preference",
            "X-Confidential-Compute must be required, preferred, or disabled",
        )
    if header == "disabled":
        default_minimum = "standard"
    elif header == "required":
        default_minimum = "tee"
    else:
        default_minimum = "no-retention"
    minimum = str(privacy_raw.get("minimum", default_minimum))
    if minimum not in PRIVACY_LEVELS:
        raise RoutingError("invalid_privacy_policy", f"Unknown privacy level {minimum!r}")
    # A caller must explicitly authorize routing below its requested privacy
    # floor. "preferred" selects the product default; it is not consent to
    # abandon zero retention when those routes are unavailable.
    allow_downgrade = _policy_boolean(
        privacy_raw,
        "allow_downgrade",
        default=False,
        section="privacy",
    )
    # "required" is an unconditional fail-closed override.
    if header == "required":
        allow_downgrade = False
        if PRIVACY_LEVELS[minimum] < PRIVACY_LEVELS["tee"]:
            minimum = "tee"
    if header == "disabled":
        allow_downgrade = False

    provider_raw = payload.get("provider", {})
    if provider_raw is None:
        provider_raw = {}
    if not isinstance(provider_raw, dict):
        raise RoutingError("invalid_provider_policy", "provider must be an object")
    sort_raw = provider_raw.get("sort", "priority")
    sort_by = sort_raw.get("by", "priority") if isinstance(sort_raw, dict) else sort_raw
    if sort_by not in {"price", "latency", "throughput", "priority"}:
        raise RoutingError("invalid_provider_policy", f"Unknown provider sort {sort_by!r}")
    max_price = provider_raw.get("max_price", {}) or {}
    if not isinstance(max_price, dict):
        raise RoutingError("invalid_provider_policy", "provider.max_price must be an object")

    required = _infer_capabilities(payload)
    return RoutingPolicy(
        privacy=PrivacyPolicy(
            minimum=minimum,
            require_zero_retention=_policy_boolean(
                privacy_raw,
                "require_zero_retention",
                default=False,
                section="privacy",
            ),
            require_response_proof=_policy_boolean(
                privacy_raw,
                "require_response_proof",
                default=False,
                section="privacy",
            ),
            allow_downgrade=allow_downgrade,
            confidential_disabled=header == "disabled",
        ),
        provider=ProviderPolicy(
            order=_strings(provider_raw.get("order", []), "provider.order"),
            only=_strings(provider_raw.get("only", []), "provider.only"),
            ignore=_strings(provider_raw.get("ignore", []), "provider.ignore"),
            allow_fallbacks=_policy_boolean(
                provider_raw,
                "allow_fallbacks",
                default=True,
                section="provider",
            ),
            require_parameters=_policy_boolean(
                provider_raw,
                "require_parameters",
                default=True,
                section="provider",
            ),
            sort=sort_by,
            max_input_price_microusd_per_million=_usd_price(max_price.get("input")),
            max_output_price_microusd_per_million=_usd_price(max_price.get("output")),
            regions=_strings(provider_raw.get("regions", []), "provider.regions"),
        ),
        required_capabilities=required,
        client_encrypted=client_encrypted,
        automatic=payload.get("model") in AUTO_MODEL_IDS,
    )


def route_endpoints(
    config: GatewayConfig,
    model_id: str,
    policy: RoutingPolicy,
    health: EndpointHealth,
) -> list[Endpoint]:
    return _route_endpoints(
        config,
        model_id,
        policy,
        health,
        claim_half_open=True,
    )


def _route_endpoints(
    config: GatewayConfig,
    model_id: str,
    policy: RoutingPolicy,
    health: EndpointHealth,
    *,
    claim_half_open: bool,
) -> list[Endpoint]:
    candidates = config.endpoints_for(model_id)
    candidates = [
        endpoint
        for endpoint in candidates
        if not endpoint.requires_client_encryption or policy.client_encrypted
    ]
    if policy.client_encrypted:
        # Client ciphertext and key-agreement headers are provider/model
        # specific. Never forward them to a plaintext or different endpoint.
        candidates = [endpoint for endpoint in candidates if endpoint.requires_client_encryption]
    provider_policy = policy.provider
    if provider_policy.only:
        candidates = [
            endpoint for endpoint in candidates if endpoint.provider in provider_policy.only
        ]
    if provider_policy.ignore:
        candidates = [
            endpoint for endpoint in candidates if endpoint.provider not in provider_policy.ignore
        ]
    if provider_policy.regions:
        allowed_regions = set(provider_policy.regions)
        candidates = [
            endpoint
            for endpoint in candidates
            if allowed_regions.intersection(
                endpoint.regions or config.providers[endpoint.provider].regions
            )
        ]
    candidates = [endpoint for endpoint in candidates if _privacy_matches(endpoint, policy)]
    input_capabilities = policy.required_capabilities.intersection(MANDATORY_INPUT_CAPABILITIES)
    if policy.automatic:
        enforced_input_capabilities = input_capabilities
    else:
        # Preserve passthrough for older explicit models that do not declare
        # the modality. Once a model declares it, enforce endpoint-level
        # support because provider deployments can still differ.
        enforced_input_capabilities = input_capabilities.intersection(
            _declared_model_input_capabilities(config.models[model_id])
        )
    required_capabilities = (policy.required_capabilities - input_capabilities).union(
        enforced_input_capabilities
    )
    if not provider_policy.require_parameters:
        # Input modalities are not optional request parameters. Non-text input
        # must never be sent to an endpoint that has not declared support when
        # Auto or the explicitly selected model promises that modality.
        required_capabilities = enforced_input_capabilities
    if required_capabilities:
        candidates = [
            endpoint
            for endpoint in candidates
            if _supports_capabilities(
                endpoint,
                config.models[model_id].capabilities,
                required_capabilities,
            )
        ]
    if provider_policy.max_input_price_microusd_per_million is not None:
        candidates = [
            endpoint
            for endpoint in candidates
            if endpoint.input_price_microusd_per_million
            <= provider_policy.max_input_price_microusd_per_million
        ]
    if provider_policy.max_output_price_microusd_per_million is not None:
        candidates = [
            endpoint
            for endpoint in candidates
            if endpoint.output_price_microusd_per_million
            <= provider_policy.max_output_price_microusd_per_million
        ]
    if policy.privacy.allow_downgrade and not policy.client_encrypted:
        downgrade_policy = RoutingPolicy(
            privacy=PrivacyPolicy(
                minimum="standard",
                require_zero_retention=policy.privacy.require_zero_retention,
                require_response_proof=policy.privacy.require_response_proof,
                allow_downgrade=False,
                confidential_disabled=True,
            ),
            provider=policy.provider,
            required_capabilities=policy.required_capabilities,
            automatic=policy.automatic,
        )
        try:
            # The outer routing pass claims probes once after merging the
            # normal and explicitly authorized downgrade candidates.
            downgrade = _route_endpoints(
                config,
                model_id,
                downgrade_policy,
                health,
                claim_half_open=False,
            )
        except RoutingError:
            downgrade = []
        seen = {endpoint.id for endpoint in candidates}
        candidates.extend(endpoint for endpoint in downgrade if endpoint.id not in seen)
    if not candidates:
        raise RoutingError(
            "privacy_requirements_unavailable",
            f"Model {model_id!r} has no endpoint satisfying the request policy",
        )

    available_ids = health.available_endpoint_ids(
        (endpoint.id for endpoint in candidates),
        claim_half_open=claim_half_open,
    )
    candidates = [endpoint for endpoint in candidates if endpoint.id in available_ids]
    if not candidates:
        raise RoutingError(
            "all_endpoints_unavailable",
            f"All eligible endpoints for model {model_id!r} have open circuits",
        )

    order_index = {provider: index for index, provider in enumerate(provider_policy.order)}

    def key(endpoint: Endpoint) -> tuple[Any, ...]:
        stats = health.get(endpoint.id)
        explicit_rank = order_index.get(endpoint.provider, len(order_index))
        if provider_policy.sort == "price":
            metric: float = (
                endpoint.input_price_microusd_per_million
                + endpoint.output_price_microusd_per_million
            )
        elif provider_policy.sort == "latency":
            metric = stats.latency
        elif provider_policy.sort == "throughput":
            metric = -stats.throughput
        else:
            metric = endpoint.priority
        return explicit_rank, metric, -stats.success_rate, endpoint.id

    candidates.sort(key=key)
    return candidates if provider_policy.allow_fallbacks else candidates[:1]


def _privacy_matches(endpoint: Endpoint, policy: RoutingPolicy) -> bool:
    privacy = policy.privacy
    if privacy.confidential_disabled and endpoint.confidential:
        return False
    if not _satisfies_minimum_privacy(endpoint, privacy.minimum):
        return False
    if privacy.require_zero_retention and endpoint.privacy.retention != "zero":
        return False
    if privacy.require_response_proof and endpoint.privacy.attestation in {"none", "gateway"}:
        return False
    return True


def _satisfies_minimum_privacy(endpoint: Endpoint, minimum: str) -> bool:
    level = endpoint.privacy.level
    if minimum == "standard":
        return True
    if minimum == "no-retention":
        return endpoint.privacy.retention == "zero" or level == "no-retention"
    if minimum == "tee":
        return level in {"tee", "verified-tee", "signed-output", "e2ee-tee"}
    if minimum == "verified-tee":
        return level in {"verified-tee", "signed-output"}
    if minimum == "signed-output":
        return level == "signed-output"
    if minimum == "e2ee-tee":
        return level == "e2ee-tee"
    return False


def _infer_capabilities(payload: dict[str, Any]) -> frozenset[str]:
    capabilities = set(_recognized_non_text_inputs(payload))
    if payload.get("tools"):
        capabilities.add("tools")
    if payload.get("tool_choice") is not None:
        capabilities.add("tool_choice")
    if payload.get("response_format") is not None:
        capabilities.add("structured_outputs")
    if payload.get("reasoning") is not None or payload.get("reasoning_effort") is not None:
        capabilities.add("reasoning")
    return frozenset(capabilities)


def _recognized_non_text_inputs(payload: dict[str, Any]) -> tuple[str, ...]:
    capabilities: list[str] = []
    if payload.get("attachments"):
        capabilities.extend(["attachments"] * _attachment_count(payload["attachments"]))
    capabilities.extend(_content_non_text_inputs(payload.get("input")))
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return tuple(capabilities)
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("attachments"):
            capabilities.extend(["attachments"] * _attachment_count(message["attachments"]))
        capabilities.extend(_content_non_text_inputs(message.get("content")))
    return tuple(capabilities)


def _content_non_text_inputs(value: Any) -> list[str]:
    if isinstance(value, list):
        return [capability for item in value for capability in _content_non_text_inputs(item)]
    if not isinstance(value, dict):
        return []
    capability = INPUT_CAPABILITIES_BY_CONTENT_TYPE.get(value.get("type"))
    if capability:
        return [capability]
    capabilities: list[str] = []
    if value.get("attachments"):
        capabilities.extend(["attachments"] * _attachment_count(value["attachments"]))
    capabilities.extend(_content_non_text_inputs(value.get("content")))
    return capabilities


def _declared_model_input_capabilities(model: ModelRoute) -> frozenset[str]:
    capabilities = set(model.capabilities).intersection(MANDATORY_INPUT_CAPABILITIES)
    for modality in model.input_modalities:
        capability = INPUT_CAPABILITIES_BY_MODALITY.get(modality)
        if capability:
            capabilities.add(capability)
    return frozenset(capabilities)


def estimate_bounded_prompt_units(payload: dict[str, Any]) -> int:
    """Estimate routing or fallback usage without counting encoded media as text."""
    estimation_payload = _payload_for_token_estimation(payload)
    text_upper_bound = len(json.dumps(estimation_payload, separators=(",", ":")).encode("utf-8"))
    non_text_allowance = (
        len(_recognized_non_text_inputs(payload)) * NON_TEXT_INPUT_HEURISTIC_ALLOWANCE
    )
    return max(1, text_upper_bound + non_text_allowance)


def has_non_text_input(payload: dict[str, Any]) -> bool:
    return bool(_recognized_non_text_inputs(payload))


def _payload_for_token_estimation(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = _sanitize_estimation_value(payload)
    return sanitized if isinstance(sanitized, dict) else {}


def _sanitize_estimation_value(value: Any) -> Any:
    if isinstance(value, dict):
        content_type = value.get("type")
        if content_type in INPUT_CAPABILITIES_BY_CONTENT_TYPE:
            return {"type": content_type, "input": NON_TEXT_INPUT_PLACEHOLDER}
        return {
            key: (
                _attachment_placeholder(item)
                if key == "attachments" and item
                else _sanitize_estimation_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_estimation_value(item) for item in value]
    return value


def _attachment_placeholder(value: Any) -> dict[str, Any]:
    count = len(value) if isinstance(value, (list, tuple)) else 1
    return {"count": count, "input": NON_TEXT_INPUT_PLACEHOLDER}


def _attachment_count(value: Any) -> int:
    return len(value) if isinstance(value, (list, tuple)) else 1


def _supports_capabilities(
    endpoint: Endpoint,
    model_capabilities: frozenset[str],
    required_capabilities: frozenset[str],
) -> bool:
    # Input modalities are endpoint-specific: a multimodal model may be
    # exposed through another provider or deployment that accepts text only.
    # Other capability declarations retain the legacy model-level fallback.
    input_capabilities = required_capabilities.intersection(MANDATORY_INPUT_CAPABILITIES)
    if not input_capabilities.issubset(endpoint.capabilities):
        return False
    remaining = required_capabilities - MANDATORY_INPUT_CAPABILITIES
    return remaining.issubset(endpoint.capabilities or model_capabilities)


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise RoutingError("invalid_provider_policy", f"{name} must be a list of strings")
    return tuple(value)


def _policy_boolean(
    policy: dict[str, Any],
    field: str,
    *,
    default: bool,
    section: Literal["privacy", "provider"],
) -> bool:
    if field not in policy:
        return default
    value = policy[field]
    if not isinstance(value, bool):
        raise RoutingError(
            f"invalid_{section}_policy",
            f"{section}.{field} must be a boolean",
        )
    return value


def _usd_price(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise RoutingError("invalid_provider_policy", "max_price values must be numbers") from exc
    if parsed < 0:
        raise RoutingError("invalid_provider_policy", "max_price values cannot be negative")
    return round(parsed * 1_000_000)
