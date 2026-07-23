# Remaining parity items requiring owner decisions

These are the production-parity items that are not purely engineering: they need a budget commitment, an org-level change, or external procurement. Each is scoped so the decision is concrete.

## Hosted private-inference credentials

- **Status**: the provider router, catalog, deployment wiring, price validation, and fail-closed
  readiness checks are implemented. Secret Manager resources are declared for Phala and Venice,
  but production has no enabled provider-key versions yet.
- **Decision needed**: fund the Phala and Venice accounts, create least-privilege production keys,
  and add them as enabled versions of `phala-api-key` and `venice-api-key`.
- **Launch gate**: do not publish the hosted catalog secret or promote a router revision until both
  provider keys pass a private canary request and evidence-header check. The service intentionally
  returns not-ready when an enabled catalog provider lacks its credential.

## Hosted-provider attestation policy

- **Status**: endpoint privacy is filtered before routing, expected provider evidence is captured,
  missing required evidence fails closed, and raw evidence status is never mislabeled as verified.
  Full cryptographic verification of provider hardware quotes, software measurements, freshness,
  and response-signing-key binding is not yet implemented.
- **Decision needed**: approve exact Phala and Venice workload measurements/build provenance that
  production should trust, plus rotation policy when providers upgrade their serving stack.
- **Launch posture**: endpoints remain labeled `tee` or `e2ee-tee`, not `verified-tee`, until that
  trust policy and verifier are deployed. Marketing must preserve this distinction.

## Reserved / diversified GPU capacity

- **Status**: the confidential GPU pool is a single dedicated TDX node class (`enable_confidential_gpu_pool`, currently off; the live confidential path runs on the CPU TDX pool). Admission control protects whatever capacity exists but does not create more.
- **Decision needed**: whether to purchase a committed-use or reserved H100/TDX allocation, and in how many zones. This is a recurring cost commitment, not a code change.
- **When taken**: the model plane is already multi-region (us-central1 + us-east4). Diversifying GPU zones and turning on reservations is a Terraform variable flip plus a quota/commitment purchase.

## VPC Service Controls

- **Status**: not enabled. VPC-SC is configured at the organization/access-policy level, above this project's Terraform.
- **Decision needed**: define a service perimeter around the project's data services (Firestore, Storage, Secret Manager, Artifact Registry) and an ingress/egress policy that still allows the operator and CI. This risks breaking access if scoped wrong, so it needs an owner with org-admin context and a maintenance window.
- **Recommendation**: stage with a dry-run perimeter first; the platform already restricts model-pod egress and Cloud Run ingress, so VPC-SC is defense-in-depth rather than the only control.

## Independent penetration test

- **Status**: not performed. Internal drills (failover, Redis loss, restore, rollout, Binary Authorization, egress) are recorded under `docs/recovery-drills/`, but an independent test is by definition external.
- **Decision needed**: engage a third-party firm; scope should include the public API auth boundary, the Basic-auth site gate, tenant isolation, spend-limit enforcement under concurrency, and the attestation endpoint.
- **Prerequisite**: none technical — the platform is ready to test. This is a procurement and scheduling decision.

## Attestation hardening (partially owner, partially engineering)

The attestation endpoint returns a real, client-verifiable TDX quote today. Restricted drill records validate the control without publishing live infrastructure evidence. Two follow-ons remain, both engineering but worth an explicit priority call:

- Server-side quote verification / attestation PKI (verify against Intel PCS in-platform, not only client-side).
- Binding a measured-boot model image digest into report-data rather than gateway-asserted configuration, and gating confidential key release on a passing attestation (fail-closed key release).
