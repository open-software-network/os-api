# Live platform validation

This is a public summary of platform validation, not a production inventory. Project identifiers,
IP addresses, direct service URLs, revision names, credential names, and raw provider evidence are
intentionally omitted.

The following controls have been exercised with synthetic requests in isolated and production-like
GCP environments:

- OpenAI-compatible model discovery, chat completions, and SSE streaming
- Invalid and revoked API-key rejection
- Per-key rate limiting and bounded degraded behavior during Redis loss
- Strict confidential routing that returns an error rather than downgrading
- Intel TDX placement and nonce-bound attestation responses
- Regional gateway failover and recovery
- Digest-pinned deployment, Binary Authorization, and rollback
- Charge-on-success billing and idempotent settlement
- Admission control under concurrent load
- Prompt and credential log scanning

Detailed drill evidence is retained in a restricted operational system because it contains live
infrastructure metadata. Public guidance for reproducing the controls is available in
[operations](operations.md), [architecture](architecture.md), and the [threat model](threat-model.md).

This validation does not make every replacement model container or provider private by default.
Operators must review their selected providers, model images, network policy, attestation evidence,
and retention behavior against the threat model.
