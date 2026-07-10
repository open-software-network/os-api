# OpenSoftware API

## Connection details

- Site and dashboard: `https://api.opensoftware.co`
- Base URL: `https://api.opensoftware.co/v1`
- Model: `qwen2.5-0.5b-instruct`
- Context window: 32,768 tokens
- Current compute: `c3-standard-4` with Intel TDX
- Rate limit: 60 requests per minute per API key
- OpenAI automatic tool choice: enabled with the vLLM Hermes parser

The public edge uses a global Google HTTPS load balancer, Certificate Manager, and Cloud Armor in front of Cloud Run. Managed API keys are created in the OpenSoftware-login dashboard and stored as SHA-256 hashes in Firestore. The service uses TLS-authenticated Memorystore for rate limits and reaches vLLM through a private internal GKE load balancer.

Create a key at `https://api.opensoftware.co/dashboard`. Set its expiration and optional lifetime spend limit. The raw key is displayed once; only the prefix and hash are retained. Use the key only from a server-side application. Do not embed it in browser JavaScript or a mobile binary.

The original app-test key remains available during migration:

Retrieve the app-test key without putting it in shell history:

```bash
export SOVEREIGN_API_KEY="$(gcloud secrets versions access latest \
  --secret=app-client-api-key \
  --project=autonomous-lab-490610)"
export SOVEREIGN_BASE_URL="https://api.opensoftware.co/v1"
```

## OpenAI client

```python
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["SOVEREIGN_API_KEY"],
    base_url=os.environ["SOVEREIGN_BASE_URL"],
)

response = client.chat.completions.create(
    model="qwen2.5-0.5b-instruct",
    messages=[{"role": "user", "content": "Hello from my app"}],
)
print(response.choices[0].message.content)
```

## curl

```bash
curl "$SOVEREIGN_BASE_URL/chat/completions" \
  -H "Authorization: Bearer $SOVEREIGN_API_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Confidential-Compute: required" \
  -d '{
    "model": "qwen2.5-0.5b-instruct",
    "messages": [{"role": "user", "content": "Hello from my app"}],
    "stream": false
  }'
```

Successful model responses currently include:

```text
X-Compute-Privacy: confidential
X-Model-Backend: confidential-vllm-cpu
X-Request-ID: 62de42e8-42ea-45a7-99c3-b1f3efb15ea7
X-RateLimit-Limit-Requests: 60
X-RateLimit-Remaining-Requests: 59
X-RateLimit-Reset-Requests: 1783708800
```

`X-RateLimit-Reset-Requests` is a Unix timestamp. A `429` response also includes `Retry-After`. Supply your own safe `X-Request-ID` (letters, digits, `.`, `_`, `:`, `/`, or `-`, up to 128 characters) to correlate an API error with support; otherwise the gateway creates one.

The L4 backend remains configured, but all three L4 zones in `us-central1` reported temporary capacity stockout during public deployment. Switch the Cloud Run model-config secret back to the private L4 load balancer after L4 capacity becomes available.

## Operational commands

Apply the current live backend state:

```bash
kubectl apply -k deploy/kubernetes/overlays/public-live
```

Stop model compute charges:

```bash
kubectl -n inference scale deployment/confidential-vllm-cpu --replicas=0
gcloud container clusters resize sovereign-inference \
  --node-pool=confidential-cpu-tdx \
  --num-nodes=0 \
  --region=us-central1 \
  --project=autonomous-lab-490610 \
  --quiet
```
