# Attested Confidential Space gateway

Production can run os-api as a Google Cloud Confidential Space workload instead
of Cloud Run. This deployment is verified independently:

```text
caller -> attested os-api gateway -> selected model provider
```

The Terraform `enable_confidential_gateway` switch creates regional managed
instance groups backed by Intel TDX Confidential Space instance templates. The
load balancer keeps the same public API surface.

## Fail-closed identity and secrets

The bootstrap VM service account can pull the pinned container and write logs,
but it cannot read runtime secrets. Google Cloud Attestation accepts only a
stable, non-debug Confidential Space workload on Intel TDX launched with that
dedicated bootstrap service account. An identity that passes that hardware
policy may impersonate the runtime service account. The application then reads
provider credentials and config from Secret Manager before it starts serving.

A debug workload, ordinary VM, workload using another bootstrap identity, or
unavailable attestation service cannot obtain runtime credentials. The policy
does not pin one application image digest. Instead, each immutable deployment
publishes its actual digest in the public attestation so changes are independently
visible without coordinating releases with June or Chat.

## Public verification

`POST /v1/gateway/attestation` is public and accepts a caller-generated URL-safe
nonce between 10 and 88 characters:

```bash
curl https://api.opensoftware.co/v1/gateway/attestation \
  -H 'content-type: application/json' \
  -d '{"nonce":"replace-with-a-fresh-random-nonce"}'
```

The response contains a Google Cloud Attestation OIDC token bound to that nonce.
Its audience retains the legacy `https://june-api.opensoftware.co/os-api-gateway`
value for compatibility, but verification does not depend on June. A verifier
must check Google's signature,
expiration, issuer, audience, nonce, `CONFIDENTIAL_SPACE`, debug-disabled state,
Intel TDX hardware, `STABLE` support attribute, and the exact approved image
digest. The API's public `/verify` page links this evidence and the public source.

The endpoint returns `503` outside Confidential Space. It never manufactures
development evidence. Responses use `Cache-Control: no-store`.

## Rollout

1. Build and publish os-api, then record its immutable `sha256:` digest.
2. Set `gateway_image` to that digest and enable `enable_confidential_gateway`.
3. Apply Terraform and wait for both regional managed instance groups to pass
   the private health check.
4. Verify the public source and request fresh evidence from the candidate.
5. Shift user traffic only after health checks and verification pass.

Cloud Run remains the default until the switch is enabled. This change does not
deploy production by itself.
