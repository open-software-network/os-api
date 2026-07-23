# Regional Redis-loss drill — 2026-07-10

## Result

Passed. With us-central1 Redis made unreachable, a fresh gateway instance still started and served inference in that region by failing open on rate limiting and admission control, while the us-east4 region — now on its own Redis — was unaffected. Hard spend limits (Firestore) and Cloud Armor per-IP limits remained in force throughout.

## What changed to make this possible

- Each gateway region has its own Memorystore instance (`sovereign-inference-limits`, `sovereign-inference-limits-us-east4`). Redis is no longer a shared cross-region dependency.
- Redis operations fail open: on a Redis error the gateway logs `redis_degraded` and serves the request without the per-key RPM or per-backend concurrency check. Spend and settlement are unaffected because they live in Firestore.
- `/readyz` gates readiness on Firestore only; Redis is reported as `degraded` but keeps the region ready. Redis client operations are bounded (1s socket timeout, 2s readiness ping) so a partition degrades fast instead of hanging the probe.

## Timeline and evidence

- Central Redis egress blocked with a temporary deny firewall rule on tcp:6378 to the instance IP (21:29:50Z).
- A warm instance kept its established connection (expected: the rule blocks new connections only), so a fresh 0.9.1 canary revision was deployed *while Redis was blocked* to exercise cold start under outage.
- The fresh instance became ready and served:
  - `/readyz` → `{"status":"ready","components":{"firestore":"ready","redis":"degraded"}}`
  - inference → HTTP 200 with **no** `x-ratelimit-remaining-requests` header (fail-open path), at ~2.7s (the bounded Redis timeout).
  - logs recorded `redis_degraded` for both the `rate_limit` and `concurrency` checks.
- The public endpoint (serving from us-east4 on its own Redis) returned 200 throughout.
- Deny rule removed (21:50:48Z); rate limiting immediately resumed (remaining counter decremented 59 → 58 across two requests).

## Design note surfaced by the drill

The first attempt failed to start the fresh instance: the readiness probe timed out because `redis.ping()` hung on the firewalled connection. This exposed a contradiction — a readiness check that gates startup on Redis would prevent the fail-open path from ever engaging for new instances during an outage. Fixed by making Redis non-fatal for readiness and bounding all Redis operations with explicit timeouts. Alert: "OpenSoftware API serving with Redis degraded" fires on the `redis_degraded` metric.

Rerun after any change to the Redis client configuration, the readiness logic, or the per-region Redis topology.
