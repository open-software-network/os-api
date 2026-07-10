# Production operations

## Service objectives

- Public control-plane availability target: 99.9% per calendar month.
- Confidential model availability is reported separately and is not included in the control-plane SLO while capacity uses a single TDX pool.
- The API never silently downgrades a request whose `X-Confidential-Compute` value is `required`.
- Prompts, completions, bearer tokens, wallet grants, and full request URLs must never appear in logs, metrics, traces, or support tickets.

## Release process

The `Deploy production` GitHub workflow is manually dispatched from `main`. It authenticates with GCP Workload Identity Federation, builds an SBOM/provenance-attested image, pushes it to Artifact Registry, and deploys the image by digest to a private IAM-gated canary service. It deploys and promotes the identical digest to production only after:

1. The canary `/readyz` confirms Redis and Firestore access.
2. An IAM-authenticated canary `/v1/models` call succeeds with the dedicated smoke key.
3. The public readiness endpoint succeeds after traffic promotion.

The previous revision remains available for rollback:

```bash
gcloud run revisions list \
  --service=sovereign-api \
  --region=us-central1 \
  --project=autonomous-lab-490610

gcloud run services update-traffic sovereign-api \
  --region=us-central1 \
  --project=autonomous-lab-490610 \
  --to-revisions=REVISION_NAME=100
```

Do not deploy mutable tags. Terraform owns service configuration and CI owns the digest-pinned application revision.

## Alerts

Terraform creates an external readiness check and policies for sustained 5xx responses, readiness failures, wallet settlement failures, authentication spikes, scheduled backup failures, sustained admission-control rejections (`model_overloaded`), model-backend timeouts and connection failures, and the gateway running near its Cloud Run instance ceiling. Alert notifications go to the configured operations channel. The next alert set should cover Redis memory pressure, model load failures, GPU Xid events, and time-to-first-token by backend.

On any alert:

1. Confirm scope and copy one `X-Request-ID`; never copy the request body or API key.
2. Check Cloud Run revision traffic and structured `event` logs.
3. Check `/readyz`, Redis availability, and the selected model pod.
4. Roll back the gateway if the incident began with a release.
5. If the model is unhealthy, fail the affected privacy mode closed rather than silently changing its privacy class.
6. Record customer impact, start/end times, and corrective actions in the incident review.

## Model rollouts

Model server images are pinned by digest in `deploy/kubernetes` and never deployed by mutable tag. Every inference response identifies the artifact that served it: `x-model-backend` names the backend and `x-model-revision` carries the pinned image revision from the model configuration (`revision` per backend in `models.yaml`). When rolling a model image, update the digest in the manifests **and** the `revision` fields in both the in-cluster ConfigMap patch and the `cloudrun-model-config` secret so the header stays truthful.

Roll out with `scripts/model_rollout.sh`, which enforces three gates:

1. **Canary**: the exact new image starts as an off-service deployment on the production cluster (no Service selects it) and must answer golden prompts before any serving pod is touched.
2. **Health-gated promotion**: the CPU model deployment uses `RollingUpdate` with `maxSurge: 1, maxUnavailable: 0`, so the old model keeps serving until the new pod is ready; a stuck rollout is automatically undone. The surge pod schedules onto an autoscaled node (the pool allows two nodes), so allow ~5–15 minutes.
3. **Post-promote verification**: golden prompts run against the serving deployment; failure triggers an automatic `rollout undo`.

The GPU deployment keeps `Recreate` because it owns a single dedicated TDX GPU node; treat GPU model rollouts as brief planned unavailability of the confidential GPU pool until capacity is diversified. Roll back manually at any time with `kubectl -n inference rollout undo deployment/<name>` — data and gateway config are unaffected by model rollbacks. The latest exercised rollout and automatic rollback are recorded in [the 2026-07-10 model rollout drill](recovery-drills/2026-07-10-model-rollout.md).

## Billing reconciliation

Wallet charges use `inference:<billing_id>` as an idempotency key and are retried three times. A `wallet_charge_failed` event means inference was delivered and local usage settled, but the wallet charge did not confirm. Re-run only the idempotent charge for that billing ID after verifying its Firestore usage event. Never synthesize a new billing ID for the same usage.

## Key compromise

1. Revoke the key in the dashboard; revocation is effective on the next authenticated request.
2. Search request metadata by key ID or fingerprint, never by raw key.
3. Rotate any upstream credential if there is evidence it was exposed.
4. Preserve security logs and notify the owner using the account email.

## Supply chain

Binary Authorization enforces a deny-by-default image allowlist on the inference GKE cluster and every gateway Cloud Run service: only images from the project's `sovereign-inference` Artifact Registry repository and the pinned `docker.io/vllm/*` model servers are admitted; Google-maintained GKE system images are exempted via the global policy. Enforcement is admission-time, so it never disrupts running workloads, and it is opt-in per workload — other clusters in the project are unaffected. A blocked deploy fails with `Denied by an ALWAYS_DENY admission rule`; fix the image source rather than editing the policy. The enforcement drill is recorded in [the 2026-07-10 Binary Authorization drill](recovery-drills/2026-07-10-binauthz.md).

## Site access gate

The human-facing site, docs, dashboard, login callbacks, and dashboard JSON routes use HTTP Basic authentication when `SITE_BASIC_AUTH_USERNAME` and `SITE_BASIC_AUTH_PASSWORD` are configured. The password is stored in the `site-basic-auth-password` Secret Manager secret. `/v1/*`, `/healthz`, and `/readyz` are intentionally exempt so API-key clients and infrastructure probes continue to work. Rotate the site password by adding a new secret version and bumping `RUNTIME_CONFIG_VERSION` in both Cloud Run services.

## Edge and denial-of-service controls

Cloud Armor throttles `/v1/*` at 600 requests per source IP per minute. A source that reaches 1,800 requests in one minute is banned for ten minutes. These regional, approximate edge controls are intentionally looser than the 60 RPM per-key Redis limit and exist only to protect availability; they are not billing quotas. Monitoring alerts when rejected Bearer credentials exceed 60 per minute. Review load-balancer source distribution and request IDs before changing thresholds. Do not enable body-inspecting WAF rules on model prompts without a privacy and false-positive review.

Production DNS resolves `api.opensoftware.co` directly to the global load-balancer address `34.36.164.16` in DNS-only mode. Cloud Run uses `INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER`, the legacy Cloud Run domain mapping is removed, and the default `run.app` URL cannot bypass Cloud Armor. Keep the Certificate Manager DNS-authorization CNAME in place so certificate renewal continues to work.

## Capacity and shutdown

The gateway keeps one Cloud Run instance warm and can scale to ten. Model compute scales independently and remains the bottleneck.

Admission to each model backend is bounded by `max_concurrency` in `models.yaml`, enforced globally across every gateway instance and region through a shared Redis in-flight counter. Requests beyond the bound are rejected **before any billing reservation** with `503`, `Retry-After: 10`, and OpenAI-style error code `model_overloaded`; clients are expected to retry. The counter key self-heals via a 900-second expiry refreshed on each admit, so a crashed gateway instance cannot permanently shrink capacity. Size `max_concurrency` from measured backend saturation, not from the alert threshold: the live saturation test admitted exactly 8 of 24 concurrent requests at the configured limit of 8 and recovered immediately after the burst. Raising the limit without adding model capacity converts fast, honest 503s into slow queueing for everyone. Stop model charges without deleting data:

```bash
kubectl -n inference scale deployment/confidential-vllm-cpu --replicas=0
gcloud container clusters resize sovereign-inference \
  --node-pool=confidential-cpu-tdx \
  --num-nodes=0 \
  --region=us-central1 \
  --project=autonomous-lab-490610 \
  --quiet
```

## Backup and recovery

Terraform state is stored in a versioned GCS backend. Secret Manager retains secret versions; Firestore is the system of record for keys, usage, and customer audit metadata. Firestore point-in-time recovery retains document versions for seven days. Cloud Scheduler starts a managed export of `sovereign_api_keys`, `sovereign_usage`, and `sovereign_audit_events` every day at 04:00 UTC. Exports go to the versioned `autonomous-lab-490610-os-api-firestore-backups` bucket, have a 30-day minimum retention period, and expire after 90 days. The retention policy is intentionally not locked until an owner explicitly accepts the irreversible lock.

Run and verify an export without waiting for the next schedule:

```bash
gcloud scheduler jobs run os-api-firestore-export \
  --location=us-central1 \
  --project=autonomous-lab-490610

gcloud firestore operations list \
  --database='(default)' \
  --project=autonomous-lab-490610

gcloud storage ls \
  gs://autonomous-lab-490610-os-api-firestore-backups/
```

Treat a successful scheduler HTTP response as "export accepted," not "backup complete." Close the check only after the Firestore operation reports `done: true`, has no error, and its timestamped prefix contains a matching `.overall_export_metadata` object.

Quarterly, restore the latest completed export into the designated isolated recovery project and record the measured recovery point and recovery time. The recovery project must use a bucket/database-compatible location, must not have production runtime service accounts, and must never receive production traffic. Verify key, usage, and audit collection counts; sample documents for schema compatibility; confirm raw key secrets and prompts are absent; and then destroy the recovery database. Never restore into production during a drill, and never reactivate an expired or revoked key.

The latest completed exercise is recorded in [the 2026-07-10 recovery drill](recovery-drills/2026-07-10.md).

## Regional failover

The gateway runs in us-central1 (`sovereign-api`) and us-east4 (`sovereign-api-secondary`) behind the same global load balancer, on the same image digest, against the same nam7 Firestore database. Every response carries an `x-gateway-region` header identifying the serving region. Both internal model load balancers set `allowGlobalAccess`, and the vLLM NetworkPolicies admit both gateway subnets (10.20.0.0/20 and 10.30.0.0/24); a change to either breaks cross-region inference silently, so rerun the failover drill after touching them.

Known regional single points of failure: Redis (rate limits and spend reservations) and the GKE model cluster both live in us-central1. A full us-central1 outage therefore still takes down inference and readiness in both regions. Do not advertise a regional-outage SLO until those are replicated.

To fail away from a region, detach its NEG from the `os-api-edge` backend service and confirm the other region serves via the header; restore by re-running `terraform apply`, which is authoritative for the backend set. Failback routing is eventually consistent — judge recovery by the attached backend set and continuous 200s, not by which region serves your client. The latest exercise, including two defects the first attempt surfaced, is recorded in [the 2026-07-10 failover drill](recovery-drills/2026-07-10-failover.md).
