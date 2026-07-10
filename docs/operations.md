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

Terraform creates an external readiness check and policies for sustained 5xx responses, readiness failures, wallet settlement failures, authentication spikes, and scheduled backup failures. Alert notifications go to the configured operations channel. The next alert set should cover Redis memory pressure, pending inference pods, model load failures, GPU Xid events, and time-to-first-token by backend.

On any alert:

1. Confirm scope and copy one `X-Request-ID`; never copy the request body or API key.
2. Check Cloud Run revision traffic and structured `event` logs.
3. Check `/readyz`, Redis availability, and the selected model pod.
4. Roll back the gateway if the incident began with a release.
5. If the model is unhealthy, fail the affected privacy mode closed rather than silently changing its privacy class.
6. Record customer impact, start/end times, and corrective actions in the incident review.

## Billing reconciliation

Wallet charges use `inference:<billing_id>` as an idempotency key and are retried three times. A `wallet_charge_failed` event means inference was delivered and local usage settled, but the wallet charge did not confirm. Re-run only the idempotent charge for that billing ID after verifying its Firestore usage event. Never synthesize a new billing ID for the same usage.

## Key compromise

1. Revoke the key in the dashboard; revocation is effective on the next authenticated request.
2. Search request metadata by key ID or fingerprint, never by raw key.
3. Rotate any upstream credential if there is evidence it was exposed.
4. Preserve security logs and notify the owner using the account email.

## Site access gate

The human-facing site, docs, dashboard, login callbacks, and dashboard JSON routes use HTTP Basic authentication when `SITE_BASIC_AUTH_USERNAME` and `SITE_BASIC_AUTH_PASSWORD` are configured. The password is stored in the `site-basic-auth-password` Secret Manager secret. `/v1/*`, `/healthz`, and `/readyz` are intentionally exempt so API-key clients and infrastructure probes continue to work. Rotate the site password by adding a new secret version and bumping `RUNTIME_CONFIG_VERSION` in both Cloud Run services.

## Edge and denial-of-service controls

Cloud Armor throttles `/v1/*` at 600 requests per source IP per minute. A source that reaches 1,800 requests in one minute is banned for ten minutes. These regional, approximate edge controls are intentionally looser than the 60 RPM per-key Redis limit and exist only to protect availability; they are not billing quotas. Monitoring alerts when rejected Bearer credentials exceed 60 per minute. Review load-balancer source distribution and request IDs before changing thresholds. Do not enable body-inspecting WAF rules on model prompts without a privacy and false-positive review.

Production DNS resolves `api.opensoftware.co` directly to the global load-balancer address `34.36.164.16` in DNS-only mode. Cloud Run uses `INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER`, the legacy Cloud Run domain mapping is removed, and the default `run.app` URL cannot bypass Cloud Armor. Keep the Certificate Manager DNS-authorization CNAME in place so certificate renewal continues to work.

## Capacity and shutdown

The gateway keeps one Cloud Run instance warm and can scale to ten. Model compute scales independently and remains the bottleneck. Stop model charges without deleting data:

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
