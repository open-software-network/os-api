# Threat model

## Protected assets

- User prompts and model outputs
- Model weights and private adapters
- Tenant API keys and upstream service credentials
- Usage and billing integrity

## Claims this implementation can make

- The gateway source does not write prompt or response bodies to logs or storage.
- A request with confidential compute `required` is rejected when a confidential backend is absent; it is never routed to ordinary compute.
- The confidential vLLM pod can schedule only on an H100 node labeled by GKE as Intel TDX confidential compute.
- API keys are compared as SHA-256 digests and are not stored in plaintext by the gateway.

## Claims it cannot make yet

- End-to-end verifiable attestation to an external API client. GKE node labels are a scheduler control, not cryptographic proof presented to the caller.
- Protection from a malicious application image. A TEE protects the workload boundary; it does not make vulnerable or exfiltrating code safe.
- Guaranteed deletion from GPU/CPU memory at a user-selected instant.
- Anonymity from network metadata, tenant usage records, abuse systems, or billing systems.
- High availability for confidential H100 inference. Current GCP H100 confidential nodes require interruptible provisioning and quota/capacity planning.

## Required production controls

1. Pin all deployed images by digest, sign them, and enforce signatures with Binary Authorization.
2. Use an attestation policy bound to the exact workload image digest before releasing model or decryption keys. Confidential Space is a strong option when externally verifiable single-workload attestation matters more than Kubernetes orchestration.
3. Disable body capture at the HTTPS edge, APM, service mesh, exception tracker, and vLLM layer—not only in this gateway.
4. Use VPC Service Controls, private nodes, egress allowlists, and separate service accounts per workload.
5. Rotate tenant and upstream keys; support immediate revocation through a control-plane datastore/cache.
6. Encrypt model disks with CMEK where contractual key control is required and define key-destruction procedures.
7. Run independent penetration tests and produce a data-flow inventory before making marketing privacy claims.

