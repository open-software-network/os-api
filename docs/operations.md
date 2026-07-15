# Production operations

Examples use environment variables so the runbook is safe to reuse in another project:

```bash
export PROJECT_ID="your-gcp-project-id"
export REGION="us-central1"
```

## Service objectives

- Public control-plane availability target: 99.9% per calendar month.
- Confidential model availability is reported separately and is not included in the control-plane SLO while capacity uses a single TDX pool.
- The API never silently downgrades a request whose `X-Confidential-Compute` value is `required`.
- Prompts, completions, bearer tokens, wallet grants, and full request URLs must never appear in logs, metrics, traces, or support tickets.

## Cloud Run release process

The `Deploy production` GitHub workflow is manually dispatched from `main`. It authenticates with GCP Workload Identity Federation, builds an SBOM/provenance-attested image, pushes it to Artifact Registry, and deploys the image by digest to a private IAM-gated canary service. Configure these GitHub variables on the `production` environment from the authoritative Terraform outputs:

```bash
gh variable set GCP_PROJECT_ID --env production --body "$PROJECT_ID"
gh variable set GCP_ARTIFACT_IMAGE --env production \
  --body "$(terraform -chdir=deploy/terraform output -raw github_artifact_image)"
gh variable set GCP_DEPLOY_SERVICE_ACCOUNT --env production \
  --body "$(terraform -chdir=deploy/terraform output -raw github_deployer_service_account)"
gh variable set GCP_WORKLOAD_IDENTITY_PROVIDER --env production \
  --body "$(terraform -chdir=deploy/terraform output -raw github_workload_identity_provider)"
gh variable set PUBLIC_READY_URL --env production \
  --body "$(terraform -chdir=deploy/terraform output -raw public_ready_url)"
```

Before making the repository public, require a production-environment reviewer, disallow administrator bypass, and protect `main`; the Workload Identity provider already restricts tokens to the immutable repository and owner IDs on `refs/heads/main`. The workflow fails before authentication when any value is missing, when the image is not an untagged Artifact Registry path in `GCP_PROJECT_ID`, or when the service account differs from Terraform's `os-api-github-deployer` account. After authentication, it reads the load-balancer backend configuration and refuses to continue when Confidential Space managed instance groups serve the production edge. That deployment mode must use the digest-bound Terraform rollout in [the Confidential Space gateway runbook](confidential-gateway.md); the workflow updates only Cloud Run. For a Cloud Run edge, it deploys and promotes the identical digest to production only after:

1. The canary `/readyz` confirms Redis and Firestore access.
2. An IAM-authenticated canary `/v1/models` call succeeds with the dedicated smoke key.
3. The public readiness endpoint succeeds after traffic promotion.

Immediately before promotion, the workflow snapshots both regions' traffic assignments. If either promotion or the post-promotion readiness check fails, it automatically restores both snapshots. The previous revision also remains available for manual rollback:

```bash
gcloud run revisions list \
  --service=sovereign-api \
  --region="$REGION" \
  --project="$PROJECT_ID"

gcloud run services update-traffic sovereign-api \
  --region="$REGION" \
  --project="$PROJECT_ID" \
  --to-revisions=REVISION_NAME=100
```

Do not deploy mutable tags. Terraform owns service configuration and CI owns the digest-pinned Cloud Run application revision. When Confidential Space serves the edge, Terraform also owns the active gateway digest and managed-instance-group rollout.

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

Model server images are pinned by digest in `deploy/kubernetes` and never deployed by mutable tag. Every inference response identifies the artifact that served it: `x-model-backend` names the backend and `x-model-revision` carries the pinned image revision from the model configuration (`revision` per backend in `models.yaml`). When rolling a self-hosted model image, update the digest in the manifests and the matching `revision` in the in-cluster ConfigMap patch so the header stays truthful. The hosted provider catalog is `config/models.yaml` inside the signed gateway image; change it through review and publish a new signed gateway image rather than injecting mutable model configuration from Secret Manager.

Older installations may still have `cloudrun-model-config` and `cloudrun-model-config-secondary` instances in Terraform state. The current gateway does not read them. Before applying this version, detach those two instances from state so Terraform does not delete their retained Secret Manager versions:

```bash
terraform -chdir=deploy/terraform state rm \
  'google_secret_manager_secret.runtime["cloudrun-model-config"]' \
  'google_secret_manager_secret.runtime["cloudrun-model-config-secondary"]'
```

Confirm the signed image catalog is serving correctly, then retire the orphaned secrets under your normal retention policy.

Roll out with `scripts/model_rollout.sh`, which enforces three gates:

1. **Canary**: the exact new image starts as an off-service deployment on the production cluster (no Service selects it) and must answer golden prompts before any serving pod is touched.
2. **Health-gated promotion**: the CPU model deployment uses `RollingUpdate` with `maxSurge: 1, maxUnavailable: 0`, so the old model keeps serving until the new pod is ready; a stuck rollout is automatically undone. The surge pod schedules onto an autoscaled node (the pool allows two nodes), so allow ~5–15 minutes.
3. **Post-promote verification**: golden prompts run against the serving deployment; failure triggers an automatic `rollout undo`.

The GPU deployment keeps `Recreate` because it owns a single dedicated TDX GPU node; treat GPU model rollouts as brief planned unavailability of the confidential GPU pool until capacity is diversified. Roll back manually at any time with `kubectl -n inference rollout undo deployment/<name>` — data and gateway config are unaffected by model rollbacks. The latest exercised rollout and automatic rollback are recorded in [the 2026-07-10 model rollout drill](recovery-drills/2026-07-10-model-rollout.md).

## Confidential-compute attestation

Clients can request cryptographic proof that confidential inference runs on genuine Intel TDX hardware bound to the deployed workload. A privileged DaemonSet (`deploy/kubernetes/attestation`) on each confidential node exposes a minimal quote agent that turns a 64-byte report-data value into a raw TDX quote via the kernel's `configfs-tsm` interface; it never sees prompts, keys, or customer data. `POST /v1/attestation` with `{"nonce": "<8-128 url-safe chars>"}` returns the quote plus the canonical binding it commits to: the client nonce, the gateway version and region, and the confidential backends with their pinned model revisions. The quote's report-data field is the SHA-512 of that binding, so a verifier confirms freshness (their nonce) and workload identity (versions/revisions) by recomputing the hash and checking it appears in the quote, then validating the quote against Intel's PCS certificate chain.

The endpoint fails closed: if the agent is unreachable or returns an error, it responds `503 attestation_unavailable` and never substitutes unverifiable evidence. `ATTESTATION_AGENT_URL` per region points at the agent's internal load balancer; if that IP changes, update the Cloud Run env and Terraform var. What this proves is hardware-and-binding freshness; it is not yet a full remote-attestation PKI with quote-verification performed server-side, and the report-data binding covers configuration the gateway asserts rather than a measured boot of the model image — see the parity checklist for the remaining hardening.

## Billing reconciliation

Wallet charges use `inference:<billing_id>` as an idempotency key and are retried three times. A `wallet_charge_failed` event means inference was delivered and local usage settled, but the wallet charge did not confirm. Re-run only the idempotent charge for that billing ID after verifying its Firestore usage event. Never synthesize a new billing ID for the same usage.

## Key compromise

1. Revoke the key in the dashboard; revocation is effective on the next authenticated request.
2. Search request metadata by key ID or fingerprint, never by raw key.
3. Rotate any upstream credential if there is evidence it was exposed.
4. Preserve security logs and notify the owner using the account email.

## Supply chain

Binary Authorization enforces a deny-by-default policy on the inference clusters and every gateway Cloud Run service. Every non-system workload image, including digest-pinned third-party model servers, must carry a signature from the `os-api-release` attestor (a Cloud KMS EC-P256 key). Google-maintained GKE system images remain exempt through the global policy. Enforcement is admission-time, so it never disrupts running workloads, and it is opt-in per workload — other clusters in the project are unaffected.

The GitHub deploy workflow signs every image it builds, for both the multi-arch index digest (what Cloud Run resolves) and each platform manifest digest (what GKE evaluates). To sign a digest by hand during a manual release or rollback drill:

```bash
gcloud beta container binauthz attestations sign-and-create \
  --project="$PROJECT_ID" \
  --artifact-url="${REGION}-docker.pkg.dev/${PROJECT_ID}/sovereign-inference/<repo>@sha256:<digest>" \
  --attestor=os-api-release --attestor-project="$PROJECT_ID" \
  --keyversion="projects/${PROJECT_ID}/locations/${REGION}/keyRings/os-api-binauthz/cryptoKeys/release-signing/cryptoKeyVersions/1"
```

Sign both the index digest and its `linux/amd64` platform digest (resolve them with `docker buildx imagetools inspect <ref> --raw`). An unsigned image fails with `denied by attestor ... No attestations found that were valid and signed by a key trusted by the attestor`; sign the digest rather than editing the policy. The enforcement and signing drills are recorded in [the 2026-07-10 Binary Authorization drill](recovery-drills/2026-07-10-binauthz.md).

## Egress controls

The model pods handle raw prompts and are the platform's highest exfiltration risk, so their egress is restricted with GKE Dataplane V2 policies: a standard NetworkPolicy denies all egress except DNS and in-cluster traffic, and an `FQDNNetworkPolicy` allows HTTPS only to the HuggingFace hostnames the servers pull weights from. A compromised model server therefore cannot open an arbitrary C2 channel or ship prompts to an unlisted host; a blocked destination times out (verified against `example.com:443`) while HuggingFace stays reachable. If a model source changes, update `deploy/kubernetes/base/egress-policy.yaml` — do not widen it to all HTTPS. The gateway runs on Cloud Run with VPC egress restricted to private ranges for in-VPC traffic; tightening its public egress further is future hardening.

## Site access gate

The human-facing site, docs, dashboard, login callbacks, and dashboard JSON routes use HTTP Basic authentication when `SITE_BASIC_AUTH_USERNAME` and `SITE_BASIC_AUTH_PASSWORD` are configured. The password is stored in the `site-basic-auth-password` Secret Manager secret. `/v1/*`, `/healthz`, and `/readyz` are intentionally exempt so API-key clients and infrastructure probes continue to work. Rotate the site password by adding a new secret version and bumping `RUNTIME_CONFIG_VERSION` in both Cloud Run services.

## Edge and denial-of-service controls

Cloud Armor throttles `/v1/*` at 600 requests per source IP per minute. A source that reaches 1,800 requests in one minute is banned for ten minutes. These regional, approximate edge controls are intentionally looser than the 60 RPM per-key Redis limit and exist only to protect availability; they are not billing quotas. Monitoring alerts when rejected Bearer credentials exceed 60 per minute. Review load-balancer source distribution and request IDs before changing thresholds. Do not enable body-inspecting WAF rules on model prompts without a privacy and false-positive review.

Production DNS resolves the configured public API domain directly to Terraform's `production_edge_ip` output in DNS-only mode. Cloud Run uses `INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER`, the legacy Cloud Run domain mapping is removed, and the default `run.app` URL cannot bypass Cloud Armor. Keep the Certificate Manager DNS-authorization CNAME in place so certificate renewal continues to work.

## Capacity and shutdown

The gateway keeps one Cloud Run instance warm and can scale to ten. Model compute scales independently and remains the bottleneck.

Admission to each model backend is bounded by `max_concurrency` in `models.yaml`, enforced globally across every gateway instance and region through a shared Redis in-flight counter. Requests beyond the bound are rejected **before any billing reservation** with `503`, `Retry-After: 10`, and OpenAI-style error code `model_overloaded`; clients are expected to retry. The counter key self-heals via a 900-second expiry refreshed on each admit, so a crashed gateway instance cannot permanently shrink capacity. Size `max_concurrency` from measured backend saturation, not from the alert threshold: the live saturation test admitted exactly 8 of 24 concurrent requests at the configured limit of 8 and recovered immediately after the burst. Raising the limit without adding model capacity converts fast, honest 503s into slow queueing for everyone. Stop model charges without deleting data:

```bash
kubectl -n inference scale deployment/confidential-vllm-cpu --replicas=0
gcloud container clusters resize sovereign-inference \
  --node-pool=confidential-cpu-tdx \
  --num-nodes=0 \
  --region="$REGION" \
  --project="$PROJECT_ID" \
  --quiet
```

## Backup and recovery

Terraform provisions the active Firestore database whenever the public control plane is enabled; `enable_firestore_backups` controls only the export bucket and schedule. Firestore database location is effectively permanent, so review `region`, `enable_multi_region_control_plane`, and `multi_region_firestore_location` before the first apply. If the selected database already exists, import it before applying rather than attempting to recreate it:

```bash
# Single-region default database
terraform -chdir=deploy/terraform import \
  'google_firestore_database.primary[0]' \
  "projects/${PROJECT_ID}/databases/(default)"

# Multi-region named database; set this to the configured database ID.
export FIRESTORE_DATABASE_ID="os-api-global"
terraform -chdir=deploy/terraform import \
  'google_firestore_database.global[0]' \
  "projects/${PROJECT_ID}/databases/${FIRESTORE_DATABASE_ID}"
```

Import only the resource selected by the current Terraform variables, then inspect a full plan before applying. The same apply creates the composite indexes required by the key, usage, and audit list queries; do not promote traffic until Terraform reports those index resources ready. `firestore_collection_prefix` is authoritative for the runtime, indexes, and exports; changing it selects a different logical dataset and therefore requires an explicit data migration. Terraform state is stored in a versioned GCS backend. Secret Manager retains secret versions; Firestore is the system of record for keys, usage, privacy receipts, and customer audit metadata. Firestore point-in-time recovery retains document versions for seven days. Cloud Scheduler starts a managed export of `${firestore_collection_prefix}_api_keys`, `${firestore_collection_prefix}_usage`, `${firestore_collection_prefix}_audit_events`, and `${firestore_collection_prefix}_privacy_receipts` every day at 04:00 UTC. Single-region exports go to `${PROJECT_ID}-os-api-firestore-backups`; multi-region exports go to `${PROJECT_ID}-os-api-global-backups`. Both buckets are versioned, have a 30-day minimum retention period, and expire objects after 90 days. The retention policy is intentionally not locked until an owner explicitly accepts the irreversible lock.

Run and verify an export without waiting for the next schedule:

```bash
gcloud scheduler jobs run os-api-firestore-export \
  --location=us-central1 \
  --project="$PROJECT_ID"

gcloud firestore operations list \
  --database='(default)' \
  --project="$PROJECT_ID"

gcloud storage ls \
  "gs://${PROJECT_ID}-os-api-firestore-backups/"
```

Treat a successful scheduler HTTP response as "export accepted," not "backup complete." Close the check only after the Firestore operation reports `done: true`, has no error, and its timestamped prefix contains a matching `.overall_export_metadata` object.

Quarterly, restore the latest completed export into the designated isolated recovery project and record the measured recovery point and recovery time. The recovery project must use a bucket/database-compatible location, must not have production runtime service accounts, and must never receive production traffic. Verify key, usage, audit, and privacy-receipt collection counts; sample documents for schema compatibility; confirm raw key secrets and prompts are absent; and then destroy the recovery database. Never restore into production during a drill, and never reactivate an expired or revoked key.

The latest completed exercise is recorded in [the 2026-07-10 recovery drill](recovery-drills/2026-07-10.md).

## Regional failover

The gateway runs in us-central1 (`sovereign-api`) and us-east4 (`sovereign-api-secondary`) behind the same global load balancer, on the same image digest, against the same nam7 Firestore database. Every response carries an `x-gateway-region` header identifying the serving region. Both internal model load balancers set `allowGlobalAccess`, and the vLLM NetworkPolicies admit both gateway subnets (10.20.0.0/20 and 10.30.0.0/24); a change to either breaks cross-region inference silently, so rerun the failover drill after touching them.

Each region is independent for its regional Redis dependency, and both gateways run the same signed image and reviewed hosted-provider catalog. Spend limits and settlement remain globally consistent because they live in the multi-region Firestore database, not Redis. Optional self-hosted model clusters are managed separately through the Kubernetes overlays; do not use mutable Secret Manager model catalogs to alter a signed gateway release.

Because Redis is per-region and only backs rate limiting and admission control — which fail open — a Redis outage degrades one region rather than failing it. `/readyz` reflects this: it fails readiness only when Firestore (the system of record) is unreachable; Redis is reported as `degraded` but keeps the region ready and serving. A degraded region logs `redis_degraded` and raises the "serving with Redis degraded" alert; per-key RPM and per-backend `max_concurrency` are unenforced there until Redis recovers, while hard spend limits and Cloud Armor per-IP limits still apply. Consequence to remember: these limits are per-region bounds, so a cross-region traffic split can allow up to 2x a single region's configured rate before spend limits catch it.

There is no remaining shared regional single point of failure in the serving path. GPU capacity is still a single dedicated node class (see the capacity section) and remains the one non-replicated resource.

To fail away from a region, detach its NEG from the `os-api-edge` backend service and confirm the other region serves via the header; restore by re-running `terraform apply`, which is authoritative for the backend set. Failback routing is eventually consistent — judge recovery by the attached backend set and continuous 200s, not by which region serves your client. The latest exercise, including two defects the first attempt surfaced, is recorded in [the 2026-07-10 failover drill](recovery-drills/2026-07-10-failover.md).
