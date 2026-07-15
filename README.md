# OpenSoftware API

OpenSoftware API is an OpenAI-compatible router for policy-enforced private model inference. It routes
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
- SSE streaming without buffering the full generated response
- OpenSoftware Accounts login with PKCE and rotating HttpOnly session cookies
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
- Optional Intel TDX Confidential Space gateway with digest-bound runtime secret release and public nonce-bound Google attestation

The standard L4 path and strict confidential routing on an Intel TDX node have both been exercised against real GCP resources; see [the live test record](https://github.com/open-software-network/os-api/blob/main/docs/live-test.md). The production H100 + TDX path remains quota-gated.

The access-controlled API, developer dashboard, and docs are published at `https://api.opensoftware.co`. See [public API usage](https://github.com/open-software-network/os-api/blob/main/docs/public-api.md) for endpoint discovery and OpenAI client examples.

The API does not persist prompts or responses. That is a behavior of this gateway, not a blanket guarantee about every GCP service or a model container you replace it with; see [the threat model](https://github.com/open-software-network/os-api/blob/main/docs/threat-model.md) and [Confidential Space gateway](https://github.com/open-software-network/os-api/blob/main/docs/confidential-gateway.md).

## API privacy contract

Set one request header:

| Header | Behavior |
| --- | --- |
| `X-Confidential-Compute: required` | Use only a configured TEE backend; return `409` instead of downgrading. |
| `X-Confidential-Compute: preferred` | Require zero retention, prefer the configured private endpoint, and fall back to a compatible TEE endpoint. This is the default. |
| `X-Confidential-Compute: disabled` | Use the lower-cost standard GPU pool only. |

Responses include `X-Compute-Privacy` and `X-Model-Backend`. A production edge proxy must preserve these headers.
Chat and legacy-completions requests always carry an explicit output bound. When neither OpenAI
output-limit field is supplied, the gateway applies a 2,048-token default; embeddings are unchanged.
The default was deliberately changed from TEE-first to Venice-private-first on July 14, 2026. This
is a breaking privacy-policy choice: callers that require a hardware execution guarantee must send
`X-Confidential-Compute: required` rather than infer TEE use from an omitted header. For hosted
providers, `X-OS-Privacy-Level` reports the precise level (`no-retention`, `tee`, or `e2ee-tee`),
while the legacy `X-Compute-Privacy` header reports the coarser compute class.

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Confidential-Compute: required" \
  -d '{
    "model": "openai/gpt-oss-20b",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": true
  }'
```

## Local development

Python 3.11+ and `uv` are the shortest path:

```bash
uv sync --locked --extra dev
export API_KEY_HASHES="local=$(printf 'sk-local' | shasum -a 256 | cut -d ' ' -f 1)"
export API_KEY=sk-local
export MODEL_CONFIG_PATH=config/models.yaml
uv run uvicorn sovereign_gateway.app:app --port 8080 --no-access-log
```

The default catalog expects `PHALA_API_KEY` and `VENICE_API_KEY`. It combines the reviewed Auto
routes with the providers' live `/v1/models` catalogs, cached for five minutes by default.
Production readiness fails closed when an enabled provider credential is absent.

On a Linux NVIDIA host, start the self-hosted profile with
`docker compose --profile gpu up --build`; it uses the separate
`config/models.self-hosted.yaml` catalog and requires callers to set
`X-Confidential-Compute: disabled`:

```bash
cp .env.example .env
docker compose --profile gpu up --build

curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-local" \
  -H "Content-Type: application/json" \
  -H "X-Confidential-Compute: disabled" \
  -d '{"model":"qwen/qwen2.5-0.5b-instruct","messages":[{"role":"user","content":"Hello"}]}'
```

The example key is for isolated local development only. Replace it before exposing the gateway to any network. On a laptop without an NVIDIA GPU, run the test suite instead; it uses in-process OpenAI-compatible mocks:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Create a real key with `uv run python scripts/create_api_key.py TENANT_ID`; put only the printed hash entry in `API_KEY_HASHES`.

## GCP deployment

1. Review [architecture](https://github.com/open-software-network/os-api/blob/main/docs/architecture.md), [threat model](https://github.com/open-software-network/os-api/blob/main/docs/threat-model.md), and [operations](https://github.com/open-software-network/os-api/blob/main/docs/operations.md).
2. Review [private provider routing](https://github.com/open-software-network/os-api/blob/main/docs/private-routing.md), including the privacy taxonomy and
   E2EE boundary.
3. Add hosted-provider API keys to Secret Manager, or obtain GPU quota when enabling self-hosted endpoints.
4. Build and push the gateway image to the Artifact Registry repository created by Terraform.
5. Copy `deploy/terraform/backend.hcl.example` to the ignored `backend.hcl`, create its private versioned bucket, then run `terraform init -backend-config=backend.hcl` and `terraform apply` from `deploy/terraform`.
6. Create the Kubernetes secrets described in `deploy/kubernetes/base/secrets.example.yaml`; never commit their values.
7. Follow the [GKE-specific Kubernetes instructions](https://github.com/open-software-network/os-api/blob/main/deploy/kubernetes/README.md), then apply the appropriate hosted-provider or self-hosted overlay.
8. Enable the global HTTPS load balancer and complete the Certificate Manager DNS authorization before restricting Cloud Run ingress.

The confidential node pool is disabled by default because GCP rejects creation without zone-specific preemptible H100 quota. Enable it only after quota and capacity are confirmed:

```hcl
enable_confidential_gpu_pool = true
confidential_gpu_zone        = "a-supported-zone"
```

For a lower-cost validation of TDX scheduling and fail-closed API routing, Terraform also includes an optional scale-to-zero `c3-standard-4` confidential CPU pool. The `standard-smoke` overlay can route the small Qwen smoke model to it. This proves the gateway contract and TDX placement, but it is not an H100 performance substitute.

## Remaining production boundaries

The first control plane is live, including login, key lifecycle, hard spend limits, usage metering, and wallet settlement. The remaining launch controls and measurable exit criteria are tracked in the [production parity checklist](https://github.com/open-software-network/os-api/blob/main/docs/production-parity.md) and sequenced in [operations](https://github.com/open-software-network/os-api/blob/main/docs/operations.md).

## Contributing and security

Contributions are welcome. Start with [CONTRIBUTING.md](https://github.com/open-software-network/os-api/blob/main/CONTRIBUTING.md), follow the [Code of Conduct](https://github.com/open-software-network/os-api/blob/main/CODE_OF_CONDUCT.md), and see [governance](https://github.com/open-software-network/os-api/blob/main/GOVERNANCE.md), [support boundaries](https://github.com/open-software-network/os-api/blob/main/SUPPORT.md), the [changelog](https://github.com/open-software-network/os-api/blob/main/CHANGELOG.md), and [authors](https://github.com/open-software-network/os-api/blob/main/AUTHORS.md). Report vulnerabilities through the private process in [SECURITY.md](https://github.com/open-software-network/os-api/blob/main/SECURITY.md) rather than a public issue.

## License

The gateway is available under the [MIT License](https://github.com/open-software-network/os-api/blob/main/LICENSE). Provider services, model weights, container images, and trademarks remain subject to their respective owners' terms; see [THIRD_PARTY_NOTICES.md](https://github.com/open-software-network/os-api/blob/main/THIRD_PARTY_NOTICES.md).
