# GKE deployment

The checked-in manifests are Google Kubernetes Engine templates, not portable generic Kubernetes manifests or a ready-to-apply production environment. They rely on GKE node labels, load-balancer annotations, and Dataplane V2 `FQDNNetworkPolicy`. Before applying an overlay:

1. Build the gateway and attestation agent from the repository root (for example, `docker build -f deploy/attestation-agent/Dockerfile .`), push both to a registry you control, and sign their immutable digests.
2. Replace every `example.invalid` image and all-zero digest placeholder.
3. Create `gateway-secrets` from `base/secrets.example.yaml` through your secret-management workflow; never commit the rendered Secret.
4. Review node selectors, CIDR ranges, provider credentials, model licenses, resource limits, and the privileged TDX quote-agent boundary for your cluster.
5. Render the chosen overlay with `kubectl kustomize` and review the complete output before applying it.

`scripts/model_rollout.sh` accepts only digest-pinned candidate images. Its off-service canary retains the model-network label, so the same egress policy covers both serving and canary pods.
