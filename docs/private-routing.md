# Private provider routing

OpenSoftware API is an OpenAI-compatible router for verifiably private inference. It does not
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

`X-Confidential-Compute: required` remains supported and can never downgrade to a non-TEE endpoint.
The structured policy provides finer control:

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
capabilities.

## Fallback rules

Fallbacks occur only between endpoints for the same canonical model. A timeout, connection error,
HTTP 408/409/425/429, or 5xx response may trigger the next endpoint. Customer errors are returned
without retry. Privacy filters are applied before the attempt list is constructed, so a fallback
cannot weaken the requested privacy guarantee. Only the successful endpoint is billed.

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

## Catalog operations

The catalog in `config/models.yaml` is fail-fast validated at application startup. Before every
release, `scripts/validate_provider_catalog.py` queries the providers' public model catalogs and
fails if a model disappears, a TEE/E2EE capability is removed, or configured pass-through pricing
differs from the live price.

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
