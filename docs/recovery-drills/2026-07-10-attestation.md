# Confidential-compute attestation verification — 2026-07-10

## Result

Passed. The attestation endpoint returned a genuine Intel TDX quote produced by a confidential
node, bound it to a client nonce and the configured workload identity, and failed closed when
evidence was unavailable.

## Control tested

1. A node-local quote agent obtains a TDX quote without handling prompts, credentials, or customer
   data.
2. The gateway creates a canonical binding containing the client nonce and expected workload
   attributes, then places its SHA-512 digest in the quote's report data.
3. The response includes the quote, canonical binding, and reported digest for client verification.

The client must generate a fresh nonce, recompute the binding digest, confirm the nonce and
expected workload attributes, locate the digest in the quote, and validate the quote against the
platform certificate chain.

## Scope and follow-up

- Quote verification remains a client responsibility; the gateway does not provide an independent
  attestation PKI.
- The binding describes gateway-configured workload identity. It is not a measured-boot proof of
  the model image.
- Confidential key release is not yet gated on attestation.

Repeat after changes to the quote agent, binding schema, confidential node configuration, or
client-verification contract. Test valid evidence, nonce mismatch, malformed evidence, and agent
unavailability without recording raw production evidence in this repository.
