# Confidential-compute attestation verification — 2026-07-10

## Result

Passed. `POST /v1/attestation` returns a genuine Intel TDX quote generated on the confidential node, bound to a client nonce and the deployed workload, and fails closed when evidence is unavailable. Verified on the private canary and on the public endpoint in both regions.

## How it works

1. A privileged DaemonSet (`tdx-quote-agent`) runs on every confidential node. It mounts `configfs`, writes a 64-byte report-data value to `/sys/kernel/config/tsm/report/<id>/inblob`, and reads the resulting raw TDX quote from `outblob`. It handles no prompts, keys, or customer data.
2. The gateway builds a canonical binding — client nonce, gateway version, gateway region, and each confidential backend with its pinned model revision — and sets report-data to its SHA-512.
3. The response returns the quote (base64), the canonical binding, and the report-data hash.

## Client verification (documented contract)

A client:
1. Sends a fresh random `nonce`.
2. Recomputes `SHA-512(binding_canonical)` and confirms it equals `report_data_sha512` and appears at bytes 568–632 of the decoded quote.
3. Confirms `binding.nonce` is its own nonce (freshness) and that `gateway_version` / `confidential_backends[].revision` match the expected release (workload identity).
4. Validates the quote against Intel's PCS certificate chain (standard TDX quote verification).

## Evidence

- Canary: `provider tdx_guest`, 8000-byte quote, header `0400020081000000`, `binding_hash_matches_reported: True`, `report_data_in_quote: True`, backends `[{confidential-vllm-cpu, vllm-openai-cpu@89e1fbe82903}]`.
- Public endpoint: returned a valid bound quote stamped `region: us-east4`, and the us-central1 canary returned `region: us-central1` — each region attests its own confidential node.
- Fail-closed: unit tests confirm `503 attestation_unavailable` when the agent URL is unset or the agent errors; the gateway never returns a substitute.

## Honest scope

- The quote is verified **by the client**; the gateway does not yet run server-side quote verification or a full attestation PKI.
- report-data binds gateway-asserted configuration (version, region, configured model revision), not a measured-boot hash of the running model image. Binding a measured model image digest is the next hardening step.
- No confidential key release is gated on attestation yet; that is the eventual fail-closed-key-release goal.

Rerun after changes to the quote agent, the binding contents, or the confidential node pool.
