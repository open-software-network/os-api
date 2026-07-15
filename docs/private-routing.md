# Private provider routing

OpenSoftware API is an OpenAI-compatible router for policy-enforced private inference. It does not
operate a GPU fleet in the hosted-provider deployment. Public model IDs map to one or more provider
endpoints and routing filters privacy and feature requirements before considering price or health.

## Privacy levels

| Level | Contract |
|---|---|
| `standard` | No confidential-compute guarantee. |
| `no-retention` | Provider policy commits to zero retention, without hardware proof. |
| `tee` | Execution is advertised inside CPU/GPU trusted execution environments. |
| `verified-tee` | Attestation was independently verified against an approved measurement policy. |
| `signed-output` | Output is cryptographically bound to an attested workload identity. |
| `e2ee-tee` | Client encrypts to a key bound to the model TEE; the router sees ciphertext. |

Levels are ordered minimum guarantees. Routing never labels evidence as verified merely because a
provider returned an evidence header. Evidence states are `present`, `missing`, `unverified`, or
`not-applicable`. A future verifier can promote an endpoint to `verified-tee` only after checking
the hardware chain, freshness, workload measurements, and key binding.

`X-Confidential-Compute: preferred` is the default. It requires zero retention and prioritizes a
configured Venice private endpoint, then falls back to a compatible Phala TEE endpoint. This is a
provider-policy privacy guarantee, not a confidential-compute guarantee. Set
`X-Confidential-Compute: required` when TEE execution is required; it can never downgrade to a
non-TEE endpoint. The structured policy provides finer control:

This is a deliberate breaking policy change from the earlier TEE-first default, approved on July
14, 2026. Existing callers that relied on an omitted header as an implicit hardware guarantee must
send `X-Confidential-Compute: required`. A default request does not fall below zero retention when
Venice and Phala are unavailable; ordinary standard fallback requires the caller to explicitly set
`privacy.allow_downgrade: true`.

```json
{
  "model": "openai/gpt-oss-20b",
  "messages": [{"role": "user", "content": "Hello"}],
  "privacy": {
    "minimum": "tee",
    "require_zero_retention": true,
    "require_response_proof": true,
    "allow_downgrade": false
  },
  "provider": {
    "sort": "price",
    "only": ["phala", "venice"],
    "allow_fallbacks": true,
    "require_parameters": true,
    "max_price": {"input": 0.10, "output": 0.50}
  }
}
```

Supported provider controls are `order`, `only`, `ignore`, `allow_fallbacks`,
`require_parameters`, `sort` (`priority`, `price`, `latency`, or `throughput`), `max_price`, and
`regions`. Tool, structured-output, and reasoning parameters automatically become required endpoint
capabilities. During Auto selection, non-text input modalities are mandatory even when
`provider.require_parameters` is false. Image-bearing chat content (`image_url` or `input_image`, as
a message content part or under top-level Responses `input`) requires an endpoint explicitly marked for vision. File parts
(`input_file` or `file`), audio parts (`input_audio` or `audio_url`), and non-empty attachments fail
closed until an endpoint is explicitly reviewed for the corresponding capability. Explicit models
that declare a modality also require endpoint-level support; models without that declaration retain
passthrough behavior for backward compatibility.

## Automatic model selection

Use the synthetic model ID `open-software/auto` (or `auto`) and set `auto.cost_quality` from `0`
(lowest estimated request cost) to `1` (highest configured quality). The router first removes models
that cannot satisfy the request's privacy, provider, price, capability, or context requirements. It
then ranks the remaining models using the request's estimated input/output token mix. Automatic
routing never weakens an explicit requirement. When `cost_quality` is omitted, the router defaults
to `1` so quality is preferred unless the caller explicitly chooses a lower-cost policy.

```json
{
  "model": "open-software/auto",
  "auto": {"cost_quality": 0.7},
  "messages": [{"role": "user", "content": "Analyze this meeting"}],
  "tools": [{"type": "function", "function": {"name": "save_note"}}],
  "privacy": {"minimum": "tee", "require_zero_retention": true}
}
```

The successful response identifies the actual canonical model in both its `model` field and the
`X-OS-Auto-Selected-Model` header. Quality scores are explicit, version-controlled catalog inputs,
not provider marketing labels. Models without a score are excluded from automatic selection. A
catalog release must update scores only from a documented evaluation snapshot and rerun the router
regression suite. Explicit model IDs remain available when reproducibility matters more than
automatic optimization.

For image input, Auto removes text-only models and endpoints before cost/quality ranking. The
initial reviewed vision route is Venice's private Kimi K2.6 endpoint. The Phala Kimi K2.6 endpoint
remains text-only in this catalog until its image-input support is independently confirmed. If
provider or privacy controls exclude every reviewed vision endpoint, the request fails with
`auto_route_unavailable` instead of sending the image to a text-only route.

Auto's routing-context heuristic substitutes bounded placeholders for non-text input payloads and
attachments. Large base64 data URLs therefore do not consume the text-token estimate, while each
non-text content part and each attachment adds a fixed 4,096-unit routing allowance. Multiple inputs
multiply that allowance. This heuristic is not used as a billing bound: authorization reserves
against the larger of the full raw request byte size and the selected model's entire context-token
capacity whenever non-text input is present. This covers both large inline data URLs and short
remote URLs whose visual token usage is not knowable before inference. Fallback candidates are
compared using this bound. Metered non-text requests fail closed if an eligible model has no
configured context capacity. Every chat or legacy-completions request has an enforced output bound:
omitting both output-limit fields adds a 2,048-token default. A valid explicit limit is forwarded
and the reservation is capped at the model's configured maximum. If both `max_completion_tokens`
and `max_tokens` are set, the gateway uses the larger positive integer and forwards one canonical
field (`max_completion_tokens` for chat, `max_tokens` for the legacy completions endpoint). Every
supplied output limit must be a positive, non-boolean integer; explicit `null` values are removed
and treated as omitted. Invalid single or dual values fail with a 400 response before any upstream
call. Embedding requests are not given a generation limit. This prevents an upstream provider from
honoring a value that the gateway's billing estimate parsed or clamped differently.
Nested media content is never rewritten. Settlement uses
billable prompt/completion usage reported by the upstream provider. Missing usage, `total_tokens`
alone, or zero-only token fields emit a warning and fall back to the bounded structural estimate
plus 4,096 units per non-text input and the 2,048-token default output estimate. That fallback never
exceeds or automatically charges the full-context reservation.

The initial score snapshot and its limitations are recorded in
[`auto-quality-policy.md`](auto-quality-policy.md). The slider is an ordinal product policy, not a
claim that one scalar captures every workload. Requests still filter on tools, structured outputs,
context, privacy, and provider constraints before quality ranking.

## Fallback rules

Fallbacks occur only between endpoints for the same canonical model. A timeout, connection error,
HTTP 408/409/425/429, or 5xx response may trigger the next endpoint. Customer errors are returned
without retry. Only the successful endpoint is billed.

`X-Confidential-Compute: required` and `privacy.allow_downgrade: false` fail closed and never weaken
the requested privacy guarantee. The default `preferred` policy attempts a configured
zero-retention Venice private endpoint first and then a compatible Phala TEE endpoint. The response
reports the actual privacy level and endpoint in headers. Set the header to `required` when
confidential compute is mandatory; client-encrypted Venice E2EE routes remain isolated from
plaintext fallback because their ciphertext and key agreement are endpoint-specific.

Hosted-provider responses expose the exact contract in `X-OS-Privacy-Level`: Venice private reports
`no-retention`, Phala reports `tee`, and an encrypted Venice route reports `e2ee-tee`. The legacy
`X-Compute-Privacy` header remains a coarse compute-class field, so Venice private reports
`standard`; clients must not interpret that field or the default header behavior as TEE evidence.

Semantic model fallback is opt-in through an ordered `models` array. Every model in the array is
independently filtered by the same privacy, provider, region, price, and capability policy. The
router reserves spend against the most expensive eligible endpoint, charges only the successful
one, and normalizes the response `model` field to the canonical model actually used. If `model` is
omitted, the first `models` entry is the primary model.

```json
{
  "models": ["openai/gpt-oss-120b", "openai/gpt-oss-20b"],
  "messages": [{"role": "user", "content": "Hello"}],
  "privacy": {"minimum": "tee", "allow_downgrade": false}
}
```

Three consecutive retryable failures open a process-local circuit for 30 seconds by default. The
thresholds are configurable with `ENDPOINT_FAILURE_THRESHOLD` and
`ENDPOINT_CIRCUIT_COOLDOWN_SECONDS`. Endpoint status and observed latency/throughput are available
from `GET /v1/models/{model}/endpoints`.

## Evidence and receipts

Successful responses include:

```text
X-OS-Provider
X-OS-Endpoint
X-OS-Privacy-Level
X-OS-Attestation-Status
X-OS-Receipt-ID
X-OS-Generation-ID
```

`GET /v1/privacy/receipts/{generation_id}` returns normalized evidence metadata. Managed
deployments persist receipts in a tenant-scoped Firestore collection; prompts and completions are
not stored. Raw provider evidence is intentionally not described as verified until a cryptographic
verifier has checked it.

Phala responses are associated with ACI receipt, workload-identity, and keyset-digest headers.
Venice E2EE endpoints require the client to supply the three `X-Venice-TEE-*` encryption headers;
the router forwards only this explicit allowlist. Endpoints marked `requires_client_encryption`
are ineligible without those headers, preventing accidental plaintext submission to an E2EE model.
Before forwarding ciphertext, the router fetches a fresh Venice attestation with a 32-byte nonce,
requires Venice's attestation result to be successful, verifies the returned nonce, and compares the
attested signing key to the model public key selected by the client. A mismatch fails closed. This
is a freshness and key-binding check using Venice's server-side verifier; it is not represented as
independent hardware-quote verification.

For web scrape compatibility, os-api performs an HTTPS/public-address DNS preflight and Venice
independently resolves and rejects private or reserved targets at fetch time. The latter enforcement
is required because a preflight alone is not connection-bound and cannot prevent DNS rebinding. The
July 12, 2026 canary explicitly verified Venice rejects loopback and link-local metadata targets
with `400`; reverify this contract before changing augmentation providers.

June's video compatibility path forwards Venice quote and queue JSON and preserves retrieve's
polymorphic response: JSON while a job is processing, or a streamed video body when complete.
Quote/retrieve requests are bounded at 1 MiB, queue requests at 31 MiB, and provider JSON at 1 MiB.
Only four queue request bodies are admitted concurrently per instance, and decoded validation
objects are released before forwarding to keep large base64 animation frames memory-bounded.
The 101 MiB streaming ceiling sits above June's terminal 100 MiB limit so June classifies an
oversized chunked result before the proxy's defense-in-depth cap. Animation source payloads above
31 MiB require an upload-by-reference design because base64 expansion can exceed Cloud Run's
HTTP/1 ingress ceiling. Compatibility
service keys keep `X-OS-Billing-Owner: caller`, so June remains the sole billing owner.

## Catalog operations

`config/models.yaml` contains only the reviewed models eligible for Auto. At runtime the gateway
fetches Venice's and Phala's OpenAI-compatible `/v1/models` endpoints, adds every Venice text model
and every Phala TEE model under its provider-native ID, and refreshes that union every five minutes.
An explicit unknown ID triggers a rate-limited refresh before returning `model_not_found`, so newly
added and previously saved Venice IDs do not require an os-api release. A failed refresh keeps the
last successful in-memory catalog; pricing must be present and positive before a live model becomes
routable. `scripts/validate_provider_catalog.py` still gates the curated Auto routes before release.

Provider secrets are read only from environment variables backed by Secret Manager in production.
If an enabled provider declares `api_key_env` and the key is absent, `/readyz` returns 503.

## Adding a provider

1. Implement a provider adapter in `providers.py` for request normalization and evidence capture.
2. Add the provider and endpoints to the catalog with exact public prices and conservative privacy
   claims.
3. Extend the live catalog validator.
4. Add fallback, error, streaming, evidence, billing, and capability tests.
5. Add the API key to Secret Manager and runtime IAM before enabling the endpoint.

Provider integrations must not log request or response bodies. Debug modes that expose transformed
messages are forbidden in production.

## Venice compatibility for June

The production compatibility surface implements Venice endpoints consumed by June API: typed model
catalogs, audio transcription, augment search/scrape, and JSON image generation. It is guarded by
`VENICE_COMPAT_ENABLED=true`, and only static service API keys may call it. These responses declare
`X-OS-Billing-Owner: caller`, so June remains the single billing authority during a reversible
migration. See `docs/os-june-venice-migration.md` for the completed sequence and rollback boundary.
