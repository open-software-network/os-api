# Hosted private-router launch drill — 2026-07-11

## Result

Passed. OpenSoftware API 0.10.0 was deployed as an OpenAI-compatible private inference router in
both production regions using the same Binary Authorization-signed image digest. Real confidential
inference completed through the private canary, the global public edge, and the east production
gateway. No self-hosted GPU was required.

## Supply chain and deployment

- Image index digest: `sha256:1ce01ff8d7f0ce35d770b0808da6f339f2e0f7e40498363a62ce996ea5e641b5`
- Cloud Run platform manifest: `sha256:b7eedbb2ad199e2b3567b0f52cba0499077afb4a5011288b5b4d7b28a879be76`
- Both the index and amd64 manifest received KMS-backed Binary Authorization attestations.
- Private canary: `sovereign-api-canary-immutable-192623`
- Primary: `sovereign-api-immutable-192623` (`us-central1`, 100% regional traffic)
- Secondary: `sovereign-api-secondary-immutable-192623` (`us-east4`, 100% regional traffic)
- `MODEL_CONFIG_PATH=/app/config/models.yaml` binds the catalog to the signed image. The release
  workflow no longer publishes a mutable `latest` catalog secret ahead of canary validation, so an
  old code revision cannot cold-start against a new schema.
- Direct primary ingress remained unavailable outside the global load balancer, returning 404 as
  designed; it was not relaxed for testing.

## Credential checks

- Venice API key authenticated successfully and returned fresh TEE attestation with
  `verified=true`, a matching 32-byte client nonce, and a signing key.
- The original Phala-format credential was rejected with HTTP 401 and was never promoted.
- A replacement confidential gateway credential returned a successful GPT-OSS 20B completion,
  usage/cost metadata, and a receipt. It was stored as Secret Manager version 2; rejected version 1
  was disabled.
- Runtime access to both provider secrets is limited to the existing
  `sovereign-api-runtime` service account.

No secret value was printed in drill output or added to the repository.

## Canary verification

The private IAM-gated canary passed all of the following against the exact promoted image:

- `/readyz`: HTTP 200, `status=ready`
- `/v1/models`: eight canonical private models
- Real `/v1/chat/completions` request for `openai/gpt-oss-20b`
- `X-Confidential-Compute: required`
- `privacy.minimum=tee`
- `privacy.require_zero_retention=true`
- `privacy.require_response_proof=true`
- Canonical response model: `openai/gpt-oss-20b`
- Provider: `phala`
- Privacy level: `tee`
- Evidence status: `present`
- Non-empty response content
- Tenant-scoped receipt retrieval: passed

## Public verification

The global public endpoint returned:

- `/readyz`: `ready`
- Model count: 8
- Gateway region: `us-east4`
- Canonical response model: `openai/gpt-oss-20b`
- Provider: `phala`
- Privacy level: `tee`
- Evidence status: `present`
- Tenant-scoped durable receipt: provider, privacy level, and evidence status all matched

## Important interpretation

`evidence.status=present` means the expected provider receipt identifiers were returned. It does
not mean OpenSoftware independently verified the complete Intel/NVIDIA quote, software
measurements, build provenance, and response signature. The catalog therefore advertises `tee`,
not `verified-tee`. Configuration rejects unsupported `verified-tee` and `signed-output` claims.

Venice E2EE routing additionally challenges the Venice attestation endpoint with a fresh nonce and
checks that the client-selected encryption key matches the attested signing key before forwarding
ciphertext. This is a freshness/key-binding control, not an independent hardware verifier.

## Repeat the drill

Repeat after provider credential rotation, catalog or pricing changes, provider adapter changes,
privacy-policy changes, billing changes, or deployment pipeline changes. The release workflow now
performs the real confidential canary automatically and refuses promotion when it fails.
