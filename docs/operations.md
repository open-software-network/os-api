# Operations and productization

## Deployment order

1. Apply Terraform with both GPU pools disabled and verify the private GKE control plane path.
2. Push the gateway image and deploy the gateway plus Redis connectivity.
3. Enable the L4 pool, deploy one small model, and test OpenAI SDK compatibility and streaming.
4. Optionally validate strict routing on the lower-cost C3 + TDX pool with the `standard-smoke` overlay.
5. Secure preemptible H100 quota in a supported zone, enable the confidential GPU pool, and verify both the TDX and H100 node labels.
6. Validate that deleting/tainting the TDX nodes makes `required` requests fail closed.
7. Publish the Cloud Run edge at `api.opensoftware.co`, register it in OpenSoftware Accounts, and enable Firestore key management and Credit wallet settlement.
8. Add Cloudflare WAF rules, alerting, and SLOs before broad public access.

## Minimum alerts

- Gateway 5xx and 429 rate
- Per-backend time to first token and completion latency
- Pending GPU pods and unavailable confidential capacity
- Redis connectivity/failures
- Model load failures and GPU Xid errors
- API authentication failure spikes

Metrics and traces must never include prompts, completions, raw authorization headers, or full request URLs.

## Commercial API roadmap

1. Model health/capacity-aware routing, circuit breaking, and admission control
2. Signed model/image supply chain and attestation-backed key release
3. Multi-zone standard capacity and a published confidential-capacity policy
4. Abuse automation, customer audit exports, and administrator controls
5. Additional OpenAI endpoints such as Responses, audio, reranking, and image generation
