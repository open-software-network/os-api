# Regional Redis-loss drill — 2026-07-10

## Result

Passed. When a region's Redis dependency became unreachable, a fresh gateway instance started and
served synthetic inference through the documented degraded path. The other region remained
independent, and hard spend and edge controls remained active.

## Behavior verified

- Redis is regional rather than a shared cross-region dependency.
- Rate limiting and backend admission control fail open when Redis is unavailable.
- Spend enforcement remains in the durable billing store and is not bypassed by Redis loss.
- Readiness reports Redis as degraded without making the whole gateway unready.
- Connection and operation timeouts bound the delay introduced by an unreachable dependency.
- Degraded-operation telemetry is emitted for both rate-limit and concurrency checks.

The exercise must test both a warm gateway and a newly started gateway. Block connectivity using a
temporary, narrowly scoped rule; confirm readiness, a synthetic request, degraded telemetry, and
the absence of Redis-derived response metadata; then remove the rule and verify normal enforcement
resumes. Do not record live addresses, rule names, counters, or revision identifiers in this file.

Repeat after changes to the Redis client, readiness logic, fail-open policy, regional topology,
timeouts, or hard-spend enforcement.
