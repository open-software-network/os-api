# Production parity checklist

Production parity means the platform is ready for a broad commercial launch, not merely that one model returns successful responses. Every control below requires an implementation, an automated test or reproducible drill, an owner-facing runbook, and live verification.

| Capability | Current state | Exit criteria |
| --- | --- | --- |
| API contract, login, key lifecycle, billing | Complete | OpenAI-compatible requests, fail-closed privacy routing, expiration/revocation, hard spend limits, idempotent settlement, and live smoke tests remain green. |
| Edge and origin security | Complete | Managed TLS, Cloud Armor, direct-origin blocking, rate-based abuse bans, authentication-spike alerts, and documented threshold tuning. |
| Customer auditability | Complete | Metadata-only key and inference events, JSONL/CSV export, retained backups, tenant-isolation coverage, CSV-injection defense, and a live managed-key canary are verified. |
| Backup and recovery | In progress | Seven-day PITR and daily exports to a versioned, retention-protected bucket are automated; a successful restore drill into an isolated project remains required. |
| Multi-region control plane | Pending | At least two gateway regions behind health-based routing with no shared regional single point of failure and a documented failover drill. |
| Model rollout safety | Pending | Digest-pinned model revisions, readiness and quality gates, canary traffic, automatic rollback, and version-visible responses. |
| Capacity and admission control | Pending | Reserved or diversified GPU capacity, concurrency queues, overload responses, capacity alerts, and tested scale/failure behavior. |
| Verifiable confidential compute | Pending | Client-verifiable attestation bound to workload and model image digests, freshness, documented verification, and fail-closed key release. |
| Security assurance | Pending | Egress restrictions, Binary Authorization enforcement, VPC Service Controls where applicable, restore/incident exercises, and an independent penetration test. |

## Global acceptance tests

1. No prompt, completion, raw API key, authorization header, wallet grant, or Basic-auth credential appears in logs, metrics, traces, audit exports, or backups.
2. A confidential request never silently downgrades, including during zone loss, rollout, quota exhaustion, or attestation failure.
3. Revocation, spend enforcement, and abuse controls remain effective under concurrent requests and regional failover.
4. A release and a model rollout can each be rolled back without changing customer endpoint or credentials.
5. Recovery point and recovery time objectives are measured by recurring restore and failover drills rather than documentation alone.
