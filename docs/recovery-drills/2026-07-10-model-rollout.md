# Model rollout and rollback drill — 2026-07-10

## Result

Passed. A valid model release completed the canary and promotion gates. A deliberately invalid
release failed readiness and triggered automatic rollback while the previous ready workload
continued serving synthetic requests.

## Control tested

1. Start the candidate as an off-service canary so it cannot receive customer traffic.
2. Run a deterministic golden-prompt smoke test directly against the candidate.
3. Promote with a strategy that preserves the ready workload until the replacement is ready.
4. Run the same gate against the serving deployment.
5. On timeout or failed readiness, undo the rollout and verify the prior pinned image is serving.

Responses expose `x-model-revision`, `x-model-backend`, and `x-compute-privacy` so operators and
clients can identify the configured serving path without relying on mutable tags.

## Scope

Golden prompts are smoke tests, not a scored quality evaluation. A single-replica deployment uses
an off-service canary because traffic-weighted canaries would expose an unvalidated candidate to a
large fraction of requests. Capacity-constrained accelerators may need a different strategy and an
explicit maintenance objective.

Repeat after changes to rollout automation, probes, deployment strategy, model images, golden
prompts, node-pool sizing, or rollback retention.
