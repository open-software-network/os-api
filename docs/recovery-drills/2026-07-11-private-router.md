# Hosted private-router launch drill — 2026-07-11

## Result

Passed. The OpenAI-compatible private inference router deployed across the declared regions from a
single signed release artifact. The private canary, global edge, and each regional gateway served
synthetic confidential inference before promotion completed.

## Supply-chain and deployment checks

- The OCI index and deployable platform manifest were signed and accepted by admission policy.
- The exact canary artifact was promoted without rebuilding or changing the model catalog.
- The catalog was loaded from the immutable release image rather than a mutable external alias.
- Direct regional ingress remained restricted; verification used the intended private canary and
  global-edge paths.

## Provider and request checks

- Provider credentials were validated without printing their values or version identifiers.
- Invalid credentials were rejected and were not promoted.
- The canary and public edge returned readiness, the canonical model catalog, a non-empty synthetic
  completion, usage metadata, and a tenant-scoped receipt.
- Requests requiring confidential compute and zero retention selected an eligible provider path.
- Receipt metadata agreed with the selected provider, declared privacy level, and evidence status.

## Evidence interpretation

An evidence status of `present` means the configured provider returned the expected receipt
identifiers. It does not mean this project independently verified the complete hardware quote,
software measurements, build provenance, and response signature. The catalog must not advertise a
stronger privacy claim than the verifier actually enforces.

Where end-to-end encrypted routing is enabled, challenge attestation with a fresh nonce and bind
the encryption key selected by the client to the attested signing key. This is a freshness and
key-binding control, not an independent hardware verifier.

Repeat after provider credential rotation, catalog or pricing changes, adapter changes, privacy
policy changes, billing-path changes, or deployment-pipeline changes. Keep artifact digests,
revision names, secret versions, identities, raw receipts, and live endpoint results in the
restricted release record.
