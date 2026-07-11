# OpenSoftware API

OpenSoftware API is an OpenAI-compatible router for verifiably private model inference. It routes
each request across hosted TEE/E2EE providers such as Phala and Venice, enforces a fail-closed
privacy policy, normalizes provider behavior, performs same-model fallbacks, and exposes evidence
and the selected privacy class on every response. Self-hosted GCP GPU endpoints remain supported as
another provider type, but are not required for the hosted-provider deployment.

This repository is an original infrastructure implementation. It does not copy Venice.ai code, private APIs, branding, or product UI.

## What works

- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/completions`
- `POST /v1/embeddings`
- SSE streaming without buffering prompts or generated tokens
- OpenSoftware Accounts login with PKCE and server-side rotating sessions
- HTTP Basic protection for the human-facing site without changing API-key authentication
- Firestore-backed API keys with expiration, revocation, hard spend limits, and usage history
- OpenSoftware Credit wallet authorization and idempotent settlement
- SHA-256-hashed credentials and Redis-backed request limits
- Per-model standard/confidential routing with a no-downgrade guarantee
- Multi-provider endpoint routing by privacy, capabilities, price, health, and region
- Same-model fallbacks with circuit breaking and charge-on-success billing
- Tenant-scoped normalized privacy receipts and provider evidence metadata
- vLLM backends and GKE manifests for L4 and confidential H100 nodes
- Terraform for a private GKE cluster, GPU node pools, VPC/NAT, Artifact Registry, Memorystore, and Secret Manager
- Live global HTTPS load balancer, Certificate Manager, Cloud Armor, uptime checks, and billing/5xx alerts; direct Cloud Run ingress is blocked
- Keyless GitHub deployments with a zero-traffic candidate smoke test and digest-pinned promotion
- Dependency auditing, a lockfile-enforced container build, liveness probes, and warm gateway capacity

The standard L4 path and strict confidential routing on an Intel TDX node have both been exercised against real GCP resources; see [the live test record](docs/live-test.md). The production H100 + TDX path remains quota-gated.

The API, developer dashboard, and docs are published at `https://api.opensoftware.co`. See [public API usage](docs/public-api.md) for the endpoint, model name, OpenAI client example, and shutdown procedure.

The API does not persist prompts or responses. That is a behavior of this gateway, not a blanket guarantee about every GCP service or a model container you replace it with; see [the threat model](docs/threat-model.md).

## API privacy contract

Set one request header:

| Header | Behavior |
| --- | --- |
| `X-Confidential-Compute: required` | Use only a configured TEE backend; return `409` instead of downgrading. |
| `X-Confidential-Compute: preferred` | Use TEE when configured, otherwise explicitly return `standard-fallback`. This is the default. |
| `X-Confidential-Compute: disabled` | Use the lower-cost standard GPU pool only. |

Responses include `X-Compute-Privacy` and `X-Model-Backend`. A production edge proxy must preserve these headers.

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Confidential-Compute: required" \
  -d '{
    "model": "llama-3.1-8b-instruct",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": true
  }'
```

## Local development

Python 3.11+ and `uv` are the shortest path:

```bash
uv sync --extra dev
export API_KEY_HASHES="local=$(printf 'sk-local' | shasum -a 256 | cut -d ' ' -f 1)"
export MODEL_CONFIG_PATH=config/models.yaml
uv run uvicorn sovereign_gateway.app:app --port 8080 --no-access-log
```

The default catalog expects `PHALA_API_KEY` and `VENICE_API_KEY`. Production readiness fails closed
when an enabled provider credential is absent. The legacy self-hosted profile is still available on
a Linux NVIDIA host with `docker compose --profile gpu up --build`. On a laptop, run the test suite,
which uses in-process OpenAI-compatible mocks:

```bash
uv run --extra dev pytest
uv run --extra dev ruff check .
```

Create a real key with `python scripts/create_api_key.py TENANT_ID`; put only the printed hash entry in `API_KEY_HASHES`.

## GCP deployment

1. Review [architecture](docs/architecture.md), [threat model](docs/threat-model.md), and [operations](docs/operations.md).
2. Review [private provider routing](docs/private-routing.md), including the privacy taxonomy and
   E2EE boundary.
3. Add hosted-provider API keys to Secret Manager, or obtain GPU quota when enabling self-hosted endpoints.
4. Build and push the gateway image to the Artifact Registry repository created by Terraform.
5. Run Terraform from `deploy/terraform`.
6. Create the Kubernetes secrets described in `deploy/kubernetes/base/secrets.example.yaml`; never commit their values.
7. Apply the appropriate hosted-provider or self-hosted Kubernetes overlay when using GKE.
8. Enable the global HTTPS load balancer and complete the Certificate Manager DNS authorization before restricting Cloud Run ingress.

The confidential node pool is disabled by default because GCP rejects creation without zone-specific preemptible H100 quota. Enable it only after quota and capacity are confirmed:

```hcl
enable_confidential_gpu_pool = true
confidential_gpu_zone        = "a-supported-zone"
```

For a lower-cost validation of TDX scheduling and fail-closed API routing, Terraform also includes an optional scale-to-zero `c3-standard-4` confidential CPU pool. The `standard-smoke` overlay can route the small Qwen smoke model to it. This proves the gateway contract and TDX placement, but it is not an H100 performance substitute.

## Remaining production boundaries

The first control plane is live, including login, key lifecycle, hard spend limits, usage metering, and wallet settlement. The remaining launch controls and measurable exit criteria are tracked in the [production parity checklist](docs/production-parity.md) and sequenced in [operations](docs/operations.md).
