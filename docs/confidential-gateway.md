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
2. Add the next digest to `confidential_gateway_additional_image_digests` while
   leaving the current `gateway_image` unchanged, then apply only the workload
   identity provider and digest-scoped IAM members. Exactly those two images are
   now authorized.
3. Set `gateway_image` to the next digest while temporarily retaining the old
   digest in `confidential_gateway_additional_image_digests`. Roll and verify the
   primary managed instance group through the isolated staging load balancer,
   then roll the secondary group.
4. Verify the public source, health checks, load behavior, and fresh evidence
   before shifting user traffic.
5. Remove the old digest from `confidential_gateway_additional_image_digests`
   and apply again. Steady state authorizes only `gateway_image`.

Cloud Run remains the default until the switch is enabled. This change does not
deploy production by itself.

The measured container entrypoint starts as UID 0 only to take ownership of the
root-owned launcher attestation socket. It restricts that socket to UID/GID
10001, clears supplementary groups, drops its real, effective, and saved IDs to
10001, verifies the drop, and only then executes the API server. Confidential
Space records the entrypoint in the attestation claims, and this image's launch
policy does not allow an operator to override the command. Standard Kubernetes
deployments continue to enforce `runAsNonRoot`, UID 10001, dropped capabilities,
and disabled privilege escalation.
