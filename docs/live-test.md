# Live GCP test record

## Environment

- Date: July 9, 2026
- Project: `autonomous-lab-490610`
- Region/zone: `us-central1` / `us-central1-a`
- Billing account: `014271-346F7E-5C47CB` (`Alongside + GCP Credits`)
- GKE: regional private-node Standard cluster, `1.36.0-gke.4447000`
- Model backend: vLLM `0.23.0`, Qwen2.5 0.5B Instruct
- Compute tested: one NVIDIA L4 and one `c3-standard-4` Intel TDX node
- Edge exposure: local `kubectl port-forward` only; no public load balancer

Terraform state is in the versioned, uniform-access bucket `gs://autonomous-lab-490610-sovereign-tfstate/platform`. A gross-spend budget of $100/month is scoped to the project, with thresholds at 50%, 80%, and 100%.

## Results

| Check | Result |
| --- | --- |
| Gateway replicas | 2/2 ready |
| TLS + AUTH Memorystore readiness | Passed |
| L4 autoscaling | Pool scaled 0 → 1 and registered one healthy GPU |
| Model discovery | `200`, `qwen2.5-0.5b-instruct` |
| Non-streaming chat | `200`, `REAL GCP OK`, 1.007 s |
| SSE chat | `200`, `one two three`, 0.112 s total |
| Confidential `required` without TEE capacity | `409 confidential_compute_unavailable` |
| TDX node attestation metadata | GCE reported `enableConfidentialCompute: true`, type `TDX` |
| Confidential placement | Pod ran only on the `confidential-cpu-tdx` pool with the GKE `TDX` label |
| Confidential non-streaming chat | `200`, 2.344 s, `X-Compute-Privacy: confidential` |
| Confidential SSE chat | `200`, `One Two Three`, `X-Model-Backend: confidential-vllm-cpu` |
| Invalid API key | `401 invalid_api_key` |
| Prompt/credential log scan | No prompt-body or client-key matches |

The 9.19 GB CUDA vLLM image took 3m37s on the first pull. The 0.92 GiB model weights downloaded in 8.3s and loaded in 0.35s. First-run compile and CUDA graph warmup took about 38s.

The confidential CPU image took 53s to pull. Its weights downloaded in 9.2s and loaded in 0.52s; CPU compilation and warmup took about 90s. The tiny smoke model did not follow the first exact-match prompt perfectly, but the request completed successfully and the subsequent SSE request returned the requested three words. This test validates TDX placement, strict routing, authentication, streaming, and privacy headers. It does **not** validate H100 confidential-GPU performance; the project currently has no preemptible H100 quota.

## L4 versus confidential H100 list price

The production shapes in Terraform have materially different cost profiles in `us-central1`:

| Shape | Billing mode | Approx. hourly | 730-hour month |
| --- | --- | ---: | ---: |
| `g2-standard-8`, one L4 | On demand | $0.85 | $623 |
| `a3-highgpu-1g`, one H100 + TDX | Spot plus confidential-compute surcharge | $6.46 | $4,716 |

The confidential H100 shape is about 7.6 times the hourly cost of the L4 shape. Its estimate consists of about $6.02/hour for the Spot A3/H100 VM and $0.4391592/hour for Confidential Computing. Confidential H100 GKE nodes require Spot or Flex-start provisioning and preemptible H100 quota, so capacity can be interrupted. Prices are public list estimates before credits, discounts, disks, network, GKE, and shared platform services.

## Current cost state

Both model deployments were scaled to zero immediately after testing, and the L4 and C3 + TDX node pools were explicitly resized to zero. The following resources remain live for continued iteration:

- One regional GKE cluster and one `e2-standard-4` system node
- One 1 GiB Standard HA Memorystore for Redis instance
- Private VPC, subnet, NAT, Artifact Registry, Secret Manager, and Terraform state bucket
- Two gateway pods behind a private ClusterIP service
- Scale-to-zero L4 and C3 + TDX node pools with no nodes currently running

At public list prices, the persistent core is approximately $0.30/hour or $220/month before credits: about $0.10/hour for regional GKE management, $0.064/hour for Redis, and roughly $0.13/hour for the system VM, plus small disk/NAT/storage charges. The L4 adds about $0.85/hour only while a GPU node is present.

## Public HTTPS test

Cloud Run now exposes `https://sovereign-api-4axxohm2tq-uc.a.run.app/v1` with a dedicated app key stored in Secret Manager. The edge connects to Redis and the model only over the private VPC. External verification returned:

| Check | Result |
| --- | --- |
| Readiness | `200` |
| Missing key | `401` |
| Invalid key | `401` |
| Authenticated model discovery | `200`, `qwen2.5-0.5b-instruct` |
| Authenticated chat | `200`, 0.876 s |
| Authenticated SSE | `200`, streaming body received |
| Privacy headers | `confidential`, `confidential-vllm-cpu` |
| Automatic tool choice | `200`, structured Hermes tool call in JSON and SSE |
| Extended context | `200`, 4,630 prompt tokens plus tools after raising the window to 32,768 |

L4 allocations were retried in `us-central1-a`, `us-central1-b`, and `us-central1-c`; each returned `ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS`. The public test therefore runs the same model on the lower-cost Intel TDX CPU pool until L4 capacity returns. Unlike the earlier zero-cost state described above, the live TDX node now accrues compute charges while `confidential-vllm-cpu` remains at one replica.

## Run another smoke test

```bash
gcloud container clusters get-credentials sovereign-inference \
  --region us-central1 \
  --project autonomous-lab-490610

kubectl -n inference scale deployment/standard-vllm --replicas=1
kubectl -n inference rollout status deployment/standard-vllm --timeout=10m
kubectl -n inference port-forward service/gateway 18080:80
```

In another terminal, retrieve the one-time smoke key without printing it and call the API:

```bash
export API_KEY="$(gcloud secrets versions access latest \
  --secret=smoke-client-api-key \
  --project=autonomous-lab-490610)"

curl http://127.0.0.1:18080/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Confidential-Compute: disabled" \
  -d '{"model":"qwen2.5-0.5b-instruct","messages":[{"role":"user","content":"Hello"}]}'
```

Stop GPU charges afterward:

```bash
kubectl -n inference scale deployment/standard-vllm --replicas=0
gcloud container clusters resize sovereign-inference \
  --node-pool=standard-l4 \
  --num-nodes=0 \
  --region=us-central1 \
  --project=autonomous-lab-490610 \
  --quiet
```

For a confidential CPU smoke test, scale `confidential-vllm-cpu` to one replica, wait for it to become ready, and send the same request with `X-Confidential-Compute: required`. Scale it down afterward and explicitly resize `confidential-cpu-tdx` to zero, following the same pattern as the L4 commands above.
