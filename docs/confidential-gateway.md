# Attested Confidential Space gateway

Production can run os-api as a Google Cloud Confidential Space workload instead
of Cloud Run. The chain is:

```text
June client -> attested June API -> attested os-api gateway -> private or attested model
```

The Terraform `enable_confidential_gateway` switch creates regional managed
instance groups backed by Intel TDX Confidential Space instance templates. The
load balancer keeps the same public API surface.

## Fail-closed identity and secrets

The bootstrap VM service account can pull the pinned container and write logs,
but it cannot read runtime secrets. Google Cloud Attestation accepts only a
stable, non-debug Confidential Space workload on Intel TDX with the exact
`gateway_image` digest. Only that attested identity may impersonate the runtime
service account. The application then reads provider credentials and config
from Secret Manager before it starts serving.

An image tag, debug workload, different digest, ordinary VM, or unavailable
attestation service cannot obtain runtime credentials.

## Public verification

`POST /v1/gateway/attestation` is public and accepts a caller-generated URL-safe
nonce between 10 and 88 characters:

```bash
curl https://api.opensoftware.co/v1/gateway/attestation \
  -H 'content-type: application/json' \
  -d '{"nonce":"replace-with-a-fresh-random-nonce"}'
```

The response contains a Google Cloud Attestation OIDC token bound to that nonce
and the June gateway audience. A verifier must check Google's signature,
expiration, issuer, audience, nonce, `CONFIDENTIAL_SPACE`, debug-disabled state,
Intel TDX hardware, `STABLE` support attribute, and the exact approved image
digest. June API performs all of those checks and exposes an in-browser verifier
on its `/verify` page.

The endpoint returns `503` outside Confidential Space. It never manufactures
development evidence. Responses use `Cache-Control: no-store`.

## Rollout

1. Build and publish os-api, then record its immutable `sha256:` digest.
2. Set `gateway_image` to that digest and enable `enable_confidential_gateway`.
3. Apply Terraform and wait for both regional managed instance groups to pass
   the private health check.
4. Enable June's gateway policy with the same digest.
5. Verify from June's `/verify` page before shifting user traffic.

Cloud Run remains the default until the switch is enabled. This change does not
deploy production by itself. While Confidential Space managed instance groups
serve the production edge, the Cloud Run GitHub workflow exits before building;
it cannot update or verify the active measured workload.
